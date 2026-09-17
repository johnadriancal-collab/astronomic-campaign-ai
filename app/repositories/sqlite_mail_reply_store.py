"""SQLite-backed MailReplyStore -- enrollment_id is the literal PRIMARY
KEY; create() uses INSERT OR IGNORE so a duplicate create() call is a
true no-op at the database level too, not just in application code."""

import aiosqlite

from app.models.mail import MailReply
from app.repositories.mail_reply_store import MailReplyStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_replies (
    enrollment_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class SQLiteMailReplyStore(MailReplyStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailReplyStore.connect() must be called before use")
        return self._conn

    async def get(self, enrollment_id: str) -> MailReply | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_replies WHERE enrollment_id = ?", (enrollment_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailReply.model_validate_json(row["data"]) if row else None

    async def create(self, reply: MailReply) -> bool:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "INSERT OR IGNORE INTO mail_replies (enrollment_id, data) VALUES (?, ?)",
                (reply.enrollment_id, reply.model_dump_json()),
            )
            return cursor.rowcount == 1

    async def list_all(self) -> list[MailReply]:
        # `detected_at` lives only inside the JSON blob (no indexed column
        # on this table -- see this file's own docstring), so newest-first
        # ordering is applied in Python after parsing every row rather than
        # via SQL ORDER BY. Fine at V1 pilot scale; a real index becomes
        # worth adding once reply volume actually justifies it.
        cursor = await self._connection.execute("SELECT data FROM mail_replies")
        rows = await cursor.fetchall()
        await cursor.close()
        replies = [MailReply.model_validate_json(row["data"]) for row in rows]
        replies.sort(key=lambda r: r.detected_at, reverse=True)
        return replies
