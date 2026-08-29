"""SQLite-backed MailboxStore -- JSON blob, same shared database_path as
every other store (see sqlite_mail_campaign_store.py). get_by_google_user_id/
get_by_email filter in Python over list() -- this app's existing,
established pattern for this data scale (a handful of connected mailboxes),
matching e.g. CrmService.get_list_contacts()'s in-memory pagination."""

from pathlib import Path

import aiosqlite

from app.models.mailbox import Mailbox
from app.repositories.mailbox_store import MailboxNotFoundError, MailboxStore
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT PRIMARY KEY,
    connected_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteMailboxStore(MailboxStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteMailboxStore.connect() must be called before use")
        return self._conn

    async def create(self, mailbox: Mailbox) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mailboxes (mailbox_id, connected_at, data) VALUES (?, ?, ?)",
                    (mailbox.mailbox_id, mailbox.connected_at.isoformat(), mailbox.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"Mailbox already exists: {e}") from e

    async def get(self, mailbox_id: str) -> Mailbox | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mailboxes WHERE mailbox_id = ?", (mailbox_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return Mailbox.model_validate_json(row["data"]) if row else None

    async def get_by_google_user_id(self, google_user_id: str) -> Mailbox | None:
        for mailbox in await self.list():
            if mailbox.google_user_id == google_user_id:
                return mailbox
        return None

    async def get_by_email(self, email: str) -> Mailbox | None:
        for mailbox in await self.list():
            if mailbox.email == email:
                return mailbox
        return None

    async def save(self, mailbox: Mailbox) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mailboxes SET data = ? WHERE mailbox_id = ?",
                (mailbox.model_dump_json(), mailbox.mailbox_id),
            )
        if cursor.rowcount == 0:
            raise MailboxNotFoundError(mailbox.mailbox_id)

    async def list(self) -> list[Mailbox]:
        cursor = await self._connection.execute("SELECT data FROM mailboxes ORDER BY connected_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [Mailbox.model_validate_json(row["data"]) for row in rows]
