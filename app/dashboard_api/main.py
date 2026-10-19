"""Read-only HTTP API over MongoDB for the live dashboard.

    uv run uvicorn app.dashboard_api.main:app --host 127.0.0.1 --port 8001

If the React app has been built (`npm run build` in web/), it is served from `/` too.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pymongo import AsyncMongoClient

from app import settings
from app.dashboard_api.queries import (
    bucket_seconds,
    qoe_from_stats,
    series_from_buckets,
    series_pipeline,
    stats_from_group,
    stats_pipeline,
)

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
RECENT_LIMIT = 15
# A run still "running" with no activity for this long most likely lost its generator (killed, crashed).
STALE_AFTER = timedelta(seconds=60)

RUN_FIELDS = ("profile", "target", "metadata", "status", "started_at", "ended_at", "last_request_at")
RECENT_FIELDS = (
    "timestamp", "request_id", "url", "outcome", "status_code", "latency_ms",
    "time_to_first_byte_ms", "total_duration_ms", "bytes_received", "throughput_mbps", "error",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client: AsyncMongoClient = AsyncMongoClient(
        settings.MONGO_URL, tz_aware=True, serverSelectionTimeoutMS=3000
    )
    app.state.db = client[settings.MONGO_DB]
    yield
    await client.close()


app = FastAPI(title="QoE Dashboard API", lifespan=lifespan)


def _run_view(doc: dict[str, Any], now: datetime) -> dict[str, Any]:
    view = {"test_run_id": doc["_id"], **{f: doc.get(f) for f in RUN_FIELDS}}
    last_activity = max((t for t in (doc.get("last_request_at"), doc.get("started_at")) if t), default=None)
    if view["status"] == "running" and last_activity and now - last_activity > STALE_AFTER:
        view["status"] = "stale"
    view["duration_seconds"] = (doc.get("config") or {}).get("run", {}).get("duration_seconds")
    summary = doc.get("summary")
    view["qoe_score"] = summary["qoe"]["qoe_score"] if summary else None
    return view


@app.get("/api/health")
async def health(request: Request) -> dict[str, str]:
    await request.app.state.db.command("ping")
    return {"status": "ok"}


@app.get("/api/devices")
async def list_devices(request: Request) -> list[dict[str, Any]]:
    """Every device that has reported a run, with its latest train/journey and run count."""
    pipeline = [
        {"$match": {"metadata.device_id": {"$exists": True}}},
        {"$sort": {"started_at": -1}},
        {
            "$group": {
                "_id": "$metadata.device_id",
                "train_id": {"$first": "$metadata.train_id"},
                "journey_id": {"$first": "$metadata.journey_id"},
                "last_run_at": {"$first": "$started_at"},
                "runs": {"$sum": 1},
            }
        },
        {"$sort": {"train_id": 1, "_id": 1}},
    ]
    docs = await (await request.app.state.db.runs.aggregate(pipeline)).to_list()
    return [{"device_id": d.pop("_id"), **d} for d in docs]


@app.get("/api/runs")
async def list_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    device_id: str | None = None,
    train_id: str | None = None,
) -> list[dict[str, Any]]:
    db = request.app.state.db
    now = datetime.now(UTC)
    query: dict[str, Any] = {}
    if device_id:
        query["metadata.device_id"] = device_id
    if train_id:
        query["metadata.train_id"] = train_id
    cursor = db.runs.find(query, {"summary.qoe": 1, "config.run": 1, **{f: 1 for f in RUN_FIELDS}})
    docs = await cursor.sort([("started_at", -1), ("last_request_at", -1)]).limit(limit).to_list()
    return [_run_view(doc, now) for doc in docs]


@app.get("/api/runs/{run_id}/live")
async def run_live(request: Request, run_id: str) -> dict[str, Any]:
    """Everything one dashboard poll needs for a run: status, stats, QoE, time series, recent requests."""
    db = request.app.state.db
    now = datetime.now(UTC)
    doc = await db.runs.find_one({"_id": run_id})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    run = _run_view(doc, now)

    started = doc.get("started_at") or doc.get("last_request_at") or now
    if doc.get("ended_at"):
        ended = doc["ended_at"]
    elif run["status"] == "running":
        ended = now
    else:
        ended = doc.get("last_request_at") or now
    elapsed_s = max((ended - started).total_seconds(), 0.0)

    summary = doc.get("summary")
    if summary is not None:
        source = "summary"
        stats = {k: summary[k] for k in ("requests", "latency_ms", "bytes_received_total", "throughput_mbps")}
        qoe = summary["qoe"]
    else:
        source = "live"
        groups = await (await db.requests.aggregate(stats_pipeline(run_id))).to_list()
        stats = stats_from_group(groups[0] if groups else None, elapsed_s)
        qoe = qoe_from_stats(stats)

    bucket_s = bucket_seconds(elapsed_s)
    buckets = await (await db.requests.aggregate(series_pipeline(run_id, bucket_s))).to_list()
    if run["status"] == "running":
        # The newest bucket is still filling; plotting it would show a false drop at the right edge.
        buckets = [b for b in buckets if b["_id"] + timedelta(seconds=bucket_s) <= now]

    recent_cursor = db.requests.find({"test_run_id": run_id}, {f: 1 for f in RECENT_FIELDS})
    recent = await recent_cursor.sort("timestamp", -1).limit(RECENT_LIMIT).to_list()
    for item in recent:
        item.pop("_id", None)

    return {
        "run": run,
        "elapsed_seconds": round(elapsed_s, 1),
        "source": source,
        "stats": stats,
        "qoe": qoe,
        "bucket_seconds": bucket_s,
        "series": series_from_buckets(buckets, bucket_s),
        "recent": recent,
    }


if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
