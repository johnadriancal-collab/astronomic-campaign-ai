"""
SQLite-backed EngagementParticipantStore -- JSON blob (full
EngagementParticipant via model_dump_json()) plus TWO denormalized,
indexed columns: `engagement_id` (every Engagement detail page load needs
list_for_engagement) and `crm_contact_id` (needed for the partial unique
index enforcing at-most-one-active-participant-per-Contact-per-Engagement).

The uniqueness invariant is enforced by a real SQLite partial unique
index -- `WHERE crm_contact_id IS NOT NULL` -- empirically confirmed
(this stage's own investigation) to: (1) allow unlimited unresolved
(crm_contact_id IS NULL) rows per Engagement, since SQLite excludes NULL
rows from a partial index entirely; (2) reject a duplicate INSERT; (3)
reject an UPDATE that would create the same duplicate (the exact
"unresolved participant later linked to an already-active Contact" case);
(4) allow the same crm_contact_id across DIFFERENT Engagements. This
constraint does NOT distinguish archived from active rows -- Stage 1G's
own approved design is "restore an archived participant, never create a
new one for the same person" (same idiom as every other Client CRM
entity's own archive/restore convention), so the uniqueness check
correctly stays permanent, not scoped to "currently active" rows.

Stage 1G (2026-09-08): a brand-new table, no prior deployed shape to
accommodate.
"""

import aiosqlite

from app.models.client_crm import EngagementParticipant
from app.repositories.engagement_participant_store import (
    EngagementParticipantDuplicateError,
    EngagementParticipantNotFoundError,
    EngagementParticipantStore,
)
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engagement_participants (
    participant_id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL,
    crm_contact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_ENGAGEMENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_engagement_participants_engagement ON engagement_participants(engagement_id)
"""

# Partial unique index -- see this module's own docstring for the
# empirically-confirmed NULL/duplicate/cross-engagement behavior.
CREATE_UNIQUE_CONTACT_PER_ENGAGEMENT_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_engagement_participants_unique_contact
ON engagement_participants(engagement_id, crm_contact_id)
WHERE crm_contact_id IS NOT NULL
"""


def _row_to_participant(row: aiosqlite.Row) -> EngagementParticipant:
    return EngagementParticipant.model_validate_json(row["data"])


class SQLiteEngagementParticipantStore(EngagementParticipantStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_ENGAGEMENT_INDEX_SQL)
        await self._conn.execute(CREATE_UNIQUE_CONTACT_PER_ENGAGEMENT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEngagementParticipantStore.connect() must be called before use")
        return self._conn

    async def create(self, participant: EngagementParticipant) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO engagement_participants "
                    "(participant_id, engagement_id, crm_contact_id, created_at, updated_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        participant.participant_id,
                        participant.engagement_id,
                        participant.crm_contact_id,
                        participant.created_at.isoformat(),
                        participant.updated_at.isoformat(),
                        participant.model_dump_json(),
                    ),
                )
        except aiosqlite.IntegrityError as exc:
            raise EngagementParticipantDuplicateError(participant.engagement_id, participant.crm_contact_id) from exc

    async def get(self, participant_id: str) -> EngagementParticipant | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagement_participants WHERE participant_id = ?", (participant_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_participant(row) if row else None

    async def save(self, participant: EngagementParticipant) -> None:
        try:
            async with sqlite_write(self._connection):
                cursor = await self._connection.execute(
                    "UPDATE engagement_participants SET engagement_id = ?, crm_contact_id = ?, updated_at = ?, data = ? "
                    "WHERE participant_id = ?",
                    (
                        participant.engagement_id,
                        participant.crm_contact_id,
                        participant.updated_at.isoformat(),
                        participant.model_dump_json(),
                        participant.participant_id,
                    ),
                )
        except aiosqlite.IntegrityError as exc:
            raise EngagementParticipantDuplicateError(participant.engagement_id, participant.crm_contact_id) from exc
        if cursor.rowcount == 0:
            raise EngagementParticipantNotFoundError(participant.participant_id)

    async def list_for_engagement(self, engagement_id: str) -> list[EngagementParticipant]:
        cursor = await self._connection.execute(
            "SELECT * FROM engagement_participants WHERE engagement_id = ? ORDER BY created_at", (engagement_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_participant(row) for row in rows]
