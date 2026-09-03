"""
SQLite-backed MailEnrollmentBatchStore. `mail_enrollment_batches` has no
uniqueness constraint beyond the PRIMARY KEY on `batch_id` itself -- unlike
mail_sequence_steps' UNIQUE(mail_campaign_id, step_number), nothing about
"one campaign, many batches" needs a composite constraint; batch_id
(service-generated, a real uuid4) is already globally unique on its own.
"""

import aiosqlite

from app.models.mail import MailEnrollmentBatch
from app.repositories.mail_enrollment_batch_store import MailEnrollmentBatchStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_enrollment_batches (
    batch_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_batches_campaign
    ON mail_enrollment_batches(mail_campaign_id)
"""


class SQLiteMailEnrollmentBatchStore(MailEnrollmentBatchStore):
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
            raise RuntimeError("SQLiteMailEnrollmentBatchStore.connect() must be called before use")
        return self._conn

    async def create(self, batch: MailEnrollmentBatch) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mail_enrollment_batches (batch_id, mail_campaign_id, created_at, data) VALUES (?, ?, ?, ?)",
                    (batch.batch_id, batch.mail_campaign_id, batch.created_at.isoformat(), batch.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"MailEnrollmentBatch already exists: {batch.batch_id}") from e

    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batches WHERE batch_id = ?", (batch_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollmentBatch.model_validate_json(row["data"]) if row else None

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batches WHERE mail_campaign_id = ? ORDER BY created_at DESC",
            (mail_campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentBatch.model_validate_json(row["data"]) for row in rows]
