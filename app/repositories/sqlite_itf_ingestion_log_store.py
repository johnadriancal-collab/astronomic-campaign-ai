"""
SQLite-backed ItfIngestionLogStore. `row_number` is the primary key --
save() uses INSERT ... ON CONFLICT DO UPDATE, so re-logging an already-
processed row (a retry, or a hand-edited row being reprocessed) overwrites
its own prior entry rather than erroring, matching the ledger's documented
upsert contract.
"""

import aiosqlite

from app.models.itf import ItfIngestionLogEntry, ItfRowStatus
from app.repositories.itf_ingestion_log_store import ItfIngestionLogStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS itf_ingestion_log (
    row_number INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_id TEXT,
    crm_contact_id TEXT,
    email TEXT,
    error_message TEXT,
    processed_at TEXT NOT NULL
)
"""


class SQLiteItfIngestionLogStore(ItfIngestionLogStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._migrate_add_response_id_column()
        await self._conn.commit()

    async def _migrate_add_response_id_column(self) -> None:
        """Safe for a table that already existed before response_id was added
        to the schema (this webhook redesign's ledger table pre-dates it) --
        adds the column only if it's actually missing. A fresh table already
        has it from CREATE_TABLE_SQL above, so this is a no-op there."""
        cursor = await self._conn.execute("PRAGMA table_info(itf_ingestion_log)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        if "response_id" not in existing_columns:
            await self._conn.execute("ALTER TABLE itf_ingestion_log ADD COLUMN response_id TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteItfIngestionLogStore.connect() must be called before use")
        return self._conn

    async def save(self, entry: ItfIngestionLogEntry) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                """
                INSERT INTO itf_ingestion_log
                    (row_number, content_hash, status, response_id, crm_contact_id, email, error_message, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(row_number) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    response_id = excluded.response_id,
                    crm_contact_id = excluded.crm_contact_id,
                    email = excluded.email,
                    error_message = excluded.error_message,
                    processed_at = excluded.processed_at
                """,
                (
                    entry.row_number,
                    entry.content_hash,
                    entry.status.value,
                    entry.response_id,
                    entry.crm_contact_id,
                    entry.email,
                    entry.error_message,
                    entry.processed_at.isoformat(),
                ),
            )

    async def get(self, row_number: int) -> ItfIngestionLogEntry | None:
        cursor = await self._connection.execute(
            "SELECT row_number, content_hash, status, response_id, crm_contact_id, email, error_message, processed_at "
            "FROM itf_ingestion_log WHERE row_number = ?",
            (row_number,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_model(row) if row else None

    async def get_all(self) -> dict[int, ItfIngestionLogEntry]:
        cursor = await self._connection.execute(
            "SELECT row_number, content_hash, status, response_id, crm_contact_id, email, error_message, processed_at "
            "FROM itf_ingestion_log"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {row["row_number"]: self._row_to_model(row) for row in rows}

    @staticmethod
    def _row_to_model(row: aiosqlite.Row) -> ItfIngestionLogEntry:
        return ItfIngestionLogEntry(
            row_number=row["row_number"],
            content_hash=row["content_hash"],
            status=ItfRowStatus(row["status"]),
            response_id=row["response_id"],
            crm_contact_id=row["crm_contact_id"],
            email=row["email"],
            error_message=row["error_message"],
            processed_at=row["processed_at"],
        )
