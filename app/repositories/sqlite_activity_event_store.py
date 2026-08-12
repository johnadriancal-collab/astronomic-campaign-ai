"""
SQLite-backed ActivityEventStore -- JSON blob, same convention as
sqlite_crm_contact_list_store.py, plus `category`/`created_at` promoted to
real indexed columns since those are the two things every list query
filters/sorts by (same "denormalize what you'll actually query" precedent
as CrmContact's indexed core columns alongside its own blob).
"""

from pathlib import Path

import aiosqlite
from loguru import logger

from app.models.activity import ActivityEvent
from app.repositories.activity_event_store import ActivityEventStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS activity_events (
    event_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_activity_events_created_at ON activity_events(created_at)"
CREATE_CATEGORY_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_activity_events_category ON activity_events(category)"


class SQLiteActivityEventStore(ActivityEventStore):
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
        await self._conn.execute(CREATE_CATEGORY_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteActivityEventStore.connect() must be called before use")
        return self._conn

    async def create(self, event: ActivityEvent) -> None:
        try:
            await self._connection.execute(
                "INSERT INTO activity_events (event_id, category, created_at, data) VALUES (?, ?, ?, ?)",
                (event.event_id, event.category.value, event.created_at.isoformat(), event.model_dump_json()),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"ActivityEvent already exists: {e}") from e

    async def list(self) -> list[ActivityEvent]:
        cursor = await self._connection.execute("SELECT event_id, data FROM activity_events ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        await cursor.close()
        events: list[ActivityEvent] = []
        for row in rows:
            try:
                events.append(ActivityEvent.model_validate_json(row["data"]))
            except Exception as e:
                # One corrupted row must never break retrieval of every other event --
                # skip it and keep going, same "isolated, doesn't abort the batch"
                # instinct as CrmImportService.preview()'s per-row try/except.
                logger.error(f"Skipping unreadable activity event {row['event_id']}: {e}")
        return events
