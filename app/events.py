"""Message contract between the traffic generator and the ingest worker (D-22).

Every message is a JSON `Envelope` on a durable topic exchange. The routing key
equals the envelope `type`.
"""

from datetime import UTC, datetime
from typing import Any, Literal

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue
from pydantic import BaseModel, Field

SCHEMA_VERSION = "qoe.v1"

EXCHANGE = "qoe.events"
INGEST_QUEUE = "qoe.ingest"
# Messages the worker cannot parse are dead-lettered here for inspection instead of being lost.
DEAD_LETTER_EXCHANGE = "qoe.dead"
DEAD_LETTER_QUEUE = "qoe.ingest.dead"

RUN_STARTED = "run.started"
REQUEST_MEASURED = "request.measured"
RUN_FINISHED = "run.finished"

EventType = Literal["run.started", "request.measured", "run.finished"]


class Envelope(BaseModel):
    schema_version: Literal["qoe.v1"] = SCHEMA_VERSION
    type: EventType
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any]


async def declare_topology(channel: AbstractChannel) -> tuple[AbstractExchange, AbstractQueue]:
    """Declare the exchange, ingest queue and dead-letter queue.

    Both the publisher and the worker call this, so whichever starts first
    creates the queue, and events published before the worker starts are kept
    rather than dropped as unroutable. Declarations are idempotent, but the
    arguments must stay identical on both sides.
    """
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dead_exchange = await channel.declare_exchange(
        DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
    )
    dead_queue = await channel.declare_queue(DEAD_LETTER_QUEUE, durable=True)
    await dead_queue.bind(dead_exchange)
    queue = await channel.declare_queue(
        INGEST_QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE},
    )
    await queue.bind(exchange, routing_key="#")
    return exchange, queue
