"""
SQLite-backed CampaignStore -- the persistent replacement for
MemoryCampaignStore. Implements the exact same CampaignStore interface, so
CampaignService and every route needs zero changes to use it.

Campaign is stored as a single JSON blob (`model_dump_json()`), not mapped
into relational columns -- this keeps the Campaign model itself completely
unchanged (see models/campaign.py) and means it can gain fields freely with
no schema migration. `status` is denormalized into its own column purely so
a future listing/filtering query doesn't need to deserialize every row.
`workspace_id` exists now, unused (always NULL), so per-workspace ownership
can be added later without an ALTER TABLE.
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.campaign import Campaign
from app.repositories.campaign_store import CampaignNotFoundError, CampaignStore
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    workspace_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteCampaignStore(CampaignStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Opens the connection and ensures the schema exists. Call once at app startup."""
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
            raise RuntimeError("SQLiteCampaignStore.connect() must be called before use")
        return self._conn

    async def create(self, campaign: Campaign) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    """
                    INSERT INTO campaigns (campaign_id, workspace_id, status, created_at, updated_at, data)
                    VALUES (?, NULL, ?, ?, ?, ?)
                    """,
                    (campaign.campaign_id, campaign.status.value, now, now, campaign.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"Campaign already exists: {campaign.campaign_id}") from e

    async def get(self, campaign_id: str) -> Campaign | None:
        cursor = await self._connection.execute(
            "SELECT data FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Campaign.model_validate_json(row["data"])

    async def save(self, campaign: Campaign) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE campaigns SET status = ?, updated_at = ?, data = ? WHERE campaign_id = ?",
                (campaign.status.value, now, campaign.model_dump_json(), campaign.campaign_id),
            )
        if cursor.rowcount == 0:
            raise CampaignNotFoundError(campaign.campaign_id)

    async def list(self) -> list[Campaign]:
        cursor = await self._connection.execute("SELECT data FROM campaigns ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [Campaign.model_validate_json(row["data"]) for row in rows]
