"""SQLite-backed MailboxCredentialStore -- INTERNAL ONLY, same shared
database_path as every other store. `encrypted_refresh_token` is stored as
Fernet ciphertext text (see app/services/token_encryption.py) -- the
plaintext refresh token never touches this file."""

import aiosqlite

from app.models.mailbox import MailboxCredential
from app.repositories.mailbox_credential_store import MailboxCredentialStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mailbox_credentials (
    mailbox_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteMailboxCredentialStore(MailboxCredentialStore):
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
            raise RuntimeError("SQLiteMailboxCredentialStore.connect() must be called before use")
        return self._conn

    async def create(self, credential: MailboxCredential) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT OR REPLACE INTO mailbox_credentials (mailbox_id, created_at, data) VALUES (?, ?, ?)",
                (credential.mailbox_id, credential.created_at.isoformat(), credential.model_dump_json()),
            )

    async def get(self, mailbox_id: str) -> MailboxCredential | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mailbox_credentials WHERE mailbox_id = ?", (mailbox_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailboxCredential.model_validate_json(row["data"]) if row else None

    async def save(self, credential: MailboxCredential) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "UPDATE mailbox_credentials SET data = ? WHERE mailbox_id = ?",
                (credential.model_dump_json(), credential.mailbox_id),
            )

    async def delete(self, mailbox_id: str) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "DELETE FROM mailbox_credentials WHERE mailbox_id = ?", (mailbox_id,)
            )
