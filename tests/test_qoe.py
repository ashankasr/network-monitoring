import pytest

from app.traffic_generator import qoe


def test_linear_score_both_directions() -> None:
    assert qoe.linear_score(100, bad=1000, good=100) == 100
    assert qoe.linear_score(1000, bad=1000, good=100) == 0
    assert qoe.linear_score(550, bad=1000, good=100) == 50
    assert qoe.linear_score(90, bad=80, good=100) == 50


def test_linear_score_is_clamped() -> None:
    assert qoe.linear_score(5, bad=1000, good=100) == 100
    assert qoe.linear_score(5000, bad=1000, good=100) == 0


def test_perfect_run_scores_100() -> None:
    result = qoe.score(p95_latency_ms=50, success_pct=100, throughput_mbps=50)
    assert result["qoe_score"] == 100
    assert result["qoe_model"] == "poc-v1"
    assert result["missing"] == []


def test_weighted_combination() -> None:
    result = qoe.score(
        p95_latency_ms=qoe.LATENCY_BAD_MS,  # 0
        success_pct=90,  # 50
        throughput_mbps=qoe.THROUGHPUT_GOOD_MBPS,  # 100
    )
    assert result["components"] == {"latency": 0, "reliability": 50, "throughput": 100}
    assert result["qoe_score"] == pytest.approx(0 * 0.40 + 50 * 0.35 + 100 * 0.25)


def test_missing_component_rescales_weights() -> None:
    result = qoe.score(p95_latency_ms=qoe.LATENCY_BAD_MS, success_pct=100, throughput_mbps=None)
    assert result["missing"] == ["throughput"]
    assert result["qoe_score"] == pytest.approx(100 * 0.35 / 0.75, abs=0.1)


def test_all_failed_scores_zero() -> None:
    result = qoe.score(p95_latency_ms=None, success_pct=0, throughput_mbps=None)
    assert result["qoe_score"] == 0


def test_no_completed_requests_has_no_score() -> None:
    assert qoe.score(None, None, None)["qoe_score"] is None


def test_output_is_labelled_as_heuristic() -> None:
    assert "not a validated" in qoe.score(10, 100, 10)["disclaimer"]
