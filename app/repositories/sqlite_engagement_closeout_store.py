"""
SQLite-backed EngagementCloseoutStore -- JSON blob (full EngagementCloseout
via model_dump_json()) plus ONE denormalized, indexed column: `engagement_id`
(the only real access pattern Stage 1F needs -- load "the" Closeout for an
Engagement detail page, and the service layer's at-most-one-per-engagement
check). `client_id` is stored as plain data inside the blob only, not its
own column/index -- no concrete cross-client "all this Client's Closeouts"
query exists yet to justify one, same reasoning EngagementStore's own
module docstring already gives for not indexing engagement_date/status.

Stage 1F (2026-09-08): a brand-new table, no prior deployed shape to
accommodate.
"""

import aiosqlite

from app.models.client_crm import EngagementCloseout
from app.repositories.engagement_closeout_store import EngagementCloseoutNotFoundError, EngagementCloseoutStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engagement_closeouts (
    closeout_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_ENGAGEMENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_engagement_closeouts_engagement ON engagement_closeouts(engagement_id)
"""


def _row_to_closeout(row: aiosqlite.Row) -> EngagementCloseout:
    return EngagementCloseout.model_validate_json(row["data"])


class SQLiteEngagementCloseoutStore(EngagementCloseoutStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_ENGAGEMENT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEngagementCloseoutStore.connect() must be called before use")
        return self._conn

    async def create(self, closeout: EngagementCloseout) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO engagement_closeouts (closeout_id, engagement_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    closeout.closeout_id,
                    closeout.engagement_id,
                    closeout.created_at.isoformat(),
                    closeout.updated_at.isoformat(),
                    closeout.model_dump_json(),
                ),
            )

    async def get(self, closeout_id: str) -> EngagementCloseout | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagement_closeouts WHERE closeout_id = ?", (closeout_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_closeout(row) if row else None

    async def save(self, closeout: EngagementCloseout) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE engagement_closeouts SET engagement_id = ?, updated_at = ?, data = ? WHERE closeout_id = ?",
                (
                    closeout.engagement_id,
                    closeout.updated_at.isoformat(),
                    closeout.model_dump_json(),
                    closeout.closeout_id,
                ),
            )
        if cursor.rowcount == 0:
            raise EngagementCloseoutNotFoundError(closeout.closeout_id)

    async def get_for_engagement(self, engagement_id: str) -> EngagementCloseout | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagement_closeouts WHERE engagement_id = ? ORDER BY created_at LIMIT 1", (engagement_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_closeout(row) if row else None
