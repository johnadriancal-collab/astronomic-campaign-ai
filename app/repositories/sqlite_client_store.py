"""
SQLite-backed ClientStore -- same JSON-blob convention as
SQLiteCrmContactStore: the full Client is stored via `model_dump_json()`
so new fields never need a migration. No extra indexed columns beyond the
primary key -- unlike CrmContact, a Client has no external identifier to
dedupe against, and this codebase's own convention (see ClientStore's own
list() docstring) is to filter/sort a table this size in Python over a
full list() rather than add a speculative index with no concrete fast-
lookup need yet. If a future cross-client view genuinely needs
`WHERE archived=0`/`ORDER BY name` pushed into SQL, a real column can be
added at that point without a migration step (CREATE TABLE IF NOT EXISTS
is trivially idempotent, matching every other store in this codebase).

Stage 1A (2026-09-07): a brand-new table, no prior deployed shape to
accommodate.
"""

import aiosqlite

from app.models.client_crm import Client
from app.repositories.client_store import ClientNotFoundError, ClientStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


def _row_to_client(row: aiosqlite.Row) -> Client:
    return Client.model_validate_json(row["data"])


class SQLiteClientStore(ClientStore):
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
            raise RuntimeError("SQLiteClientStore.connect() must be called before use")
        return self._conn

    async def create(self, client: Client) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO clients (client_id, created_at, updated_at, data) VALUES (?, ?, ?, ?)",
                (client.client_id, client.created_at.isoformat(), client.updated_at.isoformat(), client.model_dump_json()),
            )

    async def get(self, client_id: str) -> Client | None:
        cursor = await self._connection.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_client(row) if row else None

    async def save(self, client: Client) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE clients SET updated_at = ?, data = ? WHERE client_id = ?",
                (client.updated_at.isoformat(), client.model_dump_json(), client.client_id),
            )
        if cursor.rowcount == 0:
            raise ClientNotFoundError(client.client_id)

    async def list(self) -> list[Client]:
        cursor = await self._connection.execute("SELECT * FROM clients ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_client(row) for row in rows]
