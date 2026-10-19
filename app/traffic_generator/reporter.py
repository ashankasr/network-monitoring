"""Output: JSONL request records, the run summary, and console text."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from app.traffic_generator import qoe
from app.traffic_generator.metrics import summarize_requests
from app.traffic_generator.models import RequestRecord

if TYPE_CHECKING:
    from app.traffic_generator.config import Config
    from app.traffic_generator.runner import Progress, RunResult


class JsonlWriter:
    """Appends one JSON record per line, flushed immediately so an interrupted run keeps its data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("w", encoding="utf-8")

    def write(self, record: RequestRecord) -> None:
        self._file.write(record.model_dump_json() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def build_summary(config: Config, run_id: str, result: RunResult) -> dict:
    stats = summarize_requests(result.records, result.duration_s)
    latency = stats["latency_ms"]
    return {
        "test_run_id": run_id,
        "profile": config.profile.type,
        "target": config.target.base_url,
        "metadata": config.metadata.model_dump(),
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "duration_seconds": round(result.duration_s, 3),
        "stopped_by": result.stopped_by,
        "errors": result.errors,
        "config": {"run": config.run.model_dump(), "profile": config.profile.model_dump()},
        **stats,
        "qoe": qoe.score(
            p95_latency_ms=latency["p95"] if latency else None,
            success_pct=stats["requests"]["success_pct"],
            throughput_mbps=stats["throughput_mbps"]["aggregate"],
        ),
    }


def write_summary(path: Path, summary: dict) -> None:
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def _fmt(value: float | None, unit: str = "", digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def format_progress(progress: Progress) -> str:
    return (
        f"[{progress.elapsed_s:5.1f}s / {progress.duration_s:g}s] "
        f"requests {progress.requests}  ok {progress.successful}  failed {progress.failed}  "
        f"avg latency {_fmt(progress.avg_latency_ms, ' ms')}"
    )


def format_summary(summary: dict) -> str:
    requests = summary["requests"]
    latency = summary["latency_ms"] or {}
    result = summary["qoe"]
    components = "  ".join(f"{name} {_fmt(value)}" for name, value in result["components"].items())
    lines = [
        f"Run {summary['test_run_id']} finished ({summary['stopped_by']}) "
        f"after {summary['duration_seconds']:.1f}s",
        f"  Requests   {requests['total']} total, {requests['successful']} ok, "
        f"{requests['failed']} failed, {requests['cancelled']} cancelled "
        f"(success {_fmt(requests['success_pct'], '%')})",
        f"  Latency    avg {_fmt(latency.get('avg'), ' ms')}  p50 {_fmt(latency.get('p50'), ' ms')}  "
        f"p95 {_fmt(latency.get('p95'), ' ms')}  p99 {_fmt(latency.get('p99'), ' ms')}",
        f"  Received   {summary['bytes_received_total'] / 1_000_000:.2f} MB, "
        f"throughput {_fmt(summary['throughput_mbps']['aggregate'], ' Mbps')}",
        f"  QoE        {_fmt(result['qoe_score'])} ({result['qoe_model']}: {components})",
        f"             {result['disclaimer']}",
    ]
    lines.extend(f"  Error      {error}" for error in summary["errors"])
    return "\n".join(lines)
