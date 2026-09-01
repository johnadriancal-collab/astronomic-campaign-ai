"""SQLite-backed MailCampaignStore -- JSON blob, no unique constraint on name."""

import aiosqlite

from app.models.mail import MailCampaign
from app.repositories.mail_campaign_store import MailCampaignNotFoundError, MailCampaignStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_campaigns (
    mail_campaign_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteMailCampaignStore(MailCampaignStore):
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
            raise RuntimeError("SQLiteMailCampaignStore.connect() must be called before use")
        return self._conn

    async def create(self, campaign: MailCampaign) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO mail_campaigns (mail_campaign_id, created_at, data) VALUES (?, ?, ?)",
                    (campaign.mail_campaign_id, campaign.created_at.isoformat(), campaign.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"MailCampaign already exists: {e}") from e

    async def get(self, mail_campaign_id: str) -> MailCampaign | None:
        cursor = await self._connection.execute(
            "SELECT data FROM mail_campaigns WHERE mail_campaign_id = ?", (mail_campaign_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return MailCampaign.model_validate_json(row["data"]) if row else None

    async def save(self, campaign: MailCampaign) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_campaigns SET data = ? WHERE mail_campaign_id = ?",
                (campaign.model_dump_json(), campaign.mail_campaign_id),
            )
        if cursor.rowcount == 0:
            raise MailCampaignNotFoundError(campaign.mail_campaign_id)

    async def list(self) -> list[MailCampaign]:
        cursor = await self._connection.execute("SELECT data FROM mail_campaigns ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [MailCampaign.model_validate_json(row["data"]) for row in rows]
