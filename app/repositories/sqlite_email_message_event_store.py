"""
SQLite-backed EmailMessageEventStore. Discrete columns (not a JSON blob) --
same convention as SQLiteEmailSequenceStepStore, since this is a narrow,
stable-shape row. `apollo_event_id` is UNIQUE but nullable (test fixtures
never carry one; SQLite allows multiple NULLs in a UNIQUE column).
"""

import aiosqlite

from app.models.email_message import EmailMessageEvent, EmailMessageSource
from app.repositories.email_message_event_store import EmailMessageEventStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS email_message_events (
    email_message_event_id TEXT PRIMARY KEY,
    email_message_id TEXT NOT NULL,
    apollo_event_id TEXT UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    apollo_contact_id TEXT,
    readable_user_agent TEXT,
    region TEXT,
    country TEXT,
    source TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_email_message_events_message ON email_message_events(email_message_id)
"""


class SQLiteEmailMessageEventStore(EmailMessageEventStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
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
            raise RuntimeError("SQLiteEmailMessageEventStore.connect() must be called before use")
        return self._conn

    async def create(self, event: EmailMessageEvent) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    """
                    INSERT INTO email_message_events
                        (email_message_event_id, email_message_id, apollo_event_id, event_type, occurred_at,
                         apollo_contact_id, readable_user_agent, region, country, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.email_message_event_id,
                        event.email_message_id,
                        event.apollo_event_id,
                        event.event_type,
                        event.occurred_at.isoformat(),
                        event.apollo_contact_id,
                        event.readable_user_agent,
                        event.region,
                        event.country,
                        event.source.value,
                    ),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"EmailMessageEvent already exists: {e}") from e

    async def get_by_apollo_event_id(self, apollo_event_id: str) -> EmailMessageEvent | None:
        cursor = await self._connection.execute(
            "SELECT * FROM email_message_events WHERE apollo_event_id = ?", (apollo_event_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_event(row) if row else None

    async def list_for_message(self, email_message_id: str) -> list[EmailMessageEvent]:
        cursor = await self._connection.execute(
            "SELECT * FROM email_message_events WHERE email_message_id = ? ORDER BY occurred_at",
            (email_message_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: aiosqlite.Row) -> EmailMessageEvent:
    return EmailMessageEvent(
        email_message_event_id=row["email_message_event_id"],
        email_message_id=row["email_message_id"],
        apollo_event_id=row["apollo_event_id"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        apollo_contact_id=row["apollo_contact_id"],
        readable_user_agent=row["readable_user_agent"],
        region=row["region"],
        country=row["country"],
        source=EmailMessageSource(row["source"]),
    )
