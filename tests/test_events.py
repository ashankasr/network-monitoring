import asyncio

import pytest
from pydantic import ValidationError

from app.events import REQUEST_MEASURED, Envelope
from app.traffic_generator.publisher import EventPublisher


def test_envelope_round_trip() -> None:
    envelope = Envelope(type=REQUEST_MEASURED, data={"request_id": "r1"})
    parsed = Envelope.model_validate_json(envelope.model_dump_json())
    assert parsed == envelope
    assert parsed.schema_version == "qoe.v1"


def test_envelope_rejects_unknown_type_and_schema() -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate({"type": "request.deleted", "data": {}})
    with pytest.raises(ValidationError):
        Envelope.model_validate({"schema_version": "qoe.v2", "type": REQUEST_MEASURED, "data": {}})


def test_publisher_buffers_without_blocking_and_drops_when_full() -> None:
    async def go() -> EventPublisher:
        # Never started: nothing is sent, so the buffer fills and then overflows.
        publisher = EventPublisher("amqp://unused", buffer_size=3)
        for i in range(5):
            publisher.publish(REQUEST_MEASURED, {"i": i})
        return publisher

    publisher = asyncio.run(go())
    assert publisher.enqueued == 3
    assert publisher.dropped == 2
    assert publisher.undelivered == 3
