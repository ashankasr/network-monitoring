"""Run configuration, loaded from YAML and validated with Pydantic.

Every load-related setting has a hard ceiling: there is no unbounded mode.
Values are layered: YAML file < environment variables (D-28) < CLI flags.
"""

import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import settings
from app.limits import MAX_DELAY_MS, MAX_PAYLOAD_KB

# A scheduled runner may keep the network busy for at most this share of each interval (D-27).
MAX_DUTY_CYCLE = 0.5


class StrictModel(BaseModel):
    # Unknown keys are errors, so a typo cannot silently fall back to a default.
    model_config = ConfigDict(extra="forbid")


class TargetConfig(StrictModel):
    base_url: str

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: str) -> str:
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ValueError(f"invalid URL: {exc}") from exc
        if url.scheme not in ("http", "https"):
            raise ValueError("base_url must use http or https")
        if not url.host:
            raise ValueError("base_url must include a host")
        if url.query or url.fragment:
            raise ValueError("base_url must not include a query or fragment")
        return value.rstrip("/")


class RunConfig(StrictModel):
    duration_seconds: float = Field(30, gt=0, le=3600)
    concurrent_users: int = Field(2, ge=1, le=50)
    request_timeout_seconds: float = Field(10, gt=0, le=60)
    # Global limits shared by all users (D-10).
    max_requests_per_second: float = Field(5, gt=0, le=100)
    max_mbps: float = Field(20, gt=0, le=1000)
    # Optional seed so the sequence of planned requests is reproducible.
    seed: int | None = None


class WebBrowsingProfileConfig(StrictModel):
    type: Literal["web_browsing"] = "web_browsing"
    think_time_min_ms: int = Field(500, ge=0, le=60_000)
    think_time_max_ms: int = Field(3000, ge=0, le=60_000)
    payload_min_kb: int = Field(50, ge=1, le=MAX_PAYLOAD_KB)
    payload_max_kb: int = Field(500, ge=1, le=MAX_PAYLOAD_KB)
    server_delay_min_ms: int = Field(0, ge=0, le=MAX_DELAY_MS)
    server_delay_max_ms: int = Field(0, ge=0, le=MAX_DELAY_MS)
    # Fraction of requests sent to /failure, to exercise failure accounting (D-12).
    error_rate: float = Field(0.0, ge=0, le=1)

    @model_validator(mode="after")
    def check_ranges(self) -> Self:
        for name in ("think_time", "payload", "server_delay"):
            unit = "kb" if name == "payload" else "ms"
            low = getattr(self, f"{name}_min_{unit}")
            high = getattr(self, f"{name}_max_{unit}")
            if low > high:
                raise ValueError(f"{name}_min_{unit} ({low}) is greater than {name}_max_{unit} ({high})")
        return self


# Further profiles (download, streaming) join this as a discriminated union on `type`.
ProfileConfig = WebBrowsingProfileConfig


class MetadataConfig(StrictModel):
    # Identifies the router/device running the generator. Defaults to the hostname, which in
    # Docker is the container id, so replicas started with --scale never share an id (D-28).
    device_id: str = Field(default_factory=socket.gethostname, min_length=1)
    train_id: str = Field("local-train", min_length=1)
    journey_id: str = Field("local-journey", min_length=1)


class PublishConfig(StrictModel):
    """Optional live publishing of events to RabbitMQ (D-23)."""

    rabbitmq_url: str = settings.RABBITMQ_URL
    # Events held in memory while the broker is slow or unreachable. Beyond this, a scheduled runner
    # keeps them in its outbox only; a single run drops them.
    buffer_size: int = Field(10_000, ge=100, le=1_000_000)
    # Scheduled runners: disk cap for events not yet confirmed by the broker (D-32).
    max_outbox_mb: int = Field(200, ge=1, le=10_000)

    @field_validator("rabbitmq_url")
    @classmethod
    def check_rabbitmq_url(cls, value: str) -> str:
        if not value.startswith(("amqp://", "amqps://")):
            raise ValueError("rabbitmq_url must start with amqp:// or amqps://")
        return value


class ScheduleConfig(StrictModel):
    """Repeat the run at a fixed cadence until stopped (D-27). Each run stays finite."""

    interval_seconds: float = Field(900, ge=60, le=86_400)
    # Each start is delayed by a random 0..jitter seconds, so a fleet does not test in lockstep.
    jitter_seconds: float = Field(30, ge=0, le=3600)
    # Stop after this many runs; null runs until the process is stopped.
    max_runs: int | None = Field(None, ge=1)
    # Result files kept on disk; older runs are deleted (MongoDB keeps the long-term history).
    # Both limits apply; whichever is reached first wins (D-33).
    keep_output_runs: int = Field(96, ge=1, le=10_000)
    max_output_mb: int = Field(50, ge=1, le=10_000)


class Config(StrictModel):
    target: TargetConfig
    run: RunConfig = RunConfig()
    profile: ProfileConfig = WebBrowsingProfileConfig()
    metadata: MetadataConfig = MetadataConfig()
    # Absent or null: results go to files only.
    publish: PublishConfig | None = None
    # Absent or null: a single run.
    schedule: ScheduleConfig | None = None

    @model_validator(mode="after")
    def check_schedule(self) -> Self:
        if self.schedule is None:
            return self
        run, schedule = self.run, self.schedule
        # Worst case a run ends at jitter + duration + one request timeout; it must finish before the next slot.
        longest = schedule.jitter_seconds + run.duration_seconds + run.request_timeout_seconds
        if longest > schedule.interval_seconds:
            raise ValueError(
                f"schedule.interval_seconds ({schedule.interval_seconds:g}) must be at least "
                f"jitter + run duration + request timeout ({longest:g}) so runs never overlap"
            )
        if run.duration_seconds > MAX_DUTY_CYCLE * schedule.interval_seconds:
            raise ValueError(
                f"run.duration_seconds ({run.duration_seconds:g}) may be at most "
                f"{MAX_DUTY_CYCLE:.0%} of schedule.interval_seconds ({schedule.interval_seconds:g})"
            )
        return self


# Environment variables that override config values (D-28): name -> (section, field, enables section).
# "Enables section" means setting the variable switches that section on even if the YAML has none;
# publishing is never switched on implicitly, because QOE_RABBITMQ_URL is also set for other services.
ENV_OVERRIDES: dict[str, tuple[str, str, bool]] = {
    "QOE_TARGET_URL": ("target", "base_url", True),
    "QOE_DEVICE_ID": ("metadata", "device_id", True),
    "QOE_TRAIN_ID": ("metadata", "train_id", True),
    "QOE_JOURNEY_ID": ("metadata", "journey_id", True),
    "QOE_RUN_DURATION_SECONDS": ("run", "duration_seconds", True),
    "QOE_CONCURRENT_USERS": ("run", "concurrent_users", True),
    "QOE_MAX_REQUESTS_PER_SECOND": ("run", "max_requests_per_second", True),
    "QOE_MAX_MBPS": ("run", "max_mbps", True),
    "QOE_SCHEDULE_INTERVAL_SECONDS": ("schedule", "interval_seconds", True),
    "QOE_SCHEDULE_JITTER_SECONDS": ("schedule", "jitter_seconds", True),
    "QOE_MAX_OUTPUT_MB": ("schedule", "max_output_mb", False),
    "QOE_RABBITMQ_URL": ("publish", "rabbitmq_url", False),
    "QOE_MAX_OUTBOX_MB": ("publish", "max_outbox_mb", False),
}


def _set(data: dict[str, Any], section: str, field: str, value: Any, enables: bool) -> None:
    current = data.get(section)
    if current is None and not enables:
        return
    merged = dict(current or {})
    merged[field] = value
    data[section] = merged


def load_config(
    path: Path,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    environ: Mapping[str, str] = os.environ,
) -> Config:
    """Load a YAML config, apply environment then CLI overrides ({section: {field: value}}), and validate."""
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    for name, (section, field, enables) in ENV_OVERRIDES.items():
        value = environ.get(name, "").strip()
        if value:
            _set(data, section, field, value, enables)
    for section, fields in (overrides or {}).items():
        for field, value in fields.items():
            _set(data, section, field, value, enables=True)
    return Config.model_validate(data)
