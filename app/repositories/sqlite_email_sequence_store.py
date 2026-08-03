"""
SQLite-backed EmailSequenceStore. Same conventions as SQLiteCampaignStore/
SQLiteLeadStore: EmailSequence is stored as a JSON blob (model_dump_json()),
not mapped into relational columns, so it can gain fields freely (e.g. more
aggregate stat fields) with no schema migration. `campaign_id` and
`apollo_sequence_id` are both UNIQUE -- the former enforces the 1:1 with
Campaign, the latter is the sync join key.
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.email_sequence import EmailSequence
from app.repositories.email_sequence_store import EmailSequenceNotFoundError, EmailSequenceStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_sequences (
    email_sequence_id TEXT PRIMARY KEY,
    workspace_id TEXT,
    campaign_id TEXT NOT NULL UNIQUE,
    apollo_sequence_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteEmailSequenceStore(EmailSequenceStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEmailSequenceStore.connect() must be called before use")
        return self._conn

    async def create(self, sequence: EmailSequence) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.execute(
                """
                INSERT INTO email_sequences
                    (email_sequence_id, workspace_id, campaign_id, apollo_sequence_id, status, created_at, updated_at, data)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence.email_sequence_id,
                    sequence.campaign_id,
                    sequence.apollo_sequence_id,
                    sequence.status.value,
                    now,
                    now,
                    sequence.model_dump_json(),
                ),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(
                f"EmailSequence already exists (id or campaign_id collision): {e}"
            ) from e

    async def get(self, email_sequence_id: str) -> EmailSequence | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_sequences WHERE email_sequence_id = ?", (email_sequence_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return EmailSequence.model_validate_json(row["data"])

    async def get_by_campaign_id(self, campaign_id: str) -> EmailSequence | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_sequences WHERE campaign_id = ?", (campaign_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return EmailSequence.model_validate_json(row["data"])

    async def list_all(self) -> list[EmailSequence]:
        cursor = await self._connection.execute("SELECT data FROM email_sequences")
        rows = await cursor.fetchall()
        await cursor.close()
        return [EmailSequence.model_validate_json(row["data"]) for row in rows]

    async def get_by_apollo_sequence_id(self, apollo_sequence_id: str) -> EmailSequence | None:
        cursor = await self._connection.execute(
            "SELECT data FROM email_sequences WHERE apollo_sequence_id = ?", (apollo_sequence_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return EmailSequence.model_validate_json(row["data"])

    async def save(self, sequence: EmailSequence) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._connection.execute(
            "UPDATE email_sequences SET status = ?, updated_at = ?, data = ? WHERE email_sequence_id = ?",
            (sequence.status.value, now, sequence.model_dump_json(), sequence.email_sequence_id),
        )
        await self._connection.commit()
        if cursor.rowcount == 0:
            raise EmailSequenceNotFoundError(sequence.email_sequence_id)
