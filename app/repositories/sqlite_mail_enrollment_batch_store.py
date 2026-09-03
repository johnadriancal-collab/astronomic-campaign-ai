"""
SQLite-backed MailEnrollmentBatchStore. `idempotency_key` and `status`
were added to this table's schema in Stage 3 (2026-09-03), AFTER Stage 2
had already deployed the original (idempotency_key-less, status-less)
version of this table to production. `connect()` migrates an existing
table safely via `ALTER TABLE ... ADD COLUMN` (checked against
PRAGMA table_info first, so it's idempotent and never destroys existing
rows) rather than dropping/recreating -- same convention as
sqlite_campaign_lead_store.py's own claude_score/claude_reason migration.
Safe in practice because Stage 2 never shipped a write path for this
table, so every real deployment's copy has zero rows -- but the migration
is written unconditionally-safe regardless, not "safe because it's
empty."

UNIQUE(mail_campaign_id, idempotency_key) is a separate CREATE UNIQUE
INDEX (not an inline column constraint) specifically so it, too, can be
added after the fact via a plain `CREATE INDEX IF NOT EXISTS` -- SQLite
has no `ALTER TABLE ... ADD CONSTRAINT`, but a new index needs no special
migration handling at all.
"""

import aiosqlite

from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchStatus
from app.repositories.mail_enrollment_batch_store import (
    DuplicateBatchIdempotencyKeyError,
    MailEnrollmentBatchNotFoundError,
    MailEnrollmentBatchStore,
)
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

CREATE_CAMPAIGN_INDEX_SQL = """
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
        await self._conn.execute(CREATE_CAMPAIGN_INDEX_SQL)
        await self._migrate_add_idempotency_and_status_columns()
        await self._conn.commit()

    async def _migrate_add_idempotency_and_status_columns(self) -> None:
        """Safe for a table that already existed before idempotency_key/
        status were added (Stage 2's already-deployed shape) -- adds only
        the columns actually missing, never touches existing rows. A
        fresh table already has them from CREATE_TABLE_SQL... except
        CREATE_TABLE_SQL above deliberately does NOT declare them (see
        below), so this migration is the ONLY place they're ever added,
        for a fresh table too -- keeps exactly one code path responsible
        for "does this table have these two columns," rather than one
        path for a fresh table and a second, separately-maintained path
        for an upgraded one."""
        cursor = await self._conn.execute("PRAGMA table_info(mail_enrollment_batches)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()

        if "idempotency_key" not in existing_columns:
            await self._conn.execute("ALTER TABLE mail_enrollment_batches ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
        if "status" not in existing_columns:
            await self._conn.execute(
                f"ALTER TABLE mail_enrollment_batches ADD COLUMN status TEXT NOT NULL DEFAULT '{MailEnrollmentBatchStatus.READY.value}'"
            )
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_enrollment_batches_idempotency "
            "ON mail_enrollment_batches(mail_campaign_id, idempotency_key)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_enrollment_batches_status ON mail_enrollment_batches(status)"
        )

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
                    "INSERT INTO mail_enrollment_batches (batch_id, mail_campaign_id, created_at, data, idempotency_key, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch.batch_id,
                        batch.mail_campaign_id,
                        batch.created_at.isoformat(),
                        batch.model_dump_json(),
                        batch.idempotency_key,
                        batch.status.value,
                    ),
                )
        except aiosqlite.IntegrityError as e:
            # SQLite's own IntegrityError message format for a UNIQUE INDEX
            # violation prefixes EVERY column with the table name (e.g.
            # "UNIQUE constraint failed: mail_enrollment_batches.
            # mail_campaign_id, mail_enrollment_batches.idempotency_key"),
            # so a literal "mail_campaign_id, idempotency_key" substring
            # never actually appears -- checking for "idempotency_key"
            # alone is what reliably distinguishes this from the
            # batch_id-PRIMARY-KEY collision case below, regardless of
            # SQLite's exact message formatting.
            if "idempotency_key" in str(e):
                raise DuplicateBatchIdempotencyKeyError(batch.mail_campaign_id, batch.idempotency_key) from e
            raise ValueError(f"MailEnrollmentBatch already exists: {batch.batch_id}") from e

    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batches WHERE batch_id = ?", (batch_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollmentBatch.model_validate_json(row["data"]) if row else None

    async def save(self, batch: MailEnrollmentBatch) -> None:
        try:
            async with sqlite_write(self._connection):
                cursor = await self._connection.execute(
                    "UPDATE mail_enrollment_batches SET data = ?, idempotency_key = ?, status = ? WHERE batch_id = ?",
                    (batch.model_dump_json(), batch.idempotency_key, batch.status.value, batch.batch_id),
                )
        except aiosqlite.IntegrityError as e:
            raise DuplicateBatchIdempotencyKeyError(batch.mail_campaign_id, batch.idempotency_key) from e
        if cursor.rowcount == 0:
            raise MailEnrollmentBatchNotFoundError(batch.batch_id)

    async def get_by_idempotency_key(self, mail_campaign_id: str, idempotency_key: str) -> MailEnrollmentBatch | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batches WHERE mail_campaign_id = ? AND idempotency_key = ?",
            (mail_campaign_id, idempotency_key),
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

    async def list_by_status(self, status: MailEnrollmentBatchStatus) -> list[MailEnrollmentBatch]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batches WHERE status = ?", (status.value,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentBatch.model_validate_json(row["data"]) for row in rows]
