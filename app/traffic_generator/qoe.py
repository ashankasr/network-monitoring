"""POC QoE heuristic, model "poc-v1".

This is an experimental heuristic for demonstrating the concept. It is NOT a
validated QoE model and does not represent passenger satisfaction. All
thresholds below are placeholders chosen for plausibility (see
docs/DECISIONS.md, D-15). Replace this module wholesale when a better model exists.
"""

QOE_MODEL = "poc-v1"
QOE_DISCLAIMER = "POC heuristic only; not a validated QoE model."

WEIGHTS = {"latency": 0.40, "reliability": 0.35, "throughput": 0.25}

# p95 network latency (TTFB minus server time). Users perceive tail latency.
LATENCY_GOOD_MS = 100
LATENCY_BAD_MS = 1000
# Percentage of completed requests that succeeded.
RELIABILITY_GOOD_PCT = 100
RELIABILITY_BAD_PCT = 80
# Aggregate throughput of transfers large enough to measure.
THROUGHPUT_GOOD_MBPS = 10
THROUGHPUT_BAD_MBPS = 0.5


def linear_score(value: float, bad: float, good: float) -> float:
    """Map `value` linearly onto 0-100, where `bad` scores 0 and `good` scores 100, clamped.

    Works in either direction: `good` may be lower than `bad` (e.g. latency).
    """
    fraction = (value - bad) / (good - bad)
    return round(100 * min(max(fraction, 0.0), 1.0), 1)


def score(
    p95_latency_ms: float | None,
    success_pct: float | None,
    throughput_mbps: float | None,
) -> dict:
    """Combine the available component scores into one 0-100 score.

    A component without data (e.g. no transfers large enough to measure
    throughput) is left out and the remaining weights are rescaled; the output
    lists it under "missing". With no completed requests there is no score.
    """
    components: dict[str, float | None] = {
        "latency": None if p95_latency_ms is None
        else linear_score(p95_latency_ms, LATENCY_BAD_MS, LATENCY_GOOD_MS),
        "reliability": None if success_pct is None
        else linear_score(success_pct, RELIABILITY_BAD_PCT, RELIABILITY_GOOD_PCT),
        "throughput": None if throughput_mbps is None
        else linear_score(throughput_mbps, THROUGHPUT_BAD_MBPS, THROUGHPUT_GOOD_MBPS),
    }
    missing = [name for name, value in components.items() if value is None]

    qoe_score: float | None
    if components["reliability"] is None:
        qoe_score = None
    elif success_pct == 0:
        # Nothing worked; latency and throughput have no data, and the score should not imply otherwise.
        qoe_score = 0.0
    else:
        present = {name: value for name, value in components.items() if value is not None}
        total_weight = sum(WEIGHTS[name] for name in present)
        qoe_score = round(sum(value * WEIGHTS[name] for name, value in present.items()) / total_weight, 1)

    return {
        "qoe_score": qoe_score,
        "qoe_model": QOE_MODEL,
        "disclaimer": QOE_DISCLAIMER,
        "components": components,
        "missing": missing,
        "weights": WEIGHTS,
    }
