"""
SQLite-backed MailEnrollmentStore. `mail_enrollments` has a composite
PRIMARY KEY (mail_campaign_id, crm_contact_id) -- create() uses INSERT OR
IGNORE, matching sqlite_crm_contact_list_member_store.py's exact pattern,
so this is the actual DB-level guarantee behind "the same contact can
never be enrolled twice in the same campaign."
"""

import aiosqlite

from app.models.mail import MailEnrollment
from app.repositories.mail_enrollment_store import MailEnrollmentNotFoundError, MailEnrollmentStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_enrollments (
    enrollment_id TEXT NOT NULL,
    mail_campaign_id TEXT NOT NULL,
    crm_contact_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (mail_campaign_id, crm_contact_id)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollments_contact
    ON mail_enrollments(crm_contact_id)
"""

CREATE_ENROLLMENT_ID_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_enrollments_enrollment_id
    ON mail_enrollments(enrollment_id)
"""


class SQLiteMailEnrollmentStore(MailEnrollmentStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_INDEX_SQL)
        await self._conn.execute(CREATE_ENROLLMENT_ID_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailEnrollmentStore.connect() must be called before use")
        return self._conn

    async def create(self, enrollment: MailEnrollment) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "INSERT OR IGNORE INTO mail_enrollments (enrollment_id, mail_campaign_id, crm_contact_id, data) "
                "VALUES (?, ?, ?, ?)",
                (
                    enrollment.enrollment_id,
                    enrollment.mail_campaign_id,
                    enrollment.crm_contact_id,
                    enrollment.model_dump_json(),
                ),
            )
        return cursor.rowcount > 0

    async def save(self, enrollment: MailEnrollment) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_enrollments SET data = ? WHERE enrollment_id = ?",
                (enrollment.model_dump_json(), enrollment.enrollment_id),
            )
        if cursor.rowcount == 0:
            raise MailEnrollmentNotFoundError(enrollment.enrollment_id)

    async def try_assign_mailbox(self, enrollment_id: str, updated: MailEnrollment) -> bool:
        """The real compare-and-swap: a single conditional UPDATE using
        SQLite's JSON1 `json_extract()` to read the CURRENT row's
        assigned_mailbox_id directly in the WHERE clause -- no separate
        SELECT-then-UPDATE round trip, so there is no window for a second
        writer to slip in between a read and a write on this connection.
        SQLite's own single-writer-per-file serialization is what makes
        this one statement atomic against a second CONNECTION (e.g. a
        future second worker process) too, exactly the same primitive
        MailEnrollmentStepStore.try_transition() already relies on."""
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_enrollments SET data = ? "
                "WHERE enrollment_id = ? AND json_extract(data, '$.assigned_mailbox_id') IS NULL",
                (updated.model_dump_json(), enrollment_id),
            )
        return cursor.rowcount > 0

    async def get(self, enrollment_id: str) -> MailEnrollment | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollments WHERE enrollment_id = ?", (enrollment_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollment.model_validate_json(row["data"]) if row else None

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollment]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollment.model_validate_json(row["data"]) for row in rows]

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "DELETE FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
            )

    async def count_for_campaign(self, mail_campaign_id: str) -> int:
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["n"] if row else 0
