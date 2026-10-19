"""Publishes run and request events to RabbitMQ without slowing measurement (D-23)."""

import asyncio
import logging

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractRobustConnection

from app.events import Envelope, EventType, declare_topology

log = logging.getLogger(__name__)

RETRY_DELAY_S = 1.0


class PublishError(Exception):
    """The broker could not be reached when the run started."""


class EventPublisher:
    """Buffers events in memory and sends them from a background task.

    `publish()` never blocks and never raises, so a slow or unavailable broker
    cannot affect timings. While the broker is unreachable, events wait in the
    buffer and sending is retried (the connection reconnects itself). When the
    buffer is full, new events are dropped and counted. The JSONL file remains
    the complete record.
    """

    def __init__(self, url: str, buffer_size: int) -> None:
        self._url = url
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=buffer_size)
        self._connection: AbstractRobustConnection | None = None
        self._exchange: AbstractExchange | None = None
        self._sender: asyncio.Task[None] | None = None
        self.enqueued = 0
        self.published = 0
        self.dropped = 0

    async def start(self, timeout_s: float = 5.0) -> None:
        try:
            self._connection = await aio_pika.connect_robust(self._url, timeout=timeout_s)
            # Publisher confirms (the default) mean `published` counts messages the broker accepted.
            channel = await self._connection.channel()
            self._exchange, _ = await declare_topology(channel)
        except Exception as exc:
            if self._connection is not None:
                await self._connection.close()
            raise PublishError(f"cannot connect to RabbitMQ: {exc!r}") from exc
        self._sender = asyncio.create_task(self._send_forever(), name="event-publisher")

    def publish(self, event_type: EventType, data: dict) -> None:
        body = Envelope(type=event_type, data=data).model_dump_json().encode()
        try:
            self._queue.put_nowait((event_type, body))
        except asyncio.QueueFull:
            self.dropped += 1
            return
        self.enqueued += 1

    @property
    def undelivered(self) -> int:
        return self.enqueued - self.published

    async def _send_forever(self) -> None:
        assert self._exchange is not None
        while True:
            routing_key, body = await self._queue.get()
            message = aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            while True:
                try:
                    await self._exchange.publish(message, routing_key=routing_key)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("publish failed (%r); retrying in %.0fs", exc, RETRY_DELAY_S)
                    await asyncio.sleep(RETRY_DELAY_S)
            self.published += 1
            self._queue.task_done()

    async def close(self, drain_timeout_s: float = 5.0) -> None:
        """Wait up to `drain_timeout_s` for buffered events to be sent, then disconnect."""
        if self._sender is not None:
            try:
                await asyncio.wait_for(self._queue.join(), drain_timeout_s)
            except TimeoutError:
                log.warning("gave up on %d buffered events after %.0fs", self.undelivered, drain_timeout_s)
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        if self._connection is not None:
            await self._connection.close()
