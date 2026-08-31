"""
SQLite-backed MailCampaignMailboxStore. `mail_campaign_mailboxes` has a
composite primary key (mail_campaign_id, mailbox_id) -- matching
sqlite_crm_contact_list_member_store.py's exact convention.

replace_for_campaign() reads the campaign's current rows, deletes them, and
re-inserts the new set, ALL inside one `sqlite_write` block -- a single
commit/rollback, so a failure partway through (e.g. a bad id slipping past
the service layer) leaves the previous selection completely intact rather
than partially replaced. Surviving mailbox_ids keep their original
`added_at` rather than getting a fresh timestamp on every save.
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_campaign_mailboxes (
    mail_campaign_id TEXT NOT NULL,
    mailbox_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (mail_campaign_id, mailbox_id)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_campaign_mailboxes_mailbox
    ON mail_campaign_mailboxes(mailbox_id)
"""


class SQLiteMailCampaignMailboxStore(MailCampaignMailboxStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
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
            raise RuntimeError("SQLiteMailCampaignMailboxStore.connect() must be called before use")
        return self._conn

    async def list_mailbox_ids_for_campaign(self, mail_campaign_id: str) -> list[str]:
        cursor = await self._connection.execute(
            "SELECT mailbox_id FROM mail_campaign_mailboxes WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["mailbox_id"] for row in rows]

    async def replace_for_campaign(self, mail_campaign_id: str, mailbox_ids: list[str]) -> None:
        deduped = list(dict.fromkeys(mailbox_ids))
        now = datetime.now(timezone.utc).isoformat()

        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "SELECT mailbox_id, added_at FROM mail_campaign_mailboxes WHERE mail_campaign_id = ?",
                (mail_campaign_id,),
            )
            existing_added_at = {row["mailbox_id"]: row["added_at"] for row in await cursor.fetchall()}
            await cursor.close()

            await self._connection.execute(
                "DELETE FROM mail_campaign_mailboxes WHERE mail_campaign_id = ?", (mail_campaign_id,)
            )
            await self._connection.executemany(
                "INSERT INTO mail_campaign_mailboxes (mail_campaign_id, mailbox_id, added_at) VALUES (?, ?, ?)",
                [(mail_campaign_id, mailbox_id, existing_added_at.get(mailbox_id, now)) for mailbox_id in deduped],
            )
