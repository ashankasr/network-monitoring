"""Global rate limiting shared by all simulated users."""

import time
from collections.abc import Callable


class RateLimiter:
    """Hands out start times so that request rate and planned bandwidth stay under their caps.

    Each reservation is placed after the previous one by at least 1/max_rps, and
    after the previous request's planned bytes would have been sent at max_mbps.
    Reservations come back in order and never burst, so the limit holds over any
    window, not just on average. Bandwidth is budgeted from the planned body size
    rather than measured, because the limiter has to decide before the request is sent.

    Not thread-safe: it is meant to be shared by tasks on one asyncio event loop,
    where reserve() runs without interruption.
    """

    def __init__(
        self,
        max_requests_per_second: float,
        max_mbps: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request_interval_s = 1 / max_requests_per_second
        self._bytes_per_s = max_mbps * 1_000_000 / 8
        self._clock = clock
        self._next_request_at = 0.0
        self._next_bytes_at = 0.0

    def reserve(self, planned_bytes: int = 0) -> float:
        """Reserve a slot and return the clock time at which the request may start."""
        start = max(self._clock(), self._next_request_at, self._next_bytes_at)
        self._next_request_at = start + self._request_interval_s
        self._next_bytes_at = start + planned_bytes / self._bytes_per_s
        return start
