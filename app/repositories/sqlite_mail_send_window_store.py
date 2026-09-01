"""
SQLite-backed MailSendWindowStore. `mail_send_windows` follows
sqlite_mail_sequence_step_store.py's exact shape -- a real primary key
(`window_id`) plus `mail_campaign_id`/`day_of_week` pulled out as indexed/
queryable columns, with the full row also duplicated into a `data` JSON
blob (this codebase's universal per-row-JSON-blob convention).

replace_for_campaign() deletes every existing row for the campaign and
re-inserts the new set, ALL inside one `sqlite_write` block -- a single
commit/rollback, so a failure partway through leaves the previous window
set completely intact rather than partially replaced (same guarantee as
sqlite_mail_campaign_mailbox_store.py's Channels replace).
"""

import aiosqlite

from app.models.mail import MailSendWindow
from app.repositories.mail_send_window_store import MailSendWindowStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_send_windows (
    window_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    day_of_week INTEGER NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_send_windows_campaign
    ON mail_send_windows(mail_campaign_id)
"""


class SQLiteMailSendWindowStore(MailSendWindowStore):
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
            raise RuntimeError("SQLiteMailSendWindowStore.connect() must be called before use")
        return self._conn

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSendWindow]:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_send_windows WHERE mail_campaign_id = ? ORDER BY day_of_week",
            (mail_campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        windows = [MailSendWindow.model_validate_json(row["data"]) for row in rows]
        return sorted(windows, key=lambda w: (w.day_of_week, w.start_time))

    async def replace_for_campaign(self, mail_campaign_id: str, windows: list[MailSendWindow]) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "DELETE FROM mail_send_windows WHERE mail_campaign_id = ?", (mail_campaign_id,)
            )
            await self._connection.executemany(
                "INSERT INTO mail_send_windows (window_id, mail_campaign_id, day_of_week, data) VALUES (?, ?, ?, ?)",
                [(w.window_id, w.mail_campaign_id, w.day_of_week, w.model_dump_json()) for w in windows],
            )
