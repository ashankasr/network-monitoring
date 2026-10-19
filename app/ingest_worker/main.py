"""Consumes QoE events from RabbitMQ and stores them in MongoDB.

    uv run python -m app.ingest_worker.main
"""

import asyncio
import logging
import sys
import time

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app import settings
from app.events import Envelope, declare_topology
from app.ingest_worker.store import Store

log = logging.getLogger("ingest_worker")

PREFETCH = 100
STATS_INTERVAL_S = 10.0
DB_RETRY_DELAY_S = 2.0


class Counters:
    def __init__(self) -> None:
        self.stored = 0
        self.rejected = 0
        self.retried = 0


async def handle(store: Store, message: AbstractIncomingMessage, counters: Counters) -> None:
    """Store one message, then ack it. A message is only acked once MongoDB has it (D-24).

    - Malformed message: rejected without requeue, so it goes to the dead-letter queue.
    - Database error: nacked with requeue after a pause, so it is retried.
    """
    try:
        envelope = Envelope.model_validate_json(message.body)
        await store.apply(envelope)
    except (ValidationError, ValueError, KeyError) as exc:
        log.warning("dead-lettering malformed message %s: %s", message.message_id, exc)
        counters.rejected += 1
        await message.reject(requeue=False)
        return
    except PyMongoError as exc:
        log.error("MongoDB error, requeueing message: %r", exc)
        counters.retried += 1
        await asyncio.sleep(DB_RETRY_DELAY_S)
        await message.nack(requeue=True)
        return
    counters.stored += 1
    await message.ack()


async def report_stats(counters: Counters) -> None:
    last = 0
    while True:
        await asyncio.sleep(STATS_INTERVAL_S)
        rate = (counters.stored - last) / STATS_INTERVAL_S
        last = counters.stored
        log.info(
            "stored %d (%.1f/s), dead-lettered %d, retried %d",
            counters.stored, rate, counters.rejected, counters.retried,
        )


async def run_worker() -> None:
    mongo = AsyncMongoClient(settings.MONGO_URL, tz_aware=True, serverSelectionTimeoutMS=5000)
    store = Store(mongo[settings.MONGO_DB])
    await store.ensure_indexes()
    log.info("connected to MongoDB %s, database %r", settings.MONGO_URL, settings.MONGO_DB)

    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL, timeout=10)
    counters = Counters()
    stats = asyncio.create_task(report_stats(counters))
    try:
        async with connection:
            channel = await connection.channel()
            # Bounds unacked messages held by this worker; the rest wait safely in the queue.
            await channel.set_qos(prefetch_count=PREFETCH)
            _, queue = await declare_topology(channel)
            log.info("consuming from %r on %s", queue.name, settings.RABBITMQ_URL.split("@")[-1])
            async with queue.iterator() as messages:
                async for message in messages:
                    await handle(store, message, counters)
    finally:
        stats.cancel()
        await mongo.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    started = time.monotonic()
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        # Unacked messages are redelivered by RabbitMQ; nothing is lost.
        log.info("stopped after %.0fs", time.monotonic() - started)
        return 0
    except (OSError, PyMongoError, aio_pika.exceptions.AMQPError) as exc:
        log.error("cannot start: %r (is `docker compose up -d` running?)", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
