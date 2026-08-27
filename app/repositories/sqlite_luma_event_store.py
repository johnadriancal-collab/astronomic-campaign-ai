"""
SQLite-backed LumaEventStore. Same JSON-blob convention as
SQLiteCrmContactStore -- the full LumaEvent is stored via
`model_dump_json()` so new fields never need a migration; `calendar_id` is
denormalized as its own column purely for potential future filtering, not
because anything queries it yet.
"""

from pathlib import Path

import aiosqlite

from app.models.luma import LumaEvent
from app.repositories.luma_event_store import LumaEventStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS luma_events (
    luma_event_id TEXT PRIMARY KEY,
    calendar_id TEXT,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_luma_events_calendar ON luma_events(calendar_id)
"""


class SQLiteLumaEventStore(LumaEventStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteLumaEventStore.connect() must be called before use")
        return self._conn

    async def save(self, event: LumaEvent) -> None:
        await self._connection.execute(
            """
            INSERT INTO luma_events (luma_event_id, calendar_id, updated_at, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(luma_event_id) DO UPDATE SET
                calendar_id = excluded.calendar_id,
                updated_at = excluded.updated_at,
                data = excluded.data
            """,
            (event.luma_event_id, event.calendar_id, event.updated_at.isoformat(), event.model_dump_json()),
        )
        await self._connection.commit()

    async def get(self, luma_event_id: str) -> LumaEvent | None:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_events WHERE luma_event_id = ?", (luma_event_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return LumaEvent.model_validate_json(row["data"]) if row else None

    async def list(self) -> list[LumaEvent]:
        cursor = await self._connection.execute("SELECT data FROM luma_events")
        rows = await cursor.fetchall()
        await cursor.close()
        return [LumaEvent.model_validate_json(row["data"]) for row in rows]
