"""MongoDB writes for each event type (D-24).

Every write is an idempotent upsert keyed by a natural id, so a message
redelivered after a crash or a nack never creates a duplicate.
- `runs`: one document per test run, `_id` = test_run_id
- `requests`: one document per request, `_id` = request_id
"""

from datetime import datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.events import REQUEST_MEASURED, RUN_FINISHED, RUN_STARTED, Envelope
from app.traffic_generator.models import RequestRecord

Update = tuple[dict[str, Any], dict[str, Any]]  # (filter, update)

RUN_STATUS_BY_STOP_REASON = {"duration": "completed", "interrupt": "interrupted", "error": "error"}


def run_started_update(data: dict[str, Any]) -> Update:
    fields = {
        "profile": data["profile"],
        "target": data["target"],
        "metadata": data["metadata"],
        "config": data["config"],
        "started_at": datetime.fromisoformat(data["started_at"]),
    }
    # Status only on insert: a late-arriving run.started must not reopen a finished run.
    return {"_id": data["test_run_id"]}, {"$set": fields, "$setOnInsert": {"status": "running"}}


def request_measured_updates(data: dict[str, Any]) -> tuple[Update, Update]:
    """The request upsert, plus a touch on its run (creating the run if run.started was lost)."""
    record = RequestRecord.model_validate(data)
    document = record.model_dump()  # datetimes stay datetimes, so Mongo stores BSON dates
    request = ({"_id": record.request_id}, {"$setOnInsert": document})
    run = (
        {"_id": record.test_run_id},
        {
            "$setOnInsert": {"status": "running", "profile": record.profile},
            "$max": {"last_request_at": record.timestamp},
        },
    )
    return request, run


def run_finished_update(summary: dict[str, Any]) -> Update:
    return (
        {"_id": summary["test_run_id"]},
        {
            "$set": {
                "status": RUN_STATUS_BY_STOP_REASON.get(summary["stopped_by"], "completed"),
                "ended_at": datetime.fromisoformat(summary["ended_at"]),
                "summary": summary,
            },
        },
    )


class Store:
    def __init__(self, db: AsyncDatabase) -> None:
        self.runs = db["runs"]
        self.requests = db["requests"]

    async def ensure_indexes(self) -> None:
        await self.requests.create_index([("test_run_id", ASCENDING), ("timestamp", ASCENDING)])
        await self.runs.create_index([("started_at", DESCENDING)])

    async def apply(self, envelope: Envelope) -> None:
        if envelope.type == RUN_STARTED:
            await self.runs.update_one(*run_started_update(envelope.data), upsert=True)
        elif envelope.type == REQUEST_MEASURED:
            request, run = request_measured_updates(envelope.data)
            await self.requests.update_one(*request, upsert=True)
            await self.runs.update_one(*run, upsert=True)
        elif envelope.type == RUN_FINISHED:
            await self.runs.update_one(*run_finished_update(envelope.data), upsert=True)
