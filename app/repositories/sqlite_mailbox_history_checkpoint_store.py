"""
SQLite-backed MailboxHistoryCheckpointStore. Single row per mailbox_id --
JSON-blob convention, same upsert shape as
sqlite_luma_backfill_checkpoint_store.py's own checkpoint store.
"""

import aiosqlite

from app.models.mail import MailboxHistoryCheckpoint
from app.repositories.mailbox_history_checkpoint_store import MailboxHistoryCheckpointStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mailbox_history_checkpoints (
    mailbox_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class SQLiteMailboxHistoryCheckpointStore(MailboxHistoryCheckpointStore):
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
            raise RuntimeError("SQLiteMailboxHistoryCheckpointStore.connect() must be called before use")
        return self._conn

    async def save(self, checkpoint: MailboxHistoryCheckpoint) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                """
                INSERT INTO mailbox_history_checkpoints (mailbox_id, data) VALUES (?, ?)
                ON CONFLICT(mailbox_id) DO UPDATE SET data = excluded.data
                """,
                (checkpoint.mailbox_id, checkpoint.model_dump_json()),
            )

    async def get(self, mailbox_id: str) -> MailboxHistoryCheckpoint | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mailbox_history_checkpoints WHERE mailbox_id = ?", (mailbox_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailboxHistoryCheckpoint.model_validate_json(row["data"]) if row else None
