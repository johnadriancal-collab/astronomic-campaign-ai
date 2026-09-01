"""SQLite-backed AuthSessionStore -- JSON blob, same shared database_path
as every other store. Sessions must survive a Railway redeploy (otherwise
every team member would be logged out on every deploy), matching the
Mailbox/MailboxCredential precedent rather than the in-memory-only OAuth
`state` store (which is deliberately allowed to reset on restart)."""

from datetime import datetime

import aiosqlite

from app.models.auth import AuthSession
from app.repositories.auth_session_store import AuthSessionStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteAuthSessionStore(AuthSessionStore):
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
            raise RuntimeError("SQLiteAuthSessionStore.connect() must be called before use")
        return self._conn

    async def create(self, session: AuthSession) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT OR REPLACE INTO auth_sessions (session_token_hash, expires_at, data) VALUES (?, ?, ?)",
                (session.session_token_hash, session.expires_at.isoformat(), session.model_dump_json()),
            )

    async def get(self, session_token_hash: str) -> AuthSession | None:
        cursor = await self._connection.execute(
            "SELECT data FROM auth_sessions WHERE session_token_hash = ?", (session_token_hash,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return AuthSession.model_validate_json(row["data"]) if row else None

    async def delete(self, session_token_hash: str) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "DELETE FROM auth_sessions WHERE session_token_hash = ?", (session_token_hash,)
            )

    async def delete_expired(self, now: datetime) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(),))
