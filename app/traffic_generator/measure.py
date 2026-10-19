"""Send one request and measure it."""

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.traffic_generator.metrics import throughput_mbps
from app.traffic_generator.profiles import PlannedRequest

_SERVER_TIMING_DUR = re.compile(r"\bdur=([0-9.]+)")


@dataclass
class Measurement:
    url: str
    success: bool
    status_code: int | None = None
    bytes_received: int = 0
    time_to_first_byte_ms: float | None = None
    server_time_ms: float | None = None
    latency_ms: float | None = None
    total_duration_ms: float | None = None
    transfer_duration_ms: float | None = None
    throughput_mbps: float | None = None
    new_connection: bool | None = None
    connect_ms: float | None = None
    error: str | None = None


class _ConnectionTrace:
    """Collects httpcore trace events to see whether a request opened a new connection."""

    def __init__(self) -> None:
        self.seen_any = False
        self.connect_started: float | None = None
        self.connect_ms: float | None = None

    async def __call__(self, event: str, info: dict[str, Any]) -> None:
        self.seen_any = True
        if event == "connection.connect_tcp.started":
            self.connect_started = time.perf_counter()
        elif event == "connection.connect_tcp.complete" and self.connect_started is not None:
            self.connect_ms = (time.perf_counter() - self.connect_started) * 1000

    @property
    def new_connection(self) -> bool | None:
        # Transports without tracing (e.g. in-process test transports) report nothing.
        if not self.seen_any:
            return None
        return self.connect_started is not None


def _parse_server_time_ms(header: str | None) -> float | None:
    if not header:
        return None
    match = _SERVER_TIMING_DUR.search(header)
    return float(match.group(1)) if match else None


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


async def measure_request(
    client: httpx.AsyncClient, planned: PlannedRequest, timeout_s: float
) -> Measurement:
    """Send a GET request and measure it.

    Network and HTTP errors are recorded on the Measurement rather than raised,
    so one bad request cannot end the run. Cancellation is not caught.
    """
    trace = _ConnectionTrace()
    request = client.build_request(
        "GET", planned.path, params=planned.params, extensions={"trace": trace}
    )
    result = Measurement(url=str(request.url), success=False)

    started = time.perf_counter()
    headers_at: float | None = None
    try:
        # httpx timeouts are per network operation; this bounds the whole request.
        async with asyncio.timeout(timeout_s):
            response = await client.send(request, stream=True)
            try:
                headers_at = time.perf_counter()
                result.status_code = response.status_code
                result.server_time_ms = _parse_server_time_ms(response.headers.get("server-timing"))
                # Raw bytes: the body is not decoded, so the count is what crossed the network.
                async for chunk in response.aiter_raw():
                    result.bytes_received += len(chunk)
            finally:
                await response.aclose()
        finished = time.perf_counter()
    except TimeoutError:
        result.error = f"timeout after {timeout_s}s"
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    result.new_connection = trace.new_connection
    result.connect_ms = None if trace.connect_ms is None else round(trace.connect_ms, 3)

    if headers_at is not None:
        ttfb_ms = _ms(headers_at - started)
        result.time_to_first_byte_ms = ttfb_ms
        # Without Server-Timing the network share cannot be separated; fall back to TTFB.
        if result.server_time_ms is None:
            result.latency_ms = ttfb_ms
        else:
            result.latency_ms = round(max(ttfb_ms - result.server_time_ms, 0.0), 3)

    if result.error is not None:
        return result

    transfer_s = finished - headers_at
    result.total_duration_ms = _ms(finished - started)
    result.transfer_duration_ms = _ms(transfer_s)
    result.throughput_mbps = throughput_mbps(result.bytes_received, transfer_s)

    if result.status_code is not None and result.status_code >= 400:
        result.error = f"HTTP {result.status_code}"
    elif planned.expected_bytes is not None and result.bytes_received != planned.expected_bytes:
        result.error = (
            f"incomplete body: expected {planned.expected_bytes} bytes, "
            f"received {result.bytes_received}"
        )
    else:
        result.success = True
    return result
