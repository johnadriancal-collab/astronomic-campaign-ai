"""
SQLite-backed EngagementStore -- JSON blob (full Engagement via
model_dump_json()) plus ONE denormalized, indexed column: `client_id`
(every Client detail page load needs list_for_client). No `engagement_date`/`status`
columns -- see EngagementStore's own module docstring: no concrete
cross-client query (e.g. "upcoming engagements across all clients") exists
yet to justify them; a real column can be added later without a migration
step if/when such a view is actually built (CREATE TABLE IF NOT EXISTS is
trivially idempotent, matching every other store in this codebase).

Stage 1A (2026-09-07): a brand-new table, no prior deployed shape to
accommodate. Stage 1E.1 (2026-09-08) is the first time this DOES need to
accommodate a prior deployed shape -- see engagement_taxonomy_migration.py's
own module docstring for why that migration runs synchronously here, in
connect(), before this store (or the app) is usable.
"""

import aiosqlite

from app.models.client_crm import Engagement
from app.repositories.engagement_store import EngagementNotFoundError, EngagementStore
from app.repositories.engagement_taxonomy_migration import migrate_legacy_dinner_taxonomy_rows
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engagements (
    engagement_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CLIENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_engagements_client ON engagements(client_id)
"""


def _row_to_engagement(row: aiosqlite.Row) -> Engagement:
    return Engagement.model_validate_json(row["data"])


class SQLiteEngagementStore(EngagementStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CLIENT_INDEX_SQL)
        await self._conn.commit()
        # Must run BEFORE this store (or the app) serves any request --
        # see engagement_taxonomy_migration.py's own module docstring for
        # why this can't safely be a separate, later-timed step.
        await migrate_legacy_dinner_taxonomy_rows(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEngagementStore.connect() must be called before use")
        return self._conn

    async def create(self, engagement: Engagement) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO engagements (engagement_id, client_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    engagement.engagement_id,
                    engagement.client_id,
                    engagement.created_at.isoformat(),
                    engagement.updated_at.isoformat(),
                    engagement.model_dump_json(),
                ),
            )

    async def get(self, engagement_id: str) -> Engagement | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE engagement_id = ?", (engagement_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_engagement(row) if row else None

    async def save(self, engagement: Engagement) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE engagements SET client_id = ?, updated_at = ?, data = ? WHERE engagement_id = ?",
                (
                    engagement.client_id,
                    engagement.updated_at.isoformat(),
                    engagement.model_dump_json(),
                    engagement.engagement_id,
                ),
            )
        if cursor.rowcount == 0:
            raise EngagementNotFoundError(engagement.engagement_id)

    async def list_for_client(self, client_id: str) -> list[Engagement]:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE client_id = ? ORDER BY created_at", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_engagement(row) for row in rows]
