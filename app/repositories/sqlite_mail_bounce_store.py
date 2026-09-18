"""SQLite-backed MailBounceStore. `gmail_message_id` is the literal
PRIMARY KEY; `mail_campaign_id` is promoted to a real indexed column
(this is a BRAND NEW table, no production data to stay compatible with)
since list_for_campaign() is the read path Bounce rate computation
depends on. `create()` uses INSERT OR IGNORE so a duplicate DSN
re-ingest is a true no-op at the database level too, matching
sqlite_mail_reply_store.py's own convention."""

import aiosqlite

from app.models.mail import MailBounce
from app.repositories.mail_bounce_store import MailBounceStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_bounces (
    gmail_message_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_CAMPAIGN_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_bounces_campaign
    ON mail_bounces(mail_campaign_id)
"""


class SQLiteMailBounceStore(MailBounceStore):
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
            raise RuntimeError("SQLiteMailBounceStore.connect() must be called before use")
        return self._conn

    async def create(self, bounce: MailBounce) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "INSERT OR IGNORE INTO mail_bounces (gmail_message_id, mail_campaign_id, enrollment_id, data) "
                "VALUES (?, ?, ?, ?)",
                (bounce.gmail_message_id, bounce.mail_campaign_id, bounce.enrollment_id, bounce.model_dump_json()),
            )
            return cursor.rowcount == 1

    async def get(self, gmail_message_id: str) -> MailBounce | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_bounces WHERE gmail_message_id = ?", (gmail_message_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailBounce.model_validate_json(row["data"]) if row else None

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailBounce]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_bounces WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailBounce.model_validate_json(row["data"]) for row in rows]
