"""SQLite-backed MailTriggerOccurrenceStore -- owns BOTH
`mail_trigger_occurrences` and `mail_trigger_occurrence_members` on ONE
aiosqlite connection, deliberately, so freeze_members() can genuinely be
one atomic SQLite transaction (see mail_trigger_occurrence_store.py's own
module docstring for why two separate stores/connections could not achieve
this -- the exact Stage 3 lesson this Stage 5A design was built around).

Real columns (not a JSON blob), matching
sqlite_mail_campaign_csv_prospect_link_store.py's reasoning: small,
structural rows, no PII, no raw enrollment content -- just ids/timestamps/
an enum-shaped string.

Stage 5A (2026-09-04): a brand-new pair of tables, no prior deployed shape
to accommodate -- CREATE TABLE IF NOT EXISTS is trivially idempotent, no
separate migration step needed."""

from datetime import datetime

import aiosqlite

from app.models.mail import MailTriggerOccurrence, MailTriggerOccurrenceMember
from app.repositories.mail_trigger_occurrence_store import MailTriggerOccurrenceStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_OCCURRENCES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_trigger_occurrences (
    trigger_id TEXT NOT NULL,
    mail_campaign_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    frozen_at TEXT,
    started_count INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (trigger_id, scheduled_for)
)
"""

# `enrollment_id` carries its own bare UNIQUE, on top of the composite
# PRIMARY KEY -- this is the actual mechanism behind "once an enrollment is
# frozen into any occurrence's cohort, it can never join a different one"
# (see MailTriggerOccurrenceMember's own docstring for why that invariant
# is correct, not merely convenient).
CREATE_MEMBERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_trigger_occurrence_members (
    trigger_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reconciled_at TEXT,
    PRIMARY KEY (trigger_id, scheduled_for, enrollment_id),
    UNIQUE (enrollment_id)
)
"""

CREATE_MEMBERS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_trigger_occurrence_members_occurrence
    ON mail_trigger_occurrence_members(trigger_id, scheduled_for)
"""


def _row_to_occurrence(row: aiosqlite.Row) -> MailTriggerOccurrence:
    return MailTriggerOccurrence(
        trigger_id=row["trigger_id"],
        mail_campaign_id=row["mail_campaign_id"],
        scheduled_for=datetime.fromisoformat(row["scheduled_for"]),
        status=row["status"],
        target_count=row["target_count"],
        frozen_at=datetime.fromisoformat(row["frozen_at"]) if row["frozen_at"] else None,
        started_count=row["started_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_member(row: aiosqlite.Row) -> MailTriggerOccurrenceMember:
    return MailTriggerOccurrenceMember(
        trigger_id=row["trigger_id"],
        scheduled_for=datetime.fromisoformat(row["scheduled_for"]),
        enrollment_id=row["enrollment_id"],
        outcome=row["outcome"],
        reconciled_at=datetime.fromisoformat(row["reconciled_at"]) if row["reconciled_at"] else None,
    )


class SQLiteMailTriggerOccurrenceStore(MailTriggerOccurrenceStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_OCCURRENCES_TABLE_SQL)
        await self._conn.execute(CREATE_MEMBERS_TABLE_SQL)
        await self._conn.execute(CREATE_MEMBERS_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailTriggerOccurrenceStore.connect() must be called before use")
        return self._conn

    async def create_occurrence(self, occurrence: MailTriggerOccurrence) -> bool:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mail_trigger_occurrences "
                    "(trigger_id, mail_campaign_id, scheduled_for, status, target_count, frozen_at, "
                    "started_count, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        occurrence.trigger_id,
                        occurrence.mail_campaign_id,
                        occurrence.scheduled_for.isoformat(),
                        occurrence.status,
                        occurrence.target_count,
                        occurrence.frozen_at.isoformat() if occurrence.frozen_at else None,
                        occurrence.started_count,
                        occurrence.created_at.isoformat(),
                        occurrence.completed_at.isoformat() if occurrence.completed_at else None,
                    ),
                )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_occurrence(self, trigger_id: str, scheduled_for: datetime) -> MailTriggerOccurrence | None:
        cursor = await self._connection.execute(
            "SELECT * FROM mail_trigger_occurrences WHERE trigger_id = ? AND scheduled_for = ?",
            (trigger_id, scheduled_for.isoformat()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_occurrence(row) if row else None

    async def freeze_members(
        self, trigger_id: str, scheduled_for: datetime, enrollment_ids: list[str], now: datetime
    ) -> bool:
        existing = await self.get_occurrence(trigger_id, scheduled_for)
        if existing is None:
            raise ValueError(f"No occurrence exists for ({trigger_id!r}, {scheduled_for!r}) -- create it first.")

        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_trigger_occurrences SET frozen_at = ? "
                "WHERE trigger_id = ? AND scheduled_for = ? AND frozen_at IS NULL",
                (now.isoformat(), trigger_id, scheduled_for.isoformat()),
            )
            if cursor.rowcount == 0:
                return False
            for enrollment_id in enrollment_ids:
                # OR IGNORE: a candidate already claimed by another
                # occurrence (the global UNIQUE(enrollment_id)) is
                # excluded from THIS cohort, not an error that aborts the
                # whole freeze -- see this store's own freeze_members()
                # docstring / MailTriggerOccurrenceMember's docstring.
                await self._connection.execute(
                    "INSERT OR IGNORE INTO mail_trigger_occurrence_members "
                    "(trigger_id, scheduled_for, enrollment_id, outcome) VALUES (?, ?, ?, 'PENDING_RECONCILE')",
                    (trigger_id, scheduled_for.isoformat(), enrollment_id),
                )
        return True

    async def list_members(self, trigger_id: str, scheduled_for: datetime) -> list[MailTriggerOccurrenceMember]:
        cursor = await self._connection.execute(
            "SELECT * FROM mail_trigger_occurrence_members WHERE trigger_id = ? AND scheduled_for = ?",
            (trigger_id, scheduled_for.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_member(row) for row in rows]

    async def mark_member_reconciled(
        self, trigger_id: str, scheduled_for: datetime, enrollment_id: str, outcome: str, reconciled_at: datetime
    ) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "UPDATE mail_trigger_occurrence_members SET outcome = ?, reconciled_at = ? "
                "WHERE trigger_id = ? AND scheduled_for = ? AND enrollment_id = ? AND outcome = 'PENDING_RECONCILE'",
                (outcome, reconciled_at.isoformat(), trigger_id, scheduled_for.isoformat(), enrollment_id),
            )

    async def complete_occurrence(
        self, trigger_id: str, scheduled_for: datetime, started_count: int, completed_at: datetime
    ) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_trigger_occurrences SET status = 'COMPLETED', started_count = ?, completed_at = ? "
                "WHERE trigger_id = ? AND scheduled_for = ? AND status = 'PREPARING'",
                (started_count, completed_at.isoformat(), trigger_id, scheduled_for.isoformat()),
            )
        return cursor.rowcount > 0
