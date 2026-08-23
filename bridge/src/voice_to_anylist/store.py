"""The shadow: what both sides looked like the last time they agreed.

Without it the bridge cannot tell "this item is new" from "this item is one I
put there myself thirty seconds ago", and every sync would echo back and forth
forever.  Comparing each side against the shadow rather than against each
other is what makes the merge decidable.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow (
    key         TEXT PRIMARY KEY,
    keep_id     TEXT,
    anylist_id  TEXT,
    name        TEXT NOT NULL,
    quantity    TEXT,
    checked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class ShadowEntry:
    key: str
    keep_id: str | None
    anylist_id: str | None
    name: str
    quantity: str | None = None
    checked: bool = False


class ShadowStore:
    """SQLite-backed shadow state.

    Pass ``":memory:"`` in tests.  The connection is held open for the life of
    the store so an in-memory database survives between calls.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Each sync cycle runs on a worker thread from asyncio's pool, so the
        # connection outlives the thread that opened it and cannot be pinned to
        # it. Access is serialised by the lock below instead.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def load(self) -> dict[str, ShadowEntry]:
        with self._lock, closing(self._conn.execute("SELECT * FROM shadow")) as cursor:
            return {
                row["key"]: ShadowEntry(
                    key=row["key"],
                    keep_id=row["keep_id"],
                    anylist_id=row["anylist_id"],
                    name=row["name"],
                    quantity=row["quantity"],
                    checked=bool(row["checked"]),
                )
                for row in cursor
            }

    def replace_all(self, entries: Iterable[ShadowEntry]) -> None:
        """Swap in a whole new shadow atomically.

        A half-written shadow is worse than a stale one: it would read as
        "these items are new" on the next cycle and duplicate them.
        """
        rows = [
            (e.key, e.keep_id, e.anylist_id, e.name, e.quantity, int(e.checked))
            for e in entries
        ]
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM shadow")
            self._conn.executemany(
                "INSERT INTO shadow (key, keep_id, anylist_id, name, quantity, checked)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock, closing(
            self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        ) as cur:
            row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock, self._conn:
            if value is None:
                self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
