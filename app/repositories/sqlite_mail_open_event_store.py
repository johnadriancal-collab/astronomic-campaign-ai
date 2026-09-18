"""SQLite-backed MailOpenEventStore. `enrollment_step_id` is the literal
PRIMARY KEY. `mail_campaign_id` is promoted to a real indexed column
(this is a BRAND NEW table, no production data to stay compatible with --
same rationale as mail_enrollment_steps' own promoted columns) since
list_for_campaign() is the one read path Open rate computation depends
on; `record_open()`'s upsert uses `INSERT ... ON CONFLICT ... DO UPDATE`
so the create-vs-bump distinction is one atomic statement, not a
read-then-write race."""

import aiosqlite

from app.models.mail import MailOpenEvent
from app.repositories.mail_open_event_store import MailOpenEventStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write
from datetime import datetime

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_open_events (
    enrollment_step_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_CAMPAIGN_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_open_events_campaign
    ON mail_open_events(mail_campaign_id)
"""


class SQLiteMailOpenEventStore(MailOpenEventStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_INDEX_CAMPAIGN_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailOpenEventStore.connect() must be called before use")
        return self._conn

    async def record_open(
        self, *, enrollment_step_id: str, mail_campaign_id: str, enrollment_id: str, at: datetime
    ) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "SELECT data FROM mail_open_events WHERE enrollment_step_id = ?", (enrollment_step_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                event = MailOpenEvent(
                    enrollment_step_id=enrollment_step_id,
                    mail_campaign_id=mail_campaign_id,
                    enrollment_id=enrollment_id,
                    first_opened_at=at,
                    last_opened_at=at,
                    open_count=1,
                )
                await self._connection.execute(
                    "INSERT OR IGNORE INTO mail_open_events "
                    "(enrollment_step_id, mail_campaign_id, enrollment_id, data) VALUES (?, ?, ?, ?)",
                    (enrollment_step_id, mail_campaign_id, enrollment_id, event.model_dump_json()),
                )
                return True

            existing = MailOpenEvent.model_validate_json(row["data"])
            updated = existing.model_copy(update={"last_opened_at": at, "open_count": existing.open_count + 1})
            await self._connection.execute(
                "UPDATE mail_open_events SET data = ? WHERE enrollment_step_id = ?",
                (updated.model_dump_json(), enrollment_step_id),
            )
            return False

    async def get(self, enrollment_step_id: str) -> MailOpenEvent | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_open_events WHERE enrollment_step_id = ?", (enrollment_step_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailOpenEvent.model_validate_json(row["data"]) if row else None

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailOpenEvent]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_open_events WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailOpenEvent.model_validate_json(row["data"]) for row in rows]
