"""
SQLite-backed MailSequenceStepStore. `mail_sequence_steps` has a real
UNIQUE(mail_campaign_id, step_number) constraint -- the DB-level backstop
for "no duplicate step numbers within one campaign" (service layer computes
the next number in normal usage; this constraint protects against a race
or bug, matching sqlite_crm_contact_list_member_store.py's composite-key
precedent).
"""

from pathlib import Path

import aiosqlite

from app.models.mail import MailSequenceStep
from app.repositories.mail_sequence_step_store import (
    DuplicateMailSequenceStepNumberError,
    MailSequenceStepNotFoundError,
    MailSequenceStepStore,
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_sequence_steps (
    step_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (mail_campaign_id, step_number)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_sequence_steps_campaign
    ON mail_sequence_steps(mail_campaign_id)
"""


class SQLiteMailSequenceStepStore(MailSequenceStepStore):
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
            raise RuntimeError("SQLiteMailSequenceStepStore.connect() must be called before use")
        return self._conn

    async def create(self, step: MailSequenceStep) -> None:
        try:
            await self._connection.execute(
                "INSERT INTO mail_sequence_steps (step_id, mail_campaign_id, step_number, data) VALUES (?, ?, ?, ?)",
                (step.step_id, step.mail_campaign_id, step.step_number, step.model_dump_json()),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise DuplicateMailSequenceStepNumberError(step.mail_campaign_id, step.step_number) from e

    async def get(self, step_id: str) -> MailSequenceStep | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_sequence_steps WHERE step_id = ?", (step_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailSequenceStep.model_validate_json(row["data"]) if row else None

    async def save(self, step: MailSequenceStep) -> None:
        try:
            cursor = await self._connection.execute(
                "UPDATE mail_sequence_steps SET step_number = ?, data = ? WHERE step_id = ?",
                (step.step_number, step.model_dump_json(), step.step_id),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise DuplicateMailSequenceStepNumberError(step.mail_campaign_id, step.step_number) from e
        if cursor.rowcount == 0:
            raise MailSequenceStepNotFoundError(step.step_id)

    async def delete(self, step_id: str) -> None:
        await self._connection.execute("DELETE FROM mail_sequence_steps WHERE step_id = ?", (step_id,))
        await self._connection.commit()

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSequenceStep]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_sequence_steps WHERE mail_campaign_id = ? ORDER BY step_number",
            (mail_campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailSequenceStep.model_validate_json(row["data"]) for row in rows]
