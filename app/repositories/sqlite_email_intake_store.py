"""
SQLite-backed EmailIntakeStore -- JSON blob, same convention as
sqlite_activity_event_store.py, plus `status`/`created_at` promoted to
real indexed columns (the two things a queue list actually filters/sorts
by) and a UNIQUE constraint on `gmail_message_id` -- that constraint IS
the idempotency guarantee at the storage layer, not just an application-
level check.
"""

import aiosqlite
from loguru import logger

from app.models.email_intake import EmailIntakeItem
from app.repositories.email_intake_store import EmailIntakeDuplicateError, EmailIntakeStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_intake_items (
    intake_id TEXT PRIMARY KEY,
    gmail_message_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CREATED_AT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_email_intake_items_created_at ON email_intake_items(created_at)"
)
CREATE_STATUS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_email_intake_items_status ON email_intake_items(status)"


class SQLiteEmailIntakeStore(EmailIntakeStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CREATED_AT_INDEX_SQL)
        await self._conn.execute(CREATE_STATUS_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEmailIntakeStore.connect() must be called before use")
        return self._conn

    async def create(self, item: EmailIntakeItem) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO email_intake_items (intake_id, gmail_message_id, status, created_at, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item.intake_id, item.gmail_message_id, item.status.value, item.created_at.isoformat(), item.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            if "gmail_message_id" in str(e):
                raise EmailIntakeDuplicateError(
                    f"EmailIntakeItem already exists for gmail_message_id={item.gmail_message_id}"
                ) from e
            raise ValueError(f"EmailIntakeItem already exists: {e}") from e

    async def save(self, item: EmailIntakeItem) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "UPDATE email_intake_items SET status = ?, data = ? WHERE intake_id = ?",
                (item.status.value, item.model_dump_json(), item.intake_id),
            )

    async def get(self, intake_id: str) -> EmailIntakeItem | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_intake_items WHERE intake_id = ?", (intake_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return EmailIntakeItem.model_validate_json(row["data"]) if row else None

    async def get_by_gmail_message_id(self, gmail_message_id: str) -> EmailIntakeItem | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_intake_items WHERE gmail_message_id = ?", (gmail_message_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return EmailIntakeItem.model_validate_json(row["data"]) if row else None

    async def list(self) -> list[EmailIntakeItem]:
        cursor = await self._connection.execute(
            "SELECT intake_id, data FROM email_intake_items ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        items: list[EmailIntakeItem] = []
        for row in rows:
            try:
                items.append(EmailIntakeItem.model_validate_json(row["data"]))
            except Exception as e:
                # Same "isolated, doesn't abort the batch" instinct as
                # SQLiteActivityEventStore.list() -- one corrupted row must
                # never break retrieval of every other queue item.
                logger.error(f"Skipping unreadable email intake item {row['intake_id']}: {e}")
        return items
