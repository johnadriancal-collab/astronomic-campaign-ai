"""SQLite-backed MailSuppressionStore -- email_normalized is the literal
PRIMARY KEY; upsert() uses INSERT ... ON CONFLICT DO UPDATE so a
suppress/unsuppress/re-suppress cycle always mutates the same one row."""

import aiosqlite

from app.models.mail import MailSuppression
from app.repositories.mail_suppression_store import MailSuppressionStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_suppressions (
    email_normalized TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class SQLiteMailSuppressionStore(MailSuppressionStore):
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
            raise RuntimeError("SQLiteMailSuppressionStore.connect() must be called before use")
        return self._conn

    async def get(self, email_normalized: str) -> MailSuppression | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_suppressions WHERE email_normalized = ?", (email_normalized,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailSuppression.model_validate_json(row["data"]) if row else None

    async def upsert(self, suppression: MailSuppression) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO mail_suppressions (email_normalized, data) VALUES (?, ?) "
                "ON CONFLICT (email_normalized) DO UPDATE SET data = excluded.data",
                (suppression.email_normalized, suppression.model_dump_json()),
            )

    async def list(self) -> list[MailSuppression]:
        cursor = await self._connection.execute("SELECT data FROM mail_suppressions")
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailSuppression.model_validate_json(row["data"]) for row in rows]
