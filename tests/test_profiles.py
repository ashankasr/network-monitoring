import random

from app.traffic_generator.config import WebBrowsingProfileConfig
from app.traffic_generator.profiles import WebBrowsingProfile, create_profile


def test_create_web_browsing_profile() -> None:
    profile = create_profile(WebBrowsingProfileConfig(), random.Random(1))
    assert isinstance(profile, WebBrowsingProfile)
    assert profile.name == "web_browsing"


def test_requests_stay_within_configured_ranges() -> None:
    cfg = WebBrowsingProfileConfig(
        think_time_min_ms=100, think_time_max_ms=200,
        payload_min_kb=50, payload_max_kb=60,
        server_delay_min_ms=5, server_delay_max_ms=10,
    )
    profile = create_profile(cfg, random.Random(1))
    for _ in range(200):
        planned = profile.next_request()
        assert planned.path == "/page"
        assert 50 <= planned.params["size_kb"] <= 60
        assert planned.expected_bytes == planned.params["size_kb"] * 1024
        assert 5 <= planned.params["delay_ms"] <= 10
        assert 0.1 <= planned.think_time_s <= 0.2


def test_error_rate_routes_to_failure_endpoint() -> None:
    profile = create_profile(WebBrowsingProfileConfig(error_rate=1.0), random.Random(1))
    planned = profile.next_request()
    assert planned.path == "/failure"
    assert planned.expected_bytes is None


def test_same_seed_gives_same_sequence() -> None:
    cfg = WebBrowsingProfileConfig(error_rate=0.3)
    first = create_profile(cfg, random.Random(42))
    second = create_profile(cfg, random.Random(42))
    assert [first.next_request() for _ in range(20)] == [second.next_request() for _ in range(20)]
