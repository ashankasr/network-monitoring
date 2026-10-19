import asyncio
from pathlib import Path

import pytest

from app.events import REQUEST_MEASURED, Envelope
from app.traffic_generator import publisher as publisher_module
from app.traffic_generator.outbox import Outbox
from app.traffic_generator.publisher import EventPublisher


def test_outbox_keeps_events_across_reopen(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
    ids = [outbox.add("request.measured", f"body{i}".encode()) for i in range(3)]
    outbox.delete([ids[0], 999])  # an unknown id is ignored
    outbox.close()

    reopened = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
    assert (reopened.count, reopened.bytes) == (2, 10)
    assert reopened.oldest(10) == [(ids[1], "request.measured", b"body1"), (ids[2], "request.measured", b"body2")]
    reopened.delete(ids[1:])
    assert (reopened.count, reopened.bytes) == (0, 0)
    reopened.close()


def test_outbox_cap_drops_oldest_events(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.db", max_bytes=1000)
    ids = [outbox.add("request.measured", b"x" * 100) for _ in range(15)]
    assert outbox.bytes <= 1000
    assert outbox.dropped == 5
    assert [row_id for row_id, _, _ in outbox.oldest(100)] == ids[5:]
    outbox.close()


class FakeExchange:
    """Records published bodies; fails the first `failures` publish calls."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[bytes] = []

    async def publish(self, message, routing_key: str) -> None:
        await asyncio.sleep(0)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker down")
        self.sent.append(message.body)


def connected(publisher: EventPublisher, exchange: FakeExchange) -> EventPublisher:
    """Start the sender against a fake exchange, as if connected."""
    publisher._exchange = exchange
    publisher._sender = asyncio.create_task(publisher._send_forever())
    return publisher


async def settle(condition, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher_module, "RETRY_DELAY_S", 0.001)
    monkeypatch.setattr(publisher_module, "BACKLOG_POLL_S", 0.01)


def data(i: int) -> dict:
    return {"i": i}


def sent_values(exchange: FakeExchange) -> list[int]:
    return [Envelope.model_validate_json(body).data["i"] for body in exchange.sent]


def test_full_buffer_spills_to_the_outbox_instead_of_dropping(tmp_path: Path) -> None:
    async def go() -> None:
        outbox = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
        publisher = EventPublisher("amqp://unused", buffer_size=2, outbox=outbox)  # never connected
        for i in range(5):
            publisher.publish(REQUEST_MEASURED, data(i))
        assert (publisher.enqueued, publisher.dropped, publisher.pending) == (5, 0, 5)

        exchange = FakeExchange()
        connected(publisher, exchange)
        await settle(lambda: outbox.count == 0)  # buffered first, then the spilled rest as backlog
        assert sorted(sent_values(exchange)) == [0, 1, 2, 3, 4]
        await publisher.close()
        outbox.close()

    asyncio.run(go())


def test_an_outbox_error_never_reaches_measurement(tmp_path: Path) -> None:
    async def go() -> None:
        outbox = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
        exchange = FakeExchange()
        publisher = connected(EventPublisher("amqp://unused", buffer_size=100, outbox=outbox), exchange)
        outbox.close()  # every insert now raises, as on a full disk
        publisher.publish(REQUEST_MEASURED, data(1))  # must not raise
        await settle(lambda: exchange.sent)  # still sent from memory
        assert sent_values(exchange) == [1]
        await publisher.close()

    asyncio.run(go())


def test_confirmed_events_leave_the_outbox_after_retries(tmp_path: Path) -> None:
    async def go() -> None:
        outbox = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
        exchange = FakeExchange(failures=3)
        publisher = connected(EventPublisher("amqp://unused", buffer_size=100, outbox=outbox), exchange)
        for i in range(3):
            publisher.publish(REQUEST_MEASURED, data(i))
        await settle(lambda: outbox.count == 0)
        assert sent_values(exchange) == [0, 1, 2]
        assert publisher.published == 3
        await publisher.close()
        outbox.close()

    asyncio.run(go())


def test_backlog_from_a_previous_process_waits_while_measuring(tmp_path: Path) -> None:
    async def go() -> None:
        # A previous process stored two events and stopped before they were confirmed.
        previous = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
        for i in (100, 101):
            previous.add(REQUEST_MEASURED, Envelope(type=REQUEST_MEASURED, data=data(i)).model_dump_json().encode())
        previous.close()

        outbox = Outbox(tmp_path / "outbox.db", max_bytes=1_000_000)
        exchange = FakeExchange()
        publisher = EventPublisher("amqp://unused", buffer_size=100, outbox=outbox)
        publisher.backlog_allowed.clear()  # a run is measuring
        connected(publisher, exchange)
        publisher.publish(REQUEST_MEASURED, data(1))  # a live event still goes out
        await settle(lambda: exchange.sent)
        await asyncio.sleep(0.05)
        assert sent_values(exchange) == [1]
        assert outbox.count == 2

        publisher.backlog_allowed.set()  # the run ended
        await settle(lambda: outbox.count == 0)
        assert sent_values(exchange) == [1, 100, 101]
        await publisher.close()
        outbox.close()

    asyncio.run(go())
