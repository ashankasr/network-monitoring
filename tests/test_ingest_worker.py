import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.events import REQUEST_MEASURED, RUN_FINISHED, RUN_STARTED, Envelope
from app.ingest_worker import main as worker
from app.ingest_worker.store import (
    request_measured_updates,
    run_finished_update,
    run_started_update,
)

REQUEST = {
    "timestamp": "2026-10-17T10:00:00+00:00",
    "test_run_id": "run-1",
    "device_id": "d", "train_id": "t", "journey_id": "j",
    "profile": "web_browsing",
    "request_id": "req-1",
    "url": "http://x/page",
    "http_method": "GET",
    "outcome": "success",
    "success": True,
    "latency_ms": 1.5,
}


def test_request_upsert_is_insert_only_and_keyed_by_request_id() -> None:
    (request_filter, request_update), (run_filter, run_update) = request_measured_updates(REQUEST)
    assert request_filter == {"_id": "req-1"}
    # $setOnInsert only: a redelivered message cannot change or duplicate the stored record.
    assert set(request_update) == {"$setOnInsert"}
    assert request_update["$setOnInsert"]["timestamp"] == datetime(2026, 10, 17, 10, tzinfo=UTC)
    assert run_filter == {"_id": "run-1"}
    assert run_update["$max"] == {"last_request_at": datetime(2026, 10, 17, 10, tzinfo=UTC)}


def test_malformed_request_is_rejected() -> None:
    with pytest.raises(ValidationError):
        request_measured_updates({**REQUEST, "outcome": "maybe"})


def test_late_run_started_does_not_reopen_a_finished_run() -> None:
    _, update = run_started_update({
        "test_run_id": "run-1", "profile": "web_browsing", "target": "http://x",
        "metadata": {}, "config": {}, "started_at": "2026-10-17T10:00:00+00:00",
    })
    assert "status" not in update["$set"]
    assert update["$setOnInsert"] == {"status": "running"}


@pytest.mark.parametrize(("stopped_by", "status"), [("duration", "completed"), ("interrupt", "interrupted"), ("error", "error")])
def test_run_finished_status(stopped_by: str, status: str) -> None:
    _, update = run_finished_update(
        {"test_run_id": "run-1", "stopped_by": stopped_by, "ended_at": "2026-10-17T10:01:00+00:00"}
    )
    assert update["$set"]["status"] == status


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.message_id = "m1"
        self.result: str | None = None

    async def ack(self) -> None:
        self.result = "ack"

    async def reject(self, requeue: bool = False) -> None:
        self.result = f"reject(requeue={requeue})"

    async def nack(self, requeue: bool = True) -> None:
        self.result = f"nack(requeue={requeue})"


class FakeStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.applied: list[Envelope] = []

    async def apply(self, envelope: Envelope) -> None:
        if self.error:
            raise self.error
        self.applied.append(envelope)


def run_handle(store: FakeStore, body: bytes) -> FakeMessage:
    message = FakeMessage(body)
    asyncio.run(worker.handle(store, message, worker.Counters()))  # type: ignore[arg-type]
    return message


def test_handle_acks_only_after_storing() -> None:
    store = FakeStore()
    body = Envelope(type=RUN_STARTED, data={}).model_dump_json().encode()
    assert run_handle(store, body).result == "ack"
    assert len(store.applied) == 1


def test_handle_dead_letters_malformed_messages() -> None:
    assert run_handle(FakeStore(), b"not json").result == "reject(requeue=False)"
    body = Envelope(type=REQUEST_MEASURED, data={}).model_dump_json().encode()
    assert run_handle(FakeStore(error=KeyError("request_id")), body).result == "reject(requeue=False)"


def test_handle_requeues_on_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "DB_RETRY_DELAY_S", 0)
    body = Envelope(type=RUN_FINISHED, data={}).model_dump_json().encode()
    assert run_handle(FakeStore(error=PyMongoError("down")), body).result == "nack(requeue=True)"
