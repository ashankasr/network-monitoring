from datetime import UTC, datetime

import pytest

from app.traffic_generator.metrics import (
    MIN_THROUGHPUT_BYTES,
    aggregate_throughput_mbps,
    percentile,
    summarize_requests,
    throughput_mbps,
)
from app.traffic_generator.models import RequestRecord


def record(outcome: str = "success", **fields) -> RequestRecord:
    base = {
        "timestamp": datetime.now(UTC),
        "test_run_id": "run",
        "device_id": "d",
        "train_id": "t",
        "journey_id": "j",
        "profile": "web_browsing",
        "request_id": "r",
        "url": "http://x/page",
        "http_method": "GET",
        "outcome": outcome,
        "success": outcome == "success",
    }
    return RequestRecord(**(base | fields))


def test_percentile_matches_linear_interpolation() -> None:
    values = [1, 2, 3, 4]
    assert percentile(values, 0) == 1
    assert percentile(values, 100) == 4
    assert percentile(values, 50) == 2.5
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)
    assert percentile([7], 99) == 7


def test_percentile_is_order_independent() -> None:
    assert percentile([5, 1, 3], 50) == 3


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1], 101)


def test_throughput_mbps() -> None:
    # 1,000,000 bytes in 1 s = 8 Mbps (decimal megabits)
    assert throughput_mbps(1_000_000, 1.0) == 8.0
    assert throughput_mbps(1_000_000, 0.5) == 16.0


def test_throughput_skipped_for_small_or_instant_transfers() -> None:
    assert throughput_mbps(MIN_THROUGHPUT_BYTES - 1, 1.0) is None
    assert throughput_mbps(1_000_000, 0.0) is None


def test_aggregate_throughput_weights_by_bytes() -> None:
    records = [
        record(bytes_received=1_000_000, transfer_duration_ms=1000, throughput_mbps=8.0),
        record(bytes_received=3_000_000, transfer_duration_ms=1000, throughput_mbps=24.0),
        record(bytes_received=1_000, transfer_duration_ms=1000, throughput_mbps=None),
    ]
    # 4 MB over 2 s = 16 Mbps (the mean of rates would also be 16 here, so check another case)
    assert aggregate_throughput_mbps(records) == 16.0
    skewed = [
        record(bytes_received=100_000, transfer_duration_ms=10, throughput_mbps=80.0),
        record(bytes_received=1_000_000, transfer_duration_ms=1000, throughput_mbps=8.0),
    ]
    # 1.1 MB over 1.01 s, not (80 + 8) / 2
    assert aggregate_throughput_mbps(skewed) == pytest.approx(8.713, abs=0.001)
    assert aggregate_throughput_mbps([]) is None


def test_summary_counts_and_excludes_cancelled_and_failed_timings() -> None:
    records = [
        record(latency_ms=10.0, bytes_received=100),
        record(latency_ms=20.0, bytes_received=100),
        record("failure", latency_ms=5000.0, bytes_received=10, error="HTTP 500"),
        record("cancelled", error="cancelled"),
    ]
    summary = summarize_requests(records, duration_s=2.0)
    assert summary["requests"] == {
        "total": 3,
        "successful": 2,
        "failed": 1,
        "cancelled": 1,
        "success_pct": 66.67,
        "achieved_rps": 1.5,
    }
    assert summary["latency_ms"]["max"] == 20.0
    assert summary["latency_ms"]["avg"] == 15.0
    assert summary["bytes_received_total"] == 210


def test_summary_of_empty_run() -> None:
    summary = summarize_requests([], duration_s=1.0)
    assert summary["requests"]["success_pct"] is None
    assert summary["latency_ms"] is None
    assert summary["throughput_mbps"]["aggregate"] is None
