"""
SQLite-backed ClientNoteStore -- JSON blob (full ClientNote via
model_dump_json()) plus TWO denormalized, indexed columns: `client_id`
(every Client detail page's Activity tab load) and `engagement_id`
(a described, concrete access pattern: Engagement-specific notes). No
`occurred_at` column -- see ClientNoteStore's own module docstring: this
table's expected size (notes per client, not millions of rows) fits this
codebase's existing convention of sorting in Python over a store's own
list_for_x() rather than pushing ORDER BY into SQL for a table this small.

Stage 1A (2026-09-07): a brand-new table, no prior deployed shape to
accommodate.
"""

import aiosqlite

from app.models.client_crm import ClientNote
from app.repositories.client_note_store import ClientNoteNotFoundError, ClientNoteStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client_notes (
    client_note_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    engagement_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CLIENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_notes_client ON client_notes(client_id)
"""

CREATE_ENGAGEMENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_notes_engagement ON client_notes(engagement_id)
"""


def _row_to_note(row: aiosqlite.Row) -> ClientNote:
    return ClientNote.model_validate_json(row["data"])


class SQLiteClientNoteStore(ClientNoteStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CLIENT_INDEX_SQL)
        await self._conn.execute(CREATE_ENGAGEMENT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteClientNoteStore.connect() must be called before use")
        return self._conn

    async def create(self, note: ClientNote) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO client_notes "
                "(client_note_id, client_id, engagement_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    note.client_note_id,
                    note.client_id,
                    note.engagement_id,
                    note.created_at.isoformat(),
                    note.updated_at.isoformat(),
                    note.model_dump_json(),
                ),
            )

    async def get(self, client_note_id: str) -> ClientNote | None:
        cursor = await self._connection.execute(
            "SELECT * FROM client_notes WHERE client_note_id = ?", (client_note_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_note(row) if row else None

    async def save(self, note: ClientNote) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE client_notes SET client_id = ?, engagement_id = ?, updated_at = ?, data = ? "
                "WHERE client_note_id = ?",
                (
                    note.client_id,
                    note.engagement_id,
                    note.updated_at.isoformat(),
                    note.model_dump_json(),
                    note.client_note_id,
                ),
            )
        if cursor.rowcount == 0:
            raise ClientNoteNotFoundError(note.client_note_id)

    async def list_for_client(self, client_id: str) -> list[ClientNote]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_notes WHERE client_id = ? ORDER BY created_at", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_note(row) for row in rows]

    async def list_for_engagement(self, engagement_id: str) -> list[ClientNote]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_notes WHERE engagement_id = ? ORDER BY created_at", (engagement_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_note(row) for row in rows]
