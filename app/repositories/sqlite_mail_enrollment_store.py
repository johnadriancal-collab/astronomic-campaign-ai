"""
SQLite-backed MailEnrollmentStore. `mail_enrollments` has a composite
PRIMARY KEY (mail_campaign_id, crm_contact_id) -- create() uses INSERT OR
IGNORE, matching sqlite_crm_contact_list_member_store.py's exact pattern,
so this is the actual DB-level guarantee behind "the same contact can
never be enrolled twice in the same campaign."
"""

from pathlib import Path

import aiosqlite

from app.models.mail import MailEnrollment
from app.repositories.mail_enrollment_store import MailEnrollmentStore

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


class SQLiteMailEnrollmentStore(MailEnrollmentStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
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
            raise RuntimeError("SQLiteMailEnrollmentStore.connect() must be called before use")
        return self._conn

    async def create(self, enrollment: MailEnrollment) -> bool:
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
        await self._connection.commit()
        return cursor.rowcount > 0

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollment]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollment.model_validate_json(row["data"]) for row in rows]

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        await self._connection.commit()

    async def count_for_campaign(self, mail_campaign_id: str) -> int:
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM mail_enrollments WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["n"] if row else 0
