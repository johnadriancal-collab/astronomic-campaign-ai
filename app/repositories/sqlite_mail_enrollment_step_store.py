"""
SQLite-backed MailEnrollmentStepStore. `mail_enrollment_steps` is a BRAND
NEW table (introduced in this same change) -- unlike every pre-existing
Mail* table, there is no deployed production data to stay compatible with
here, so this is free to promote every column the query patterns below
actually need to real, indexed SQL columns rather than leaving them buried
inside the `data` JSON blob (the older tables' convention, chosen when
their query needs were much narrower). `data` still holds the full
serialized row (same convention as every other store), so a promoted
column is purely a queryability/indexing optimization, never a second,
independently-writable source of truth -- every write updates both
together, in the same statement.

UNIQUE(enrollment_id, step_id) is the actual DB-level backstop for
MailEnrollmentStepStore.create()'s idempotency contract (the service layer
already guarantees at most one row per pair by construction -- lazy
materialization never attempts to create a second row for a step it's
already created -- this constraint protects against a race or bug, same
"service computes / store enforces" pattern as every other Mail* table's
uniqueness constraint).

try_transition()'s atomicity: the conditional `UPDATE ... WHERE
enrollment_step_id = ? AND status = ?` is a single SQL statement -- SQLite
serializes all writes to one file one at a time regardless of WAL mode, so
this statement's WHERE-clause check and its write are indivisible from any
other writer's perspective, no application-level lock required. This is
the real guarantee behind the CLAIMED transition's safety (see
MailEnrollmentStepStatus's docstring in app/models/mail.py) -- not
something achieved by discipline in the calling code.
"""

import aiosqlite

from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepNotFoundError, MailEnrollmentStepStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_enrollment_steps (
    enrollment_step_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    next_send_at TEXT,
    mailbox_id TEXT,
    sent_at TEXT,
    claimed_at TEXT,
    data TEXT NOT NULL,
    UNIQUE (enrollment_id, step_id)
)
"""

CREATE_INDEX_DUE_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_steps_due
    ON mail_enrollment_steps(status, next_send_at)
"""
CREATE_INDEX_MAILBOX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_steps_mailbox
    ON mail_enrollment_steps(mailbox_id, status, sent_at)
"""
CREATE_INDEX_CAMPAIGN_STEP_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_steps_campaign_step
    ON mail_enrollment_steps(mail_campaign_id, step_number, status, sent_at)
"""
CREATE_INDEX_ENROLLMENT_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_steps_enrollment
    ON mail_enrollment_steps(enrollment_id)
"""
CREATE_INDEX_CLAIMED_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_enrollment_steps_claimed
    ON mail_enrollment_steps(status, claimed_at)
"""

_ALL_COLUMNS = (
    "enrollment_step_id, mail_campaign_id, enrollment_id, step_id, step_number, "
    "status, next_send_at, mailbox_id, sent_at, claimed_at, data"
)


def _row_values(step: MailEnrollmentStep) -> tuple:
    return (
        step.enrollment_step_id,
        step.mail_campaign_id,
        step.enrollment_id,
        step.step_id,
        step.step_number,
        step.status.value,
        step.next_send_at.isoformat() if step.next_send_at else None,
        step.mailbox_id,
        step.sent_at.isoformat() if step.sent_at else None,
        step.claimed_at.isoformat() if step.claimed_at else None,
        step.model_dump_json(),
    )


class SQLiteMailEnrollmentStepStore(MailEnrollmentStepStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_INDEX_DUE_SQL)
        await self._conn.execute(CREATE_INDEX_MAILBOX_SQL)
        await self._conn.execute(CREATE_INDEX_CAMPAIGN_STEP_SQL)
        await self._conn.execute(CREATE_INDEX_ENROLLMENT_SQL)
        await self._conn.execute(CREATE_INDEX_CLAIMED_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailEnrollmentStepStore.connect() must be called before use")
        return self._conn

    async def create(self, step: MailEnrollmentStep) -> bool:
        try:
            async with sqlite_write(self._connection):
                cursor = await self._connection.execute(
                    f"INSERT INTO mail_enrollment_steps ({_ALL_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _row_values(step),
                )
        except aiosqlite.IntegrityError:
            return False
        return cursor.rowcount > 0

    async def get(self, enrollment_step_id: str) -> MailEnrollmentStep | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE enrollment_step_id = ?", (enrollment_step_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollmentStep.model_validate_json(row["data"]) if row else None

    async def get_by_enrollment_and_step(self, enrollment_id: str, step_id: str) -> MailEnrollmentStep | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE enrollment_id = ? AND step_id = ?",
            (enrollment_id, step_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollmentStep.model_validate_json(row["data"]) if row else None

    async def save(self, step: MailEnrollmentStep) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_enrollment_steps SET mail_campaign_id=?, enrollment_id=?, step_id=?, step_number=?, "
                "status=?, next_send_at=?, mailbox_id=?, sent_at=?, claimed_at=?, data=? WHERE enrollment_step_id=?",
                (
                    step.mail_campaign_id,
                    step.enrollment_id,
                    step.step_id,
                    step.step_number,
                    step.status.value,
                    step.next_send_at.isoformat() if step.next_send_at else None,
                    step.mailbox_id,
                    step.sent_at.isoformat() if step.sent_at else None,
                    step.claimed_at.isoformat() if step.claimed_at else None,
                    step.model_dump_json(),
                    step.enrollment_step_id,
                ),
            )
        if cursor.rowcount == 0:
            raise MailEnrollmentStepNotFoundError(step.enrollment_step_id)

    async def try_transition(
        self, enrollment_step_id: str, expected_status: MailEnrollmentStepStatus, updated: MailEnrollmentStep
    ) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_enrollment_steps SET status=?, next_send_at=?, mailbox_id=?, sent_at=?, claimed_at=?, data=? "
                "WHERE enrollment_step_id=? AND status=?",
                (
                    updated.status.value,
                    updated.next_send_at.isoformat() if updated.next_send_at else None,
                    updated.mailbox_id,
                    updated.sent_at.isoformat() if updated.sent_at else None,
                    updated.claimed_at.isoformat() if updated.claimed_at else None,
                    updated.model_dump_json(),
                    enrollment_step_id,
                    expected_status.value,
                ),
            )
        return cursor.rowcount > 0

    async def persist_prepared_fields(self, enrollment_step_id: str, updated: MailEnrollmentStep) -> bool:
        # A semantically named, SAME-STATUS use of try_transition() -- see
        # that method's ABC docstring on MailEnrollmentStepStore. Deliberately
        # NOT a separate SQL statement: try_transition()'s conditional
        # `WHERE ... AND status=?` already IS the correct atomic CAS for
        # "persist this row iff it's still CLAIMED," it just needed a
        # clearer name at the call site than passing CLAIMED as both the
        # expected and the new status.
        return await self.try_transition(enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, updated)

    async def list_for_enrollment(self, enrollment_id: str) -> list[MailEnrollmentStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE enrollment_id = ? ORDER BY step_number",
            (enrollment_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentStep.model_validate_json(row["data"]) for row in rows]

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE mail_campaign_id = ? ORDER BY enrollment_id, step_number",
            (mail_campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentStep.model_validate_json(row["data"]) for row in rows]

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "DELETE FROM mail_enrollment_steps WHERE mail_campaign_id = ?", (mail_campaign_id,)
            )

    async def list_due(self, now, limit: int = 100) -> list[MailEnrollmentStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE status = ? AND next_send_at IS NOT NULL AND next_send_at <= ? "
            "ORDER BY next_send_at LIMIT ?",
            (MailEnrollmentStepStatus.QUEUED.value, now.isoformat(), limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentStep.model_validate_json(row["data"]) for row in rows]

    async def count_sent_step_for_campaign_since(self, mail_campaign_id: str, step_number: int, since) -> int:
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM mail_enrollment_steps "
            "WHERE mail_campaign_id = ? AND step_number = ? AND status = ? AND sent_at IS NOT NULL AND sent_at >= ?",
            (mail_campaign_id, step_number, MailEnrollmentStepStatus.SENT.value, since.isoformat()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["n"] if row else 0

    async def count_sent_for_mailbox_since(self, mailbox_id: str, since) -> int:
        cursor = await self._connection.execute(
            "SELECT COUNT(*) AS n FROM mail_enrollment_steps "
            "WHERE mailbox_id = ? AND status = ? AND sent_at IS NOT NULL AND sent_at >= ?",
            (mailbox_id, MailEnrollmentStepStatus.SENT.value, since.isoformat()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["n"] if row else 0

    async def get_most_recent_sent_for_mailbox(self, mailbox_id: str) -> MailEnrollmentStep | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE mailbox_id = ? AND status = ? AND sent_at IS NOT NULL "
            "ORDER BY sent_at DESC LIMIT 1",
            (mailbox_id, MailEnrollmentStepStatus.SENT.value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailEnrollmentStep.model_validate_json(row["data"]) if row else None

    async def list_stale_claimed(self, older_than) -> list[MailEnrollmentStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE status = ? AND claimed_at IS NOT NULL AND claimed_at <= ?",
            (MailEnrollmentStepStatus.CLAIMED.value, older_than.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentStep.model_validate_json(row["data"]) for row in rows]

    async def list_stale_sending(self, older_than) -> list[MailEnrollmentStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_enrollment_steps WHERE status = ? AND claimed_at IS NOT NULL AND claimed_at <= ?",
            (MailEnrollmentStepStatus.SENDING.value, older_than.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailEnrollmentStep.model_validate_json(row["data"]) for row in rows]
