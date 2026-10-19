"""Durable store of events the broker has not confirmed yet (D-32).

A scheduled runner inserts every event here before it is published and deletes
it once RabbitMQ confirms it. Whatever remains after an outage, a full buffer
or a restart is the backlog. One SQLite file per device; only the runner's own
process opens it.
"""

import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# When the cap is reached, at least this share of it is freed at once (oldest events first),
# so a full outbox does not run a DELETE for every new event.
MIN_DROP_FRACTION = 0.01


class Outbox:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.dropped = 0
        self._db = sqlite3.connect(path, isolation_level=None)  # autocommit: each statement commits
        # auto_vacuum must be set before the table exists; it lets an emptied outbox shrink again.
        self._db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        # WAL with synchronous=NORMAL: no fsync per event. A power cut can lose the last
        # moments of events but cannot corrupt the file.
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " routing_key TEXT NOT NULL,"
            " body BLOB NOT NULL)"
        )
        self.count, self.bytes = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM events"
        ).fetchone()

    def add(self, routing_key: str, body: bytes) -> int:
        """Store an event and return its id, first dropping the oldest events if the cap requires it."""
        if self.bytes + len(body) > self.max_bytes:
            self._drop_oldest(self.bytes + len(body) - self.max_bytes)
        row_id = self._db.execute(
            "INSERT INTO events (routing_key, body) VALUES (?, ?)", (routing_key, body)
        ).lastrowid
        assert row_id is not None
        self.count += 1
        self.bytes += len(body)
        return row_id

    def delete(self, ids: Iterable[int]) -> None:
        """Remove confirmed events. Ids that are already gone (dropped by the cap) are ignored."""
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        sizes = self._db.execute(
            f"DELETE FROM events WHERE id IN ({placeholders}) RETURNING LENGTH(body)", ids
        ).fetchall()
        self.count -= len(sizes)
        self.bytes -= sum(size for (size,) in sizes)
        if self.count == 0:
            self._db.execute("PRAGMA incremental_vacuum")

    def oldest(self, limit: int) -> list[tuple[int, str, bytes]]:
        """The oldest stored events as (id, routing_key, body)."""
        return self._db.execute(
            "SELECT id, routing_key, body FROM events ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def _drop_oldest(self, at_least_bytes: int) -> None:
        target = max(at_least_bytes, int(self.max_bytes * MIN_DROP_FRACTION))
        # Delete the oldest events up to and including the one where their running size reaches `target`
        # (all of them if the total is smaller).
        sizes = self._db.execute(
            "DELETE FROM events WHERE id <= COALESCE("
            " (SELECT id FROM (SELECT id, SUM(LENGTH(body)) OVER (ORDER BY id) AS running FROM events)"
            "  WHERE running >= ? ORDER BY id LIMIT 1),"
            " (SELECT MAX(id) FROM events))"
            " RETURNING LENGTH(body)",
            (target,),
        ).fetchall()
        self.count -= len(sizes)
        self.bytes -= sum(size for (size,) in sizes)
        self.dropped += len(sizes)
        log.warning("outbox reached its %g MB cap: dropped the %d oldest undelivered events (%d in total)",
                    self.max_bytes / 1_000_000, len(sizes), self.dropped)

    def close(self) -> None:
        self._db.close()
