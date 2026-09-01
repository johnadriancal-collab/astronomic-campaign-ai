"""
SQLite-backed EmailMessageStore. Same convention as SQLiteEmailSequenceStore:
EmailMessage is stored as a JSON blob (model_dump_json()), so it can gain
fields freely with no schema migration -- important given Apollo's
`status` and other fields are deliberately open/unvalidated (see
app/models/email_message.py). `apollo_message_id` is UNIQUE but nullable
(SQLite allows multiple NULLs in a UNIQUE column), since test-fixture rows
never carry one.
"""

import aiosqlite

from app.models.email_message import EmailMessage
from app.repositories.email_message_store import EmailMessageNotFoundError, EmailMessageStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_messages (
    email_message_id TEXT PRIMARY KEY,
    email_sequence_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    apollo_message_id TEXT UNIQUE,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_email_messages_sequence ON email_messages(email_sequence_id)
"""


class SQLiteEmailMessageStore(EmailMessageStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
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
            raise RuntimeError("SQLiteEmailMessageStore.connect() must be called before use")
        return self._conn

    async def create(self, message: EmailMessage) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    """
                    INSERT INTO email_messages
                        (email_message_id, email_sequence_id, lead_id, apollo_message_id, source, status, data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.email_message_id,
                        message.email_sequence_id,
                        message.lead_id,
                        message.apollo_message_id,
                        message.source.value,
                        message.status,
                        message.model_dump_json(),
                    ),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"EmailMessage already exists: {e}") from e

    async def get(self, email_message_id: str) -> EmailMessage | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_messages WHERE email_message_id = ?", (email_message_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return EmailMessage.model_validate_json(row["data"]) if row else None

    async def get_by_apollo_message_id(self, apollo_message_id: str) -> EmailMessage | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_messages WHERE apollo_message_id = ?", (apollo_message_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return EmailMessage.model_validate_json(row["data"]) if row else None

    async def save(self, message: EmailMessage) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                """
                UPDATE email_messages
                SET email_sequence_id = ?, lead_id = ?, apollo_message_id = ?, source = ?, status = ?, data = ?
                WHERE email_message_id = ?
                """,
                (
                    message.email_sequence_id,
                    message.lead_id,
                    message.apollo_message_id,
                    message.source.value,
                    message.status,
                    message.model_dump_json(),
                    message.email_message_id,
                ),
            )
        if cursor.rowcount == 0:
            raise EmailMessageNotFoundError(message.email_message_id)

    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailMessage]:
        cursor = await self._connection.execute(
            "SELECT data FROM email_messages WHERE email_sequence_id = ?", (email_sequence_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [EmailMessage.model_validate_json(row["data"]) for row in rows]
