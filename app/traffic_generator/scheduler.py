"""Repeats finite runs at a fixed cadence (D-27)."""

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.traffic_generator.config import ScheduleConfig


def next_slot(first_slot: float, interval_s: float, now: float) -> float:
    """Start of the first slot strictly after the one containing `now`.

    Slots sit at first_slot + k * interval, so the cadence never drifts with run
    length. A run that overran a slot (which validation should prevent) skips
    the slots it missed rather than starting runs back to back.
    """
    return first_slot + (math.floor((now - first_slot) / interval_s) + 1) * interval_s


async def run_schedule(
    schedule: ScheduleConfig,
    run_once: Callable[[], Awaitable[bool]],
    *,
    rng: random.Random,
    log: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Call `run_once` at each slot until it returns False or `max_runs` is reached; return the run count.

    The first run starts right away (after jitter), so a freshly started runner reports immediately.
    """
    first_slot = clock()
    slot = first_slot
    runs = 0
    while schedule.max_runs is None or runs < schedule.max_runs:
        start_at = slot + rng.uniform(0, schedule.jitter_seconds)
        wait = start_at - clock()
        if wait > 0:
            log(f"next run in {wait:.0f}s")
            await sleep(wait)
        runs += 1
        if not await run_once():
            break
        slot = next_slot(first_slot, schedule.interval_seconds, clock())
    return runs


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def device_output_dir(base: Path, device_id: str) -> Path:
    """Per-device subdirectory, so runners sharing a volume never prune each other's files."""
    return base / (_SAFE.sub("_", device_id).strip("._") or "device")


def prune_outputs(directory: Path, keep: int) -> list[str]:
    """Delete result files of all but the newest `keep` runs; return the removed run ids.

    Runs are found by their requests file, which exists even when a run was
    killed before writing its summary.
    """
    requests = sorted(directory.glob("*-requests.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for path in requests[keep:]:
        run_id = path.name.removesuffix("-requests.jsonl")
        path.unlink(missing_ok=True)
        (directory / f"{run_id}-summary.json").unlink(missing_ok=True)
        removed.append(run_id)
    return removed
