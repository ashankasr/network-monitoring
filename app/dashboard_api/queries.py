"""MongoDB aggregations behind the live dashboard (D-25).

Live figures are computed in MongoDB so a poll does not load every request.
Percentiles use MongoDB's approximate `$percentile`, so they can differ
slightly from the generator's exact summary. Once a run's summary arrives, the
API serves the summary's figures instead.
"""

import math
from datetime import datetime, timedelta
from typing import Any

from app.traffic_generator import qoe

# Expressions shared by both pipelines.
_SUCCESS_LATENCY = {"$cond": ["$success", "$latency_ms", None]}
_THROUGHPUT_ELIGIBLE = {"$and": ["$success", {"$ne": ["$throughput_mbps", None]}]}
_SUCCESS_COUNT = {"$sum": {"$cond": ["$success", 1, 0]}}
_THROUGHPUT_BYTES = {"$sum": {"$cond": [_THROUGHPUT_ELIGIBLE, "$bytes_received", 0]}}
_THROUGHPUT_MS = {"$sum": {"$cond": [_THROUGHPUT_ELIGIBLE, "$transfer_duration_ms", 0]}}

TARGET_BUCKETS = 120
MAX_BUCKET_S = 60


def stats_pipeline(run_id: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"test_run_id": run_id}},
        {
            "$group": {
                "_id": None,
                "all": {"$sum": 1},
                "cancelled": {"$sum": {"$cond": [{"$eq": ["$outcome", "cancelled"]}, 1, 0]}},
                "successful": _SUCCESS_COUNT,
                "bytes": {"$sum": "$bytes_received"},
                "lat_avg": {"$avg": _SUCCESS_LATENCY},
                "lat_min": {"$min": _SUCCESS_LATENCY},
                "lat_max": {"$max": _SUCCESS_LATENCY},
                "lat_pct": {
                    "$percentile": {"input": _SUCCESS_LATENCY, "p": [0.5, 0.95, 0.99], "method": "approximate"}
                },
                "thr_bytes": _THROUGHPUT_BYTES,
                "thr_ms": _THROUGHPUT_MS,
                "thr_samples": {"$sum": {"$cond": [_THROUGHPUT_ELIGIBLE, 1, 0]}},
            }
        },
    ]


def series_pipeline(run_id: str, bucket_s: int) -> list[dict[str, Any]]:
    """Per-bucket counts, latency percentiles and throughput, bucketed by request start time."""
    return [
        {"$match": {"test_run_id": run_id, "outcome": {"$ne": "cancelled"}}},
        {
            "$group": {
                "_id": {"$dateTrunc": {"date": "$timestamp", "unit": "second", "binSize": bucket_s}},
                "requests": {"$sum": 1},
                "ok": _SUCCESS_COUNT,
                "lat_pct": {"$percentile": {"input": _SUCCESS_LATENCY, "p": [0.5, 0.95], "method": "approximate"}},
                "thr_bytes": _THROUGHPUT_BYTES,
                "thr_ms": _THROUGHPUT_MS,
            }
        },
        {"$sort": {"_id": 1}},
    ]


def bucket_seconds(span_s: float) -> int:
    """Bucket width giving at most about TARGET_BUCKETS points over the run."""
    return max(1, min(MAX_BUCKET_S, math.ceil(span_s / TARGET_BUCKETS)))


def _mbps(nbytes: float, ms: float) -> float | None:
    return round(nbytes * 8 / (ms / 1000) / 1_000_000, 3) if ms > 0 else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def stats_from_group(group: dict[str, Any] | None, elapsed_s: float) -> dict[str, Any]:
    """Shape the stats aggregation like the generator's summary (same keys)."""
    group = group or {}
    total = group.get("all", 0) - group.get("cancelled", 0)
    successful = group.get("successful", 0)
    pct = group.get("lat_pct") or [None, None, None]
    latency = None
    if group.get("lat_avg") is not None:
        latency = {
            "avg": _round(group["lat_avg"]),
            "min": _round(group["lat_min"]),
            "max": _round(group["lat_max"]),
            "p50": _round(pct[0]),
            "p95": _round(pct[1]),
            "p99": _round(pct[2]),
        }
    return {
        "requests": {
            "total": total,
            "successful": successful,
            "failed": total - successful,
            "cancelled": group.get("cancelled", 0),
            "success_pct": round(100 * successful / total, 2) if total else None,
            "achieved_rps": round(total / elapsed_s, 3) if elapsed_s > 0 else None,
        },
        "latency_ms": latency,
        "bytes_received_total": group.get("bytes", 0),
        "throughput_mbps": {
            "aggregate": _mbps(group.get("thr_bytes", 0), group.get("thr_ms", 0)),
            "samples": group.get("thr_samples", 0),
        },
    }


def qoe_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Same scoring function as the generator, so live and final scores use one model."""
    latency = stats["latency_ms"]
    return qoe.score(
        p95_latency_ms=latency["p95"] if latency else None,
        success_pct=stats["requests"]["success_pct"],
        throughput_mbps=stats["throughput_mbps"]["aggregate"],
    )


def series_from_buckets(buckets: list[dict[str, Any]], bucket_s: int) -> list[dict[str, Any]]:
    """Fill empty buckets so rates drop to zero rather than being interpolated across gaps."""
    if not buckets:
        return []
    by_time = {b["_id"]: b for b in buckets}
    step = timedelta(seconds=bucket_s)
    points = []
    t: datetime = buckets[0]["_id"]
    while t <= buckets[-1]["_id"]:
        b = by_time.get(t)
        if b is None:
            points.append({"t": t, "ok": 0, "failed": 0, "rps": 0.0,
                           "latency_p50": None, "latency_p95": None, "throughput_mbps": None})
        else:
            pct = b.get("lat_pct") or [None, None]
            points.append({
                "t": t,
                "ok": b["ok"],
                "failed": b["requests"] - b["ok"],
                "rps": round(b["requests"] / bucket_s, 3),
                "latency_p50": _round(pct[0]),
                "latency_p95": _round(pct[1]),
                "throughput_mbps": _mbps(b["thr_bytes"], b["thr_ms"]),
            })
        t += step
    return points
