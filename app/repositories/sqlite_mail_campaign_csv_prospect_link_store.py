"""SQLite-backed MailCampaignCsvProspectLinkStore -- real columns, not a
JSON blob (unlike most stores in this codebase), specifically because this
row is small and structural enough that real columns make its "no PII,
no raw CSV data, just three ids and a timestamp" contract self-evident
from the schema itself -- see MailCampaignCsvProspectLink's own docstring.

A brand-new table with no prior deployed shape to accommodate -- unlike
MailEnrollmentBatch's own store (which had to ALTER TABLE an
already-shipped Stage 2 table), every column here is declared directly in
CREATE_TABLE_SQL. `CREATE TABLE IF NOT EXISTS` is trivially idempotent on
its own; no separate migration step exists or is needed."""

from datetime import datetime

import aiosqlite

from app.models.mail import MailCampaignCsvProspectLink
from app.repositories.mail_campaign_csv_prospect_link_store import (
    DuplicateCsvProspectLinkError,
    MailCampaignCsvProspectLinkStore,
)
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_campaign_csv_prospect_links (
    mail_campaign_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    import_batch_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (mail_campaign_id, idempotency_key)
)
"""


class SQLiteMailCampaignCsvProspectLinkStore(MailCampaignCsvProspectLinkStore):
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
            raise RuntimeError("SQLiteMailCampaignCsvProspectLinkStore.connect() must be called before use")
        return self._conn

    async def create(self, link: MailCampaignCsvProspectLink) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mail_campaign_csv_prospect_links "
                    "(mail_campaign_id, idempotency_key, import_batch_id, created_at) VALUES (?, ?, ?, ?)",
                    (link.mail_campaign_id, link.idempotency_key, link.import_batch_id, link.created_at.isoformat()),
                )
        except aiosqlite.IntegrityError as e:
            raise DuplicateCsvProspectLinkError(link.mail_campaign_id, link.idempotency_key) from e

    async def get_by_idempotency_key(
        self, mail_campaign_id: str, idempotency_key: str
    ) -> MailCampaignCsvProspectLink | None:
        cursor = await self._connection.execute(
            "SELECT mail_campaign_id, idempotency_key, import_batch_id, created_at "
            "FROM mail_campaign_csv_prospect_links WHERE mail_campaign_id = ? AND idempotency_key = ?",
            (mail_campaign_id, idempotency_key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return MailCampaignCsvProspectLink(
            mail_campaign_id=row["mail_campaign_id"],
            idempotency_key=row["idempotency_key"],
            import_batch_id=row["import_batch_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
