import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.test_server.main import app
from app.traffic_generator.config import Config
from app.traffic_generator.reporter import JsonlWriter, build_summary
from app.traffic_generator.runner import PreflightError, Runner, preflight

BASE_URL = "http://testserver"


def asgi_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


def make_config(run: dict, profile: dict | None = None) -> Config:
    return Config.model_validate(
        {
            "target": {"base_url": BASE_URL},
            "run": {"seed": 1, **run},
            "profile": {"think_time_min_ms": 0, "think_time_max_ms": 0, "payload_min_kb": 1,
                        "payload_max_kb": 100, **(profile or {})},
        }
    )


def run_once(config: Config, path: Path, cancel_after_s: float | None = None):
    async def go():
        with JsonlWriter(path) as writer:
            runner = Runner(config, "test-run", writer, client_factory=asgi_client)
            task = asyncio.create_task(runner.run())
            if cancel_after_s is not None:
                await asyncio.sleep(cancel_after_s)
                task.cancel()
            return await task

    return asyncio.run(go())


def test_run_respects_duration_and_rate_limit(tmp_path: Path) -> None:
    config = make_config({"duration_seconds": 1, "concurrent_users": 3, "max_requests_per_second": 10})
    path = tmp_path / "requests.jsonl"
    result = run_once(config, path)

    assert result.stopped_by == "duration"
    assert 1.0 <= result.duration_s < 2.0
    # Requests start at t = 0, 0.1, ..., 0.9: never more than rate x duration (+1 for t = 0).
    assert 5 <= len(result.records) <= 11
    assert all(r.success for r in result.records)

    lines = path.read_text().splitlines()
    assert len(lines) == len(result.records)
    first = json.loads(lines[0])
    assert first["test_run_id"] == "test-run"
    assert first["bytes_received"] == first["expected_bytes"]

    summary = build_summary(config, "test-run", result)
    assert summary["requests"]["success_pct"] == 100
    assert summary["qoe"]["qoe_score"] is not None


def test_failures_are_recorded_not_raised(tmp_path: Path) -> None:
    config = make_config({"duration_seconds": 0.5, "max_requests_per_second": 10}, {"error_rate": 1.0})
    result = run_once(config, tmp_path / "r.jsonl")
    assert result.records
    assert all(r.outcome == "failure" and r.status_code == 500 for r in result.records)
    assert build_summary(config, "x", result)["qoe"]["qoe_score"] == 0


def test_cancellation_keeps_results_and_marks_in_flight_requests(tmp_path: Path) -> None:
    config = make_config(
        {"duration_seconds": 30, "concurrent_users": 2, "max_requests_per_second": 10},
        {"server_delay_min_ms": 2000, "server_delay_max_ms": 2000},
    )
    path = tmp_path / "r.jsonl"
    result = run_once(config, path, cancel_after_s=0.3)

    assert result.stopped_by == "interrupt"
    assert result.duration_s < 2
    assert [r.outcome for r in result.records] == ["cancelled", "cancelled"]
    assert len(path.read_text().splitlines()) == 2


def test_preflight_accepts_test_endpoint() -> None:
    asyncio.run(preflight(make_config({}), asgi_client))


def test_preflight_rejects_other_services() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_URL)

    with pytest.raises(PreflightError, match="not the QoE test endpoint"):
        asyncio.run(preflight(make_config({}), factory))
