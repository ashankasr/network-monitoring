"""Runs simulated users against the target within the configured limits."""

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.limits import TEST_ENDPOINT_HEADER
from app.traffic_generator.config import Config
from app.traffic_generator.measure import measure_request
from app.traffic_generator.models import RequestRecord
from app.traffic_generator.pacing import RateLimiter
from app.traffic_generator.profiles import PlannedRequest, create_profile
from app.traffic_generator.reporter import JsonlWriter

USER_AGENT = "onboard-qoe-poc/0.1 (synthetic test traffic)"

ClientFactory = Callable[[], httpx.AsyncClient]


class PreflightError(Exception):
    """The target is unreachable or is not the QoE test endpoint."""


@dataclass
class Progress:
    elapsed_s: float
    duration_s: float
    requests: int
    successful: int
    failed: int
    avg_latency_ms: float | None


@dataclass
class RunResult:
    started_at: datetime
    ended_at: datetime
    duration_s: float
    stopped_by: str  # "duration", "interrupt" or "error"
    records: list[RequestRecord]
    errors: list[str] = field(default_factory=list)


def default_client_factory(config: Config) -> ClientFactory:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=config.target.base_url,
            timeout=config.run.request_timeout_seconds,
            # Ask for the body as-is so byte counts reflect the payload (D-07).
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            follow_redirects=False,
            # Ignore proxy settings from the environment: traffic goes only to the configured target.
            trust_env=False,
        )

    return factory


async def preflight(config: Config, client_factory: ClientFactory | None = None) -> None:
    """Check that the target answers /health and identifies itself as the test endpoint."""
    base_url = config.target.base_url
    async with (client_factory or default_client_factory(config))() as client:
        try:
            response = await client.get("/health")
        except httpx.HTTPError as exc:
            raise PreflightError(f"cannot reach {base_url}/health: {exc!r}") from exc
    if response.status_code != 200 or TEST_ENDPOINT_HEADER not in response.headers:
        raise PreflightError(
            f"{base_url} is not the QoE test endpoint "
            f"(GET /health returned {response.status_code} without the {TEST_ENDPOINT_HEADER} header)"
        )
    try:
        healthy = response.json().get("status") == "ok"
    except ValueError:
        healthy = False
    if not healthy:
        raise PreflightError(f"{base_url}/health did not report status ok")


class Runner:
    def __init__(
        self,
        config: Config,
        run_id: str,
        writer: JsonlWriter,
        client_factory: ClientFactory | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        progress_interval_s: float = 2.0,
        on_record: Callable[[RequestRecord], None] | None = None,
    ) -> None:
        self._config = config
        self._run_id = run_id
        self._writer = writer
        self._client_factory = client_factory or default_client_factory(config)
        self._on_progress = on_progress
        # Called with every record after it is written; must not block.
        self._on_record = on_record
        self._progress_interval_s = progress_interval_s
        self._limiter = RateLimiter(config.run.max_requests_per_second, config.run.max_mbps)
        self.records: list[RequestRecord] = []

    async def run(self) -> RunResult:
        """Run until the duration ends or the task is cancelled (Ctrl+C), then return what was measured.

        At the deadline no new requests start; requests already in flight finish,
        bounded by the request timeout, so a run lasts at least the configured
        duration and at most duration + timeout. On cancellation, in-flight
        requests are abandoned and recorded as cancelled.
        """
        run_cfg = self._config.run
        started_at = datetime.now(UTC)
        start = time.monotonic()
        deadline = start + run_cfg.duration_seconds
        stopped_by = "duration"

        users = [
            asyncio.create_task(self._user(index, deadline), name=f"user-{index}")
            for index in range(run_cfg.concurrent_users)
        ]
        progress = asyncio.create_task(self._report_progress(start)) if self._on_progress else None
        try:
            outcomes = await asyncio.gather(*users, return_exceptions=True)
            # Users stop early when their next slot falls past the deadline; keep the
            # run window equal to the configured duration so rates are comparable.
            await asyncio.sleep(deadline - time.monotonic())
        except asyncio.CancelledError:
            # asyncio.run() cancels the main task on the first Ctrl+C; a second one raises KeyboardInterrupt.
            stopped_by = "interrupt"
            for task in users:
                task.cancel()
            outcomes = await asyncio.gather(*users, return_exceptions=True)
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        finally:
            if progress is not None:
                progress.cancel()

        errors = [f"{type(o).__name__}: {o}" for o in outcomes if isinstance(o, Exception)]
        if errors:
            stopped_by = "error"
        return RunResult(
            started_at=started_at,
            ended_at=datetime.now(UTC),
            duration_s=time.monotonic() - start,
            stopped_by=stopped_by,
            records=self.records,
            errors=errors,
        )

    def progress(self, elapsed_s: float) -> Progress:
        completed = [r for r in self.records if r.outcome != "cancelled"]
        latencies = [r.latency_ms for r in completed if r.success and r.latency_ms is not None]
        successful = sum(1 for r in completed if r.success)
        return Progress(
            elapsed_s=elapsed_s,
            duration_s=self._config.run.duration_seconds,
            requests=len(completed),
            successful=successful,
            failed=len(completed) - successful,
            avg_latency_ms=sum(latencies) / len(latencies) if latencies else None,
        )

    async def _report_progress(self, start: float) -> None:
        assert self._on_progress is not None
        while True:
            await asyncio.sleep(self._progress_interval_s)
            self._on_progress(self.progress(time.monotonic() - start))

    async def _user(self, index: int, deadline: float) -> None:
        seed = self._config.run.seed
        rng = random.Random(None if seed is None else f"{seed}:{index}")
        profile = create_profile(self._config.profile, rng)
        async with self._client_factory() as client:
            while True:
                planned = profile.next_request()
                start_at = self._limiter.reserve(planned.expected_bytes or 0)
                if start_at >= deadline:
                    return
                await asyncio.sleep(start_at - time.monotonic())
                await self._send(client, planned, profile.name)
                if time.monotonic() + planned.think_time_s >= deadline:
                    return
                await asyncio.sleep(planned.think_time_s)

    async def _send(self, client: httpx.AsyncClient, planned: PlannedRequest, profile_name: str) -> None:
        metadata = self._config.metadata
        common = {
            "timestamp": datetime.now(UTC),
            "test_run_id": self._run_id,
            "device_id": metadata.device_id,
            "train_id": metadata.train_id,
            "journey_id": metadata.journey_id,
            "profile": profile_name,
            "request_id": uuid4().hex,
            "http_method": "GET",
            "expected_bytes": planned.expected_bytes,
        }
        try:
            measurement = await measure_request(
                client, planned, self._config.run.request_timeout_seconds
            )
        except asyncio.CancelledError:
            url = client.build_request("GET", planned.path, params=planned.params).url
            self._record(
                RequestRecord(
                    **common, url=str(url), outcome="cancelled", success=False,
                    error="cancelled: run interrupted",
                )
            )
            raise
        outcome = "success" if measurement.success else "failure"
        self._record(RequestRecord(**common, outcome=outcome, **asdict(measurement)))

    def _record(self, record: RequestRecord) -> None:
        self.records.append(record)
        self._writer.write(record)
        if self._on_record is not None:
            self._on_record(record)
