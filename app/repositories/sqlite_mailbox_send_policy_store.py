"""
SQLite-backed MailboxSendPolicyStore -- same shared database_path as every
other store, same JSON-blob-per-row convention as MailboxCredential. A
missing row is a valid, expected state (see MailboxSendPolicy's own
docstring) -- `get()` simply returns None, `upsert()` is the one write path
(INSERT OR REPLACE, matching MailboxCredentialStore.create()'s exact
precedent for "there's only ever one row per key, so an upsert is the
right primitive, not a create()/save() pair").
"""

import aiosqlite

from app.models.mailbox import MailboxSendPolicy
from app.repositories.mailbox_send_policy_store import MailboxSendPolicyStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mailbox_send_policies (
    mailbox_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class SQLiteMailboxSendPolicyStore(MailboxSendPolicyStore):
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
            raise RuntimeError("SQLiteMailboxSendPolicyStore.connect() must be called before use")
        return self._conn

    async def get(self, mailbox_id: str) -> MailboxSendPolicy | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mailbox_send_policies WHERE mailbox_id = ?", (mailbox_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailboxSendPolicy.model_validate_json(row["data"]) if row else None

    async def upsert(self, policy: MailboxSendPolicy) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT OR REPLACE INTO mailbox_send_policies (mailbox_id, data) VALUES (?, ?)",
                (policy.mailbox_id, policy.model_dump_json()),
            )
