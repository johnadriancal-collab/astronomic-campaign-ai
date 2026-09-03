"""
SQLite-backed MailEnrollmentBatchMemberStore. `mail_enrollment_batch_members`
has a real PRIMARY KEY (batch_id, crm_contact_id) -- the DB-level guarantee
behind "a candidate can never appear twice within one batch's frozen
cohort" -- and a real, indexed `created_at` column (not JSON-extracted)
specifically so list_distinct_batch_ids_created_before() (the orphan-
cleanup age-threshold query) is a plain, efficient range scan.
"""

import aiosqlite

from app.models.mail import MailEnrollmentBatchMember
from app.repositories.mail_enrollment_batch_member_store import MailEnrollmentBatchMemberStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_enrollment_batch_members (
    batch_id TEXT NOT NULL,
    crm_contact_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (batch_id, crm_contact_id)
)
"""

CREATE_CREATED_AT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_batch_members_created_at
    ON mail_enrollment_batch_members(created_at)
"""


class SQLiteMailEnrollmentBatchMemberStore(MailEnrollmentBatchMemberStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CREATED_AT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailEnrollmentBatchMemberStore.connect() must be called before use")
        return self._conn

    async def create(self, member: MailEnrollmentBatchMember) -> bool:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mail_enrollment_batch_members (batch_id, crm_contact_id, created_at, data) VALUES (?, ?, ?, ?)",
                    (member.batch_id, member.crm_contact_id, member.created_at.isoformat(), member.model_dump_json()),
                )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def save(self, member: MailEnrollmentBatchMember) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_enrollment_batch_members SET data = ? WHERE batch_id = ? AND crm_contact_id = ?",
                (member.model_dump_json(), member.batch_id, member.crm_contact_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"MailEnrollmentBatchMember not found: ({member.batch_id}, {member.crm_contact_id})")

    async def list_for_batch(self, batch_id: str) -> list[MailEnrollmentBatchMember]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_batch_members WHERE batch_id = ?", (batch_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentBatchMember.model_validate_json(row["data"]) for row in rows]

    async def list_distinct_batch_ids_created_before(self, cutoff) -> list[str]:
        cursor = await self._connection.execute(
            "SELECT DISTINCT batch_id FROM mail_enrollment_batch_members WHERE created_at < ?",
            (cutoff.isoformat(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["batch_id"] for row in rows]

    async def delete_for_batch(self, batch_id: str) -> int:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "DELETE FROM mail_enrollment_batch_members WHERE batch_id = ?", (batch_id,)
            )
        return cursor.rowcount
