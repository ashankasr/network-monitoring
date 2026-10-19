"""Dashboard API tests.

The query-shaping tests are pure. The end-to-end test stores events through the
ingest worker's Store into a throwaway MongoDB database and reads them back
through the API. It is skipped when MongoDB is not running (`docker compose up -d`).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import AsyncMongoClient, MongoClient
from pymongo.errors import PyMongoError

from app import settings
from app.dashboard_api.queries import bucket_seconds, qoe_from_stats, series_from_buckets, stats_from_group
from app.events import REQUEST_MEASURED, RUN_STARTED, Envelope
from app.ingest_worker.store import Store

T0 = datetime(2026, 10, 17, 10, 0, 0, tzinfo=UTC)


def test_bucket_seconds_targets_about_120_points() -> None:
    assert bucket_seconds(10) == 1
    assert bucket_seconds(300) == 3
    assert bucket_seconds(3600) == 30
    assert bucket_seconds(100_000) == 60


def test_stats_from_group_matches_summary_shape() -> None:
    group = {
        "all": 11, "cancelled": 1, "successful": 9, "bytes": 5_000_000,
        "lat_avg": 2.0, "lat_min": 1.0, "lat_max": 9.0, "lat_pct": [2.0, 5.0, 8.0],
        "thr_bytes": 1_000_000, "thr_ms": 1000, "thr_samples": 4,
    }
    stats = stats_from_group(group, elapsed_s=5)
    assert stats["requests"] == {
        "total": 10, "successful": 9, "failed": 1, "cancelled": 1, "success_pct": 90.0, "achieved_rps": 2.0,
    }
    assert stats["latency_ms"]["p95"] == 5.0
    assert stats["throughput_mbps"] == {"aggregate": 8.0, "samples": 4}
    assert qoe_from_stats(stats)["qoe_model"] == "poc-v1"


def test_stats_from_empty_group() -> None:
    stats = stats_from_group(None, elapsed_s=0)
    assert stats["requests"]["total"] == 0
    assert stats["latency_ms"] is None
    assert qoe_from_stats(stats)["qoe_score"] is None


def test_series_fills_empty_buckets_with_zero_rate() -> None:
    buckets = [
        {"_id": T0, "requests": 2, "ok": 1, "lat_pct": [1.0, 2.0], "thr_bytes": 0, "thr_ms": 0},
        {"_id": T0 + timedelta(seconds=4), "requests": 4, "ok": 4, "lat_pct": [1.0, 3.0],
         "thr_bytes": 1_000_000, "thr_ms": 500},
    ]
    series = series_from_buckets(buckets, bucket_s=2)
    assert [p["t"] for p in series] == [T0, T0 + timedelta(seconds=2), T0 + timedelta(seconds=4)]
    assert series[0]["failed"] == 1 and series[0]["rps"] == 1.0
    assert series[1] == {"t": T0 + timedelta(seconds=2), "ok": 0, "failed": 0, "rps": 0.0,
                         "latency_p50": None, "latency_p95": None, "throughput_mbps": None}
    assert series[2]["throughput_mbps"] == 16.0


def mongo_available() -> bool:
    try:
        MongoClient(settings.MONGO_URL, serverSelectionTimeoutMS=500).admin.command("ping")
        return True
    except PyMongoError:
        return False


@pytest.mark.skipif(not mongo_available(), reason="MongoDB not running (docker compose up -d)")
def test_events_stored_by_worker_are_served_by_api(monkeypatch: pytest.MonkeyPatch) -> None:
    db_name = f"qoe_test_{uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "MONGO_DB", db_name)
    started = datetime.now(UTC) - timedelta(seconds=10)

    def request(i: int, success: bool) -> dict:
        return {
            "timestamp": (started + timedelta(seconds=i)).isoformat(),
            "test_run_id": "run-it", "device_id": "d", "train_id": "t", "journey_id": "j",
            "profile": "web_browsing", "request_id": f"req-{i}", "url": f"http://x/page?i={i}",
            "http_method": "GET", "outcome": "success" if success else "failure", "success": success,
            "status_code": 200 if success else 500, "latency_ms": 2.0 + i, "bytes_received": 100_000,
            "transfer_duration_ms": 10.0 if success else None, "throughput_mbps": 80.0 if success else None,
        }

    async def store_events() -> None:
        client = AsyncMongoClient(settings.MONGO_URL, tz_aware=True)
        store = Store(client[db_name])
        await store.ensure_indexes()
        await store.apply(Envelope(type=RUN_STARTED, data={
            "test_run_id": "run-it", "profile": "web_browsing", "target": "http://x",
            "metadata": {"device_id": "d", "train_id": "t", "journey_id": "j"},
            "config": {"run": {"duration_seconds": 30}}, "started_at": started.isoformat(),
        }))
        for i in range(5):
            envelope = Envelope(type=REQUEST_MEASURED, data=request(i, success=i != 3))
            await store.apply(envelope)
            await store.apply(envelope)  # redelivery must not duplicate
        await client.close()

    try:
        asyncio.run(store_events())
        with TestClient(__import__("app.dashboard_api.main", fromlist=["app"]).app) as api:
            runs = api.get("/api/runs").json()
            assert [r["test_run_id"] for r in runs] == ["run-it"]
            assert runs[0]["status"] == "running"

            live = api.get("/api/runs/run-it/live").json()
            assert live["source"] == "live"
            assert live["stats"]["requests"]["total"] == 5
            assert live["stats"]["requests"]["failed"] == 1
            assert live["stats"]["throughput_mbps"]["samples"] == 4
            assert live["qoe"]["qoe_score"] is not None
            assert sum(p["ok"] + p["failed"] for p in live["series"]) == 5
            assert live["recent"][0]["url"] == "http://x/page?i=4"

            assert api.get("/api/runs/nope/live").status_code == 404
    finally:
        MongoClient(settings.MONGO_URL).drop_database(db_name)
