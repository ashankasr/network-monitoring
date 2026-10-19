"""Per-request measurement record, written one per line to the JSONL output."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Outcome = Literal["success", "failure", "cancelled"]


class RequestRecord(BaseModel):
    timestamp: datetime  # wall clock (UTC) when the request started
    test_run_id: str
    device_id: str
    train_id: str
    journey_id: str
    profile: str
    request_id: str
    url: str
    http_method: str
    # "cancelled" means the run was interrupted mid-request; such requests are
    # excluded from success/failure statistics (D-13).
    outcome: Outcome
    success: bool
    status_code: int | None = None
    expected_bytes: int | None = None
    bytes_received: int = 0
    # Request start -> response headers received.
    time_to_first_byte_ms: float | None = None
    # Server processing time reported via Server-Timing (includes injected delay).
    server_time_ms: float | None = None
    # Network share of TTFB: TTFB minus server time (D-01).
    latency_ms: float | None = None
    # Request start -> last body byte received.
    total_duration_ms: float | None = None
    # Response headers -> last body byte received.
    transfer_duration_ms: float | None = None
    # Body bits / transfer duration; only for bodies large enough to be meaningful (D-02).
    throughput_mbps: float | None = None
    # Whether this request opened a new TCP connection, and how long that took (D-06).
    new_connection: bool | None = None
    connect_ms: float | None = None
    error: str | None = None
