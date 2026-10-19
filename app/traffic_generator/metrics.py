"""Statistics over measured requests."""

import math
from collections.abc import Iterable, Sequence

from app.traffic_generator.models import RequestRecord

# Below this body size, transfer time is mostly noise (one or two TCP reads), so
# per-request throughput is not reported (D-02).
MIN_THROUGHPUT_BYTES = 64 * 1024


def percentile(values: Sequence[float], pct: float) -> float:
    """Percentile with linear interpolation between closest ranks (NumPy's default method)."""
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def throughput_mbps(nbytes: int, seconds: float) -> float | None:
    """Throughput in megabits per second (10^6 bits), or None if the sample is too small to mean anything."""
    if nbytes < MIN_THROUGHPUT_BYTES or seconds <= 0:
        return None
    return round(nbytes * 8 / seconds / 1_000_000, 3)


def distribution(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "avg": round(sum(values) / len(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
    }


def aggregate_throughput_mbps(records: Iterable[RequestRecord]) -> float | None:
    """Total eligible bytes over total transfer time, rather than a mean of per-request rates.

    A mean of rates would give a 64 KB page the same weight as a 10 MB download.
    """
    total_bytes = 0
    total_s = 0.0
    for record in records:
        if record.throughput_mbps is None or record.transfer_duration_ms is None:
            continue
        total_bytes += record.bytes_received
        total_s += record.transfer_duration_ms / 1000
    if total_s <= 0:
        return None
    return round(total_bytes * 8 / total_s / 1_000_000, 3)


def summarize_requests(records: Sequence[RequestRecord], duration_s: float) -> dict:
    """Counts, latency distributions and throughput for one run.

    Cancelled requests are counted but excluded from success/failure figures.
    Timing distributions use successful requests only, because a failed
    request's timing describes the failure, not the service.
    """
    completed = [r for r in records if r.outcome != "cancelled"]
    successful = [r for r in completed if r.success]
    success_pct = round(100 * len(successful) / len(completed), 2) if completed else None
    return {
        "requests": {
            "total": len(completed),
            "successful": len(successful),
            "failed": len(completed) - len(successful),
            "cancelled": len(records) - len(completed),
            "success_pct": success_pct,
            "achieved_rps": round(len(completed) / duration_s, 3) if duration_s > 0 else None,
        },
        "latency_ms": distribution([r.latency_ms for r in successful if r.latency_ms is not None]),
        "time_to_first_byte_ms": distribution(
            [r.time_to_first_byte_ms for r in successful if r.time_to_first_byte_ms is not None]
        ),
        "total_duration_ms": distribution(
            [r.total_duration_ms for r in successful if r.total_duration_ms is not None]
        ),
        "bytes_received_total": sum(r.bytes_received for r in records),
        "throughput_mbps": {
            "aggregate": aggregate_throughput_mbps(successful),
            "samples": sum(1 for r in successful if r.throughput_mbps is not None),
        },
    }
