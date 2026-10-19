import asyncio
import os
import random
from pathlib import Path

import pytest

from app.traffic_generator.config import ScheduleConfig
from app.traffic_generator.scheduler import device_output_dir, next_slot, prune_outputs, run_schedule


def test_next_slot_keeps_a_fixed_cadence() -> None:
    assert next_slot(0, 900, 61) == 900       # a 61 s run, next slot at 15 min
    assert next_slot(0, 900, 900) == 1800     # exactly on a slot: the following one
    assert next_slot(0, 900, 1000) == 1800    # overran slot 900: skip to 1800, don't run back to back


class FakeTime:
    """Clock and sleep that advance instantly, recording the sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def run(schedule: ScheduleConfig, run_once, fake: FakeTime) -> int:
    return asyncio.run(
        run_schedule(schedule, run_once, rng=random.Random(1), log=lambda _: None,
                     clock=fake.clock, sleep=fake.sleep)
    )


def test_runs_start_on_slots_plus_jitter() -> None:
    fake = FakeTime()
    starts: list[float] = []

    async def run_once() -> bool:
        starts.append(fake.now)
        fake.now += 61  # the run takes 61 s
        return True

    schedule = ScheduleConfig(interval_seconds=900, jitter_seconds=30, max_runs=3)
    assert run(schedule, run_once, fake) == 3
    offsets = [s - 1000 - 900 * i for i, s in enumerate(starts)]
    assert all(0 <= o <= 30 for o in offsets)  # each start within its slot's jitter window


def test_no_jitter_first_run_starts_immediately() -> None:
    fake = FakeTime()
    starts: list[float] = []

    async def run_once() -> bool:
        starts.append(fake.now)
        fake.now += 60
        return True

    run(ScheduleConfig(interval_seconds=900, jitter_seconds=0, max_runs=2), run_once, fake)
    assert starts == [1000, 1900]


def test_stops_when_a_run_is_interrupted() -> None:
    fake = FakeTime()
    calls = 0

    async def run_once() -> bool:
        nonlocal calls
        calls += 1
        return calls < 2  # second run reports an interrupt

    assert run(ScheduleConfig(interval_seconds=900, jitter_seconds=0), run_once, fake) == 2


def test_prune_keeps_newest_runs(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"run{i}-requests.jsonl").write_text("{}\n")
        if i != 1:  # run1 was killed before its summary was written
            (tmp_path / f"run{i}-summary.json").write_text("{}")
        os.utime(tmp_path / f"run{i}-requests.jsonl", (i, i))
    removed = prune_outputs(tmp_path, keep=2)
    assert sorted(removed) == ["run0", "run1", "run2"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "run3-requests.jsonl", "run3-summary.json", "run4-requests.jsonl", "run4-summary.json",
    ]


@pytest.mark.parametrize(
    ("device_id", "expected"),
    [("router-101-a", "router-101-a"), ("train 7/router:1", "train_7_router_1"), ("../..", "device")],
)
def test_device_output_dir_is_safe(tmp_path: Path, device_id: str, expected: str) -> None:
    assert device_output_dir(tmp_path, device_id) == tmp_path / expected
