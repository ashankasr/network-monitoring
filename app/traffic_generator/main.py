"""Command-line entry point.

    uv run python -m app.traffic_generator.main --config configs/example.yaml

With a `schedule` section (or QOE_SCHEDULE_INTERVAL_SECONDS, or --interval) it
repeats the run until stopped; otherwise it runs once.
"""

import argparse
import asyncio
import logging
import random
import signal
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from app.events import REQUEST_MEASURED, RUN_FINISHED, RUN_STARTED
from app.traffic_generator.config import Config, load_config
from app.traffic_generator.outbox import Outbox
from app.traffic_generator.publisher import EventPublisher, PublishError
from app.traffic_generator.reporter import (
    JsonlWriter,
    build_summary,
    format_progress,
    format_summary,
    write_summary,
)
from app.traffic_generator.runner import PreflightError, Runner, RunResult, preflight
from app.traffic_generator.scheduler import device_output_dir, prune_outputs, run_schedule

MB = 1_000_000


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send synthetic HTTP traffic to the QoE test endpoint and measure it. "
        "Only target systems and networks you are authorised to test."
    )
    parser.add_argument("--config", type=Path, required=True, help="path to the YAML config")
    parser.add_argument("--duration", type=float, help="override run.duration_seconds")
    parser.add_argument("--users", type=int, help="override run.concurrent_users")
    parser.add_argument("--interval", type=float, help="repeat every N seconds (override schedule.interval_seconds)")
    parser.add_argument("--once", action="store_true", help="run once even if the config has a schedule")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="default: output/")
    return parser.parse_args(argv)


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%SZ} {message}", flush=True)


def describe(config: Config) -> str:
    run = config.run
    return (
        f"{run.duration_seconds:g}s, {run.concurrent_users} users, {run.max_requests_per_second:g} req/s, "
        f"{run.max_mbps:g} Mbps, timeout {run.request_timeout_seconds:g}s"
    )


def run_started_event(config: Config, run_id: str) -> dict:
    return {
        "test_run_id": run_id,
        "profile": config.profile.type,
        "target": config.target.base_url,
        "metadata": config.metadata.model_dump(),
        "config": {"run": config.run.model_dump(), "profile": config.profile.model_dump()},
        "started_at": datetime.now(UTC).isoformat(),
    }


def install_sigterm_handler() -> None:
    """`docker stop` sends SIGTERM; handle it like Ctrl+C by cancelling the main task (D-29).

    Without a handler, Python running as a container's PID 1 would ignore SIGTERM
    until Docker kills it, losing the summary of the run in progress.
    """
    task = asyncio.current_task()
    if task is None:
        return
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError):
        pass  # Windows: no loop signal handlers; Ctrl+C still works.


def check_writable(directory: Path) -> None:
    """Create `directory` if needed and prove a file can be written in it; raise OSError if not."""
    directory.mkdir(parents=True, exist_ok=True)
    # A unique name: runners sharing the folder can have the same PID in their own containers.
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-"):
        pass


async def run_one(
    config: Config,
    run_id: str,
    output_dir: Path,
    publisher: EventPublisher | None,
    progress_interval_s: float,
) -> tuple[RunResult, dict]:
    """Preflight, run, and write (and publish) the results of one run."""
    await preflight(config)
    if publisher is not None:
        publisher.publish(RUN_STARTED, run_started_event(config, run_id))
    with JsonlWriter(output_dir / f"{run_id}-requests.jsonl") as writer:
        runner = Runner(
            config,
            run_id,
            writer,
            on_progress=lambda p: print(format_progress(p), flush=True),
            progress_interval_s=progress_interval_s,
            on_record=None if publisher is None
            else lambda r: publisher.publish(REQUEST_MEASURED, r.model_dump(mode="json")),
        )
        result = await runner.run()
    # Write the summary to disk before publishing, so an interruption during the final publish cannot lose it.
    summary = build_summary(config, run_id, result)
    write_summary(output_dir / f"{run_id}-summary.json", summary)
    if publisher is not None:
        publisher.publish(RUN_FINISHED, summary)
    return result, summary


def events_line(publisher: EventPublisher, final: bool) -> str:
    """Before the publisher closes, unsent events are still in flight ("pending"), not lost.

    With an outbox, unsent events are kept on disk for the next start, so they are never "undelivered".
    """
    if publisher.outbox is not None:
        unsent = "waiting in the outbox"
    else:
        unsent = "undelivered" if final else "pending"
    return (f"Events:   {publisher.published} published, {publisher.pending} {unsent}, "
            f"{publisher.dropped} dropped")


async def single(config: Config, output_dir: Path) -> tuple[RunResult, dict]:
    install_sigterm_handler()
    run_id = str(uuid4())
    print(f"Run:     {run_id}")
    print(f"Profile: {config.profile.type}")
    print(f"Target:  {config.target.base_url}")
    print(f"Limits:  {describe(config)}  (Ctrl+C to stop early)", flush=True)
    publisher = None
    if config.publish is not None:
        publisher = EventPublisher(config.publish.rabbitmq_url, config.publish.buffer_size)
        await publisher.start()
    try:
        if publisher is not None:
            print(f"Publish: {config.publish.rabbitmq_url.split('@')[-1]} (live events on)", flush=True)
        result, summary = await run_one(config, run_id, output_dir, publisher, progress_interval_s=2.0)
    finally:
        if publisher is not None:
            await publisher.close()
    print(format_summary(summary))
    if publisher is not None:
        print(events_line(publisher, final=True))
    print(f"Requests: {output_dir / f'{run_id}-requests.jsonl'}\nSummary:  {output_dir / f'{run_id}-summary.json'}")
    return result, summary


async def scheduled(config: Config, base_output_dir: Path) -> int:
    """Repeat runs until stopped. A failed preflight skips that run; the schedule continues."""
    install_sigterm_handler()
    schedule = config.schedule
    assert schedule is not None
    meta = config.metadata
    output_dir = device_output_dir(base_output_dir, meta.device_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Scheduled runner: device {meta.device_id}, train {meta.train_id}, journey {meta.journey_id}")
    log(f"Every {schedule.interval_seconds:g}s (jitter up to {schedule.jitter_seconds:g}s): "
        f"{describe(config)} -> {config.target.base_url}")

    publisher = None
    outbox = None
    if config.publish is not None:
        # Measure even while the broker is unreachable; events wait in the outbox (D-32).
        outbox = Outbox(output_dir / "outbox.db", config.publish.max_outbox_mb * MB)
        publisher = EventPublisher(config.publish.rabbitmq_url, config.publish.buffer_size, outbox)
        publisher.start_in_background()
        backlog = f", {outbox.count} events waiting from before" if outbox.count else ""
        log(f"Publishing to {config.publish.rabbitmq_url.split('@')[-1]} "
            f"(outbox {outbox.path}, cap {config.publish.max_outbox_mb} MB{backlog})")

    async def once() -> bool:
        run_id = str(uuid4())
        log(f"Run {run_id} starting")
        if publisher is not None:
            publisher.backlog_allowed.clear()  # catch-up traffic stays out of the measurement
        try:
            result, summary = await run_one(config, run_id, output_dir, publisher, progress_interval_s=15.0)
        except PreflightError as exc:
            log(f"Run skipped: {exc}")
            return True
        finally:
            if publisher is not None:
                publisher.backlog_allowed.set()
        print(format_summary(summary), flush=True)
        if publisher is not None:
            print(events_line(publisher, final=False), flush=True)
        removed = prune_outputs(output_dir, schedule.keep_output_runs, schedule.max_output_mb * MB)
        if removed:
            log(f"Removed result files of {len(removed)} old run(s)")
        return result.stopped_by != "interrupt"

    try:
        runs = await run_schedule(schedule, once, rng=random.Random(), log=log)
        log(f"Stopped after {runs} run(s)")
    except asyncio.CancelledError:
        # Stopped between runs (Ctrl+C / docker stop).
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        log("Stopped")
    finally:
        if publisher is not None:
            await publisher.close()
            print(events_line(publisher, final=True), flush=True)
        if outbox is not None:
            outbox.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # aiormq and aio_pika log every failed (re)connection attempt; over days offline that is
    # thousands of lines. The publisher logs one line per outage instead.
    logging.getLogger("aiormq.connection").setLevel(logging.CRITICAL)
    logging.getLogger("aio_pika.robust_connection").setLevel(logging.ERROR)
    args = parse_args(argv)
    overrides: dict[str, dict[str, Any]] = {}
    if args.duration is not None:
        overrides.setdefault("run", {})["duration_seconds"] = args.duration
    if args.users is not None:
        overrides.setdefault("run", {})["concurrent_users"] = args.users
    if args.interval is not None:
        overrides.setdefault("schedule", {})["interval_seconds"] = args.interval
    try:
        config = load_config(args.config, overrides)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"Config error in {args.config}:\n{exc}", file=sys.stderr)
        return 2
    if args.once:
        config = config.model_copy(update={"schedule": None})
    try:
        check_writable(args.output_dir)
    except OSError as exc:
        print(f"Output folder {args.output_dir} is not writable: {exc}\n"
              "In Docker on Linux, make the bind-mounted folder writable by the container user "
              "(uid 10001): sudo chown 10001 output", file=sys.stderr)
        return 1

    try:
        if config.schedule is not None:
            return asyncio.run(scheduled(config, args.output_dir))
        result, _ = asyncio.run(single(config, args.output_dir))
    except PreflightError as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1
    except PublishError as exc:
        print(f"Publishing failed: {exc}\nStart it with `docker compose up -d`, "
              "or remove the `publish` section to run without it.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Interrupted before the run started, or a second Ctrl+C during shutdown.
        print("Aborted.", file=sys.stderr)
        return 130

    if result.errors:
        return 1
    return 130 if result.stopped_by == "interrupt" else 0


if __name__ == "__main__":
    sys.exit(main())
