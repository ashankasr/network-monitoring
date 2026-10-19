"""Publishes run and request events to RabbitMQ without slowing measurement (D-23, D-32)."""

import asyncio
import logging
import sqlite3

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractRobustConnection

from app.events import Envelope, EventType, declare_topology
from app.traffic_generator.outbox import Outbox

log = logging.getLogger(__name__)

RETRY_DELAY_S = 1.0
# Events sent concurrently and confirmed together. Pipelining matters on high-RTT train links.
SEND_BATCH = 100
# How often an idle sender checks for backlog it may send.
BACKLOG_POLL_S = 1.0
# Connection attempts and failed sends back off up to this delay.
MAX_RETRY_DELAY_S = 60.0

# (outbox row id or None, routing key, body)
Item = tuple[int | None, str, bytes]


class PublishError(Exception):
    """The broker could not be reached when the run started."""


class EventPublisher:
    """Buffers events in memory and sends them from a background task.

    `publish()` never blocks and never raises, so a slow or unavailable broker
    cannot affect timings. While the broker is unreachable, sending is retried
    (the connection reconnects itself).

    Without an outbox, a full buffer drops new events and counts them. With an
    outbox (scheduled runners, D-32), every event is stored before it is
    buffered and deleted once the broker confirms it, so a full buffer, an
    outage or a restart leaves it in the outbox as backlog. Backlog is sent only
    while `backlog_allowed` is set, which the runner clears during measurement.
    """

    def __init__(self, url: str, buffer_size: int, outbox: Outbox | None = None) -> None:
        self._url = url
        self.outbox = outbox
        self._queue: asyncio.Queue[Item] = asyncio.Queue(maxsize=buffer_size)
        self._connection: AbstractRobustConnection | None = None
        self._exchange: AbstractExchange | None = None
        self._sender: asyncio.Task[None] | None = None
        self._in_flight = 0
        # Whether the outbox may hold events that are not in the memory buffer.
        self._has_backlog = outbox is not None and outbox.count > 0
        self.backlog_allowed = asyncio.Event()
        self.backlog_allowed.set()
        self.enqueued = 0
        self.published = 0
        self._dropped = 0
        self._store_failures = 0

    async def start(self, timeout_s: float = 5.0) -> None:
        """Connect now, raising PublishError if the broker is unreachable, then start sending."""
        await self._connect(timeout_s)
        self._sender = asyncio.create_task(self._send_forever(), name="event-publisher")

    def start_in_background(self, timeout_s: float = 5.0) -> None:
        """Start sending as soon as a connection succeeds, retrying indefinitely (D-32)."""
        self._sender = asyncio.create_task(self._connect_then_send(timeout_s), name="event-publisher")

    @property
    def connected(self) -> bool:
        return self._exchange is not None

    async def _connect(self, timeout_s: float) -> None:
        try:
            self._connection = await aio_pika.connect_robust(self._url, timeout=timeout_s)
            # Publisher confirms (the default) mean `published` counts messages the broker accepted.
            channel = await self._connection.channel()
            self._exchange, _ = await declare_topology(channel)
        except Exception as exc:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            raise PublishError(f"cannot connect to RabbitMQ: {exc!r}") from exc

    async def _connect_then_send(self, timeout_s: float) -> None:
        delay = RETRY_DELAY_S
        failures = 0
        while True:
            try:
                await self._connect(timeout_s)
                break
            except PublishError as exc:
                failures += 1
                if failures == 1:
                    log.warning("%s; retrying in the background, events wait in the outbox", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_S)
        if failures:
            log.warning("connected to RabbitMQ after %d failed attempts", failures)
        await self._send_forever()

    def publish(self, event_type: EventType, data: dict) -> None:
        body = Envelope(type=event_type, data=data).model_dump_json().encode()
        row_id = self._store(event_type, body)
        try:
            self._queue.put_nowait((row_id, event_type, body))
        except asyncio.QueueFull:
            if row_id is None:
                self._dropped += 1
                return
            self._has_backlog = True  # stored; it waits in the outbox
        self.enqueued += 1

    def _store(self, event_type: EventType, body: bytes) -> int | None:
        """Insert into the outbox; on a storage error (e.g. disk full) the event stays in memory only."""
        if self.outbox is None:
            return None
        try:
            row_id = self.outbox.add(event_type, body)
        except sqlite3.Error as exc:
            self._store_failures += 1
            if self._store_failures == 1:  # one line per streak of failures
                log.warning("outbox write failed (%r); events are kept in memory only until it works again", exc)
            return None
        if self._store_failures:
            log.warning("outbox writes resumed after %d failures", self._store_failures)
            self._store_failures = 0
        return row_id

    def _forget(self, row_ids: list[int]) -> None:
        if self.outbox is None or not row_ids:
            return
        try:
            self.outbox.delete(row_ids)
        except sqlite3.Error as exc:  # the events stay stored and are sent again later, harmlessly (D-24)
            log.warning("outbox delete failed (%r)", exc)

    @property
    def dropped(self) -> int:
        """Events lost: buffer overflow without an outbox, or the outbox cap."""
        return self._dropped + (self.outbox.dropped if self.outbox is not None else 0)

    @property
    def pending(self) -> int:
        """Events not yet confirmed by the broker, including backlog from earlier processes."""
        if self.outbox is not None:
            return self.outbox.count
        return self._queue.qsize() + self._in_flight

    async def _send_forever(self) -> None:
        while True:
            batch, from_memory = await self._next_batch()
            await self._send(batch)
            if from_memory:
                for _ in batch:
                    self._queue.task_done()

    async def _next_batch(self) -> tuple[list[Item], bool]:
        """Buffered events first; backlog only once the buffer is empty and backlog is allowed.

        Returns the batch and whether it came from the memory buffer.
        """
        while True:
            if not self._queue.empty():
                first = self._queue.get_nowait()
                break
            if self._has_backlog and self.backlog_allowed.is_set():
                # The buffer is empty, so every stored event is backlog, not also queued in memory.
                assert self.outbox is not None
                rows = self.outbox.oldest(SEND_BATCH)
                if rows:
                    return rows, False
                self._has_backlog = False
            if not self._has_backlog:
                first = await self._queue.get()
                break
            try:  # backlog is waiting for permission: wake up now and then to check
                first = await asyncio.wait_for(self._queue.get(), BACKLOG_POLL_S)
                break
            except TimeoutError:
                continue
        batch = [first] + [self._queue.get_nowait() for _ in range(min(SEND_BATCH - 1, self._queue.qsize()))]
        return batch, True

    async def _send(self, batch: list[Item]) -> None:
        """Publish a batch concurrently, retrying failed events until every one is confirmed."""
        assert self._exchange is not None
        remaining = batch
        self._in_flight = len(batch)
        delay = RETRY_DELAY_S
        failures = 0
        while remaining:
            results = await asyncio.gather(
                *(self._exchange.publish(
                    aio_pika.Message(body, content_type="application/json",
                                     delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                    routing_key=routing_key,
                ) for _, routing_key, body in remaining),
                return_exceptions=True,
            )
            confirmed = [item for item, result in zip(remaining, results) if not isinstance(result, BaseException)]
            failed = [item for item, result in zip(remaining, results) if isinstance(result, BaseException)]
            self.published += len(confirmed)
            self._in_flight -= len(confirmed)
            self._forget([row_id for row_id, _, _ in confirmed if row_id is not None])
            if failed:
                failures += 1
                if failures == 1:  # one line per outage, not one per retry
                    error = next(r for r in results if isinstance(r, BaseException))
                    log.warning("publishing failed (%r); retrying", error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_S)
            elif failures:
                log.warning("publishing resumed after %d retries", failures)
            remaining = failed

    async def close(self, drain_timeout_s: float = 5.0) -> None:
        """Wait up to `drain_timeout_s` for buffered events to be sent, then disconnect.

        Events still unsent stay in the outbox, if there is one, for the next start.
        """
        if self._sender is not None:
            if self.connected:
                try:
                    await asyncio.wait_for(self._queue.join(), drain_timeout_s)
                except TimeoutError:
                    log.warning("gave up on %d buffered events after %.0fs", self.pending, drain_timeout_s)
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        if self._connection is not None:
            await self._connection.close()
