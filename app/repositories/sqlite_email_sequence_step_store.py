"""
SQLite-backed EmailSequenceStepStore. Discrete columns (not a JSON blob) --
same convention as SQLiteCampaignLeadStore, since this is a narrow,
stable-shape row. `(email_sequence_id, position)` composite primary key
enforces exactly one step per position per sequence.
"""

from pathlib import Path

import aiosqlite

from app.models.email_sequence import EmailSequenceStep
from app.repositories.email_sequence_step_store import EmailSequenceStepStore
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_sequence_steps (
    email_sequence_step_id TEXT NOT NULL,
    email_sequence_id TEXT NOT NULL,
    apollo_step_id TEXT,
    position INTEGER NOT NULL,
    day INTEGER NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (email_sequence_id, position)
)
"""


class SQLiteEmailSequenceStepStore(EmailSequenceStepStore):
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
            raise RuntimeError("SQLiteEmailSequenceStepStore.connect() must be called before use")
        return self._conn

    async def create(self, step: EmailSequenceStep) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    """
                    INSERT INTO email_sequence_steps
                        (email_sequence_step_id, email_sequence_id, apollo_step_id, position, day, subject, body)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step.email_sequence_step_id,
                        step.email_sequence_id,
                        step.apollo_step_id,
                        step.position,
                        step.day,
                        step.subject,
                        step.body,
                    ),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(
                f"EmailSequenceStep already exists for sequence {step.email_sequence_id} "
                f"position {step.position}: {e}"
            ) from e

    async def save(self, step: EmailSequenceStep) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                """
                UPDATE email_sequence_steps
                SET apollo_step_id = ?, day = ?, subject = ?, body = ?
                WHERE email_sequence_id = ? AND position = ?
                """,
                (step.apollo_step_id, step.day, step.subject, step.body, step.email_sequence_id, step.position),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"EmailSequenceStep not found: {step.email_sequence_step_id}")

    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailSequenceStep]:
        cursor = await self._connection.execute(
            """
            SELECT email_sequence_step_id, email_sequence_id, apollo_step_id, position, day, subject, body
            FROM email_sequence_steps WHERE email_sequence_id = ? ORDER BY position
            """,
            (email_sequence_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            EmailSequenceStep(
                email_sequence_step_id=row["email_sequence_step_id"],
                email_sequence_id=row["email_sequence_id"],
                apollo_step_id=row["apollo_step_id"],
                position=row["position"],
                day=row["day"],
                subject=row["subject"],
                body=row["body"],
            )
            for row in rows
        ]
