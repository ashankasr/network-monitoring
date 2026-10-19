import pytest

from app.traffic_generator.pacing import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_request_rate_is_spaced_without_bursts() -> None:
    clock = FakeClock()
    limiter = RateLimiter(max_requests_per_second=4, max_mbps=1000, clock=clock)
    starts = [limiter.reserve() for _ in range(5)]
    assert starts == pytest.approx([100.0, 100.25, 100.5, 100.75, 101.0])


def test_idle_time_does_not_accumulate_a_burst() -> None:
    clock = FakeClock()
    limiter = RateLimiter(max_requests_per_second=1, max_mbps=1000, clock=clock)
    limiter.reserve()
    clock.now += 60
    assert limiter.reserve() == 160.0
    assert limiter.reserve() == 161.0


def test_bandwidth_budget_delays_next_request() -> None:
    clock = FakeClock()
    limiter = RateLimiter(max_requests_per_second=100, max_mbps=8, clock=clock)  # 1 MB/s
    assert limiter.reserve(500_000) == 100.0
    # 0.5 MB at 1 MB/s occupies 0.5 s of the budget, longer than the 0.01 s request interval.
    assert limiter.reserve(0) == pytest.approx(100.5)
    assert limiter.reserve(0) == pytest.approx(100.51)
