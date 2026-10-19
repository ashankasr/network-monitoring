"""Traffic profiles: each decides what a simulated user requests next."""

import random
from dataclasses import dataclass, field

from app.traffic_generator.config import ProfileConfig, WebBrowsingProfileConfig


@dataclass(frozen=True)
class PlannedRequest:
    path: str
    params: dict[str, int] = field(default_factory=dict)
    # Body size the endpoint should send, or None if not known in advance.
    expected_bytes: int | None = None
    # Pause after this request completes, before the user plans the next one.
    think_time_s: float = 0.0


class WebBrowsingProfile:
    """A user loading small/medium pages with randomised think time between them."""

    name = "web_browsing"

    def __init__(self, config: WebBrowsingProfileConfig, rng: random.Random) -> None:
        self._config = config
        self._rng = rng

    def next_request(self) -> PlannedRequest:
        cfg = self._config
        think_time_s = self._rng.uniform(cfg.think_time_min_ms, cfg.think_time_max_ms) / 1000
        delay_ms = self._rng.randint(cfg.server_delay_min_ms, cfg.server_delay_max_ms)
        if self._rng.random() < cfg.error_rate:
            return PlannedRequest(
                path="/failure",
                params={"status_code": 500, "delay_ms": delay_ms},
                think_time_s=think_time_s,
            )
        size_kb = self._rng.randint(cfg.payload_min_kb, cfg.payload_max_kb)
        return PlannedRequest(
            path="/page",
            params={"size_kb": size_kb, "delay_ms": delay_ms},
            expected_bytes=size_kb * 1024,
            think_time_s=think_time_s,
        )


Profile = WebBrowsingProfile


def create_profile(config: ProfileConfig, rng: random.Random) -> Profile:
    match config:
        case WebBrowsingProfileConfig():
            return WebBrowsingProfile(config, rng)
    raise ValueError(f"unsupported profile type: {config.type}")
