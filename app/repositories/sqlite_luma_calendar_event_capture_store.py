"""
SQLite-backed LumaCalendarEventCaptureStore. Stage 6A Capture (2026-09-14),
temporary schema-discovery storage ONLY -- see
app/models/luma.py's LumaCalendarEventCapture and
app/repositories/luma_calendar_event_capture_store.py's own docstrings.

`event_type` is the table's PRIMARY KEY -- the at-most-one-row-per-type
bound is enforced by SQLite itself, not by application logic, so it holds
even under concurrent/near-simultaneous deliveries (two requests racing
to insert the same event_type both run `INSERT OR IGNORE` against the
same connection/file; SQLite's own locking + this PRIMARY KEY constraint
means exactly one of them can ever succeed, never both). `cursor.rowcount`
after `INSERT OR IGNORE` is 1 if this call actually inserted the row, 0 if
a row for that event_type already existed -- that's the exact, atomic
signal save_if_first_for_event_type() returns, with no separate
SELECT-then-INSERT race window.
"""

import json

import aiosqlite

from app.models.luma import LumaCalendarEventCapture
from app.repositories.luma_calendar_event_capture_store import LumaCalendarEventCaptureStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS luma_calendar_event_captures (
    event_type TEXT PRIMARY KEY,
    delivery_id TEXT,
    captured_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL
)
"""


class SQLiteLumaCalendarEventCaptureStore(LumaCalendarEventCaptureStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteLumaCalendarEventCaptureStore.connect() must be called before use")
        return self._conn

    async def save_if_first_for_event_type(self, capture: LumaCalendarEventCapture) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                """
                INSERT OR IGNORE INTO luma_calendar_event_captures (event_type, delivery_id, captured_at, raw_payload)
                VALUES (?, ?, ?, ?)
                """,
                (capture.event_type, capture.delivery_id, capture.captured_at.isoformat(), json.dumps(capture.raw_payload)),
            )
            return cursor.rowcount == 1

    async def get(self, event_type: str) -> LumaCalendarEventCapture | None:
        cursor = await self._connection.execute(
            "SELECT event_type, delivery_id, captured_at, raw_payload FROM luma_calendar_event_captures WHERE event_type = ?",
            (event_type,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_model(row) if row else None

    async def list(self) -> list[LumaCalendarEventCapture]:
        cursor = await self._connection.execute(
            "SELECT event_type, delivery_id, captured_at, raw_payload FROM luma_calendar_event_captures"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row) -> LumaCalendarEventCapture:
        return LumaCalendarEventCapture(
            event_type=row["event_type"],
            delivery_id=row["delivery_id"],
            captured_at=row["captured_at"],
            raw_payload=json.loads(row["raw_payload"]),
        )
