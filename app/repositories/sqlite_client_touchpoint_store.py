"""
SQLite-backed ClientTouchpointStore -- JSON blob (full ClientTouchpoint via
model_dump_json()) plus ONE denormalized, indexed column: `client_id`
(every Client detail page's Touchpoint History load). No `occurred_at`
column -- same "this table's expected size fits this codebase's existing
convention of sorting in Python over a store's own list_for_client()
rather than pushing ORDER BY into SQL" reasoning already established by
ClientNoteStore (see that module's own docstring); deterministic
newest-first ordering (occurred_at DESC, created_at DESC, touchpoint_id
DESC) is applied in Python via the shared touchpoint_sort_key -- see
client_touchpoint_store.py.

Stage 2A (2026-09-11): a brand-new table, no prior deployed shape to
accommodate.
"""

import aiosqlite

from app.models.client_crm import ClientTouchpoint
from app.repositories.client_touchpoint_store import (
    ClientTouchpointNotFoundError,
    ClientTouchpointStore,
    touchpoint_sort_key,
)
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client_touchpoints (
    touchpoint_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CLIENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_touchpoints_client ON client_touchpoints(client_id)
"""


def _row_to_touchpoint(row: aiosqlite.Row) -> ClientTouchpoint:
    return ClientTouchpoint.model_validate_json(row["data"])


class SQLiteClientTouchpointStore(ClientTouchpointStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CLIENT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteClientTouchpointStore.connect() must be called before use")
        return self._conn

    async def create(self, touchpoint: ClientTouchpoint) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO client_touchpoints (touchpoint_id, client_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    touchpoint.touchpoint_id,
                    touchpoint.client_id,
                    touchpoint.created_at.isoformat(),
                    touchpoint.updated_at.isoformat(),
                    touchpoint.model_dump_json(),
                ),
            )

    async def get(self, touchpoint_id: str) -> ClientTouchpoint | None:
        cursor = await self._connection.execute(
            "SELECT * FROM client_touchpoints WHERE touchpoint_id = ?", (touchpoint_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_touchpoint(row) if row else None

    async def save(self, touchpoint: ClientTouchpoint) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE client_touchpoints SET client_id = ?, updated_at = ?, data = ? WHERE touchpoint_id = ?",
                (
                    touchpoint.client_id,
                    touchpoint.updated_at.isoformat(),
                    touchpoint.model_dump_json(),
                    touchpoint.touchpoint_id,
                ),
            )
        if cursor.rowcount == 0:
            raise ClientTouchpointNotFoundError(touchpoint.touchpoint_id)

    async def list_for_client(self, client_id: str) -> list[ClientTouchpoint]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_touchpoints WHERE client_id = ?", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        touchpoints = [_row_to_touchpoint(row) for row in rows]
        return sorted(touchpoints, key=touchpoint_sort_key, reverse=True)
