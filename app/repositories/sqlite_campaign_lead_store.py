"""
SQLite-backed CampaignLeadStore. `campaign_leads` has a composite primary
key (campaign_id, lead_id) -- add() uses INSERT OR IGNORE, so re-adding an
existing membership during a rebuild is a harmless no-op rather than an
error, matching CampaignLeadStore's documented contract.

`claude_score`/`claude_reason` were added here (moved off Lead) after this
table already had real rows in local dev/testing -- connect() migrates an
existing table safely via `ALTER TABLE ... ADD COLUMN` (checked against
PRAGMA table_info first, so it's idempotent and never destroys existing
rows), rather than dropping/recreating the table.
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.lead import CampaignLead, CampaignLeadStatus
from app.repositories.campaign_lead_store import CampaignLeadStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS campaign_leads (
    campaign_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    status TEXT NOT NULL,
    added_at TEXT NOT NULL,
    claude_score REAL,
    claude_reason TEXT,
    PRIMARY KEY (campaign_id, lead_id)
)
"""


class SQLiteCampaignLeadStore(CampaignLeadStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._migrate_add_score_columns()
        await self._conn.commit()

    async def _migrate_add_score_columns(self) -> None:
        """
        Safe for a table that already existed before claude_score/
        claude_reason were added to the schema -- adds only the columns
        that are actually missing, never touches existing rows/columns.
        A fresh table already has them from CREATE_TABLE_SQL above, so
        this is a no-op there.
        """
        cursor = await self._conn.execute("PRAGMA table_info(campaign_leads)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()

        if "claude_score" not in existing_columns:
            await self._conn.execute("ALTER TABLE campaign_leads ADD COLUMN claude_score REAL")
        if "claude_reason" not in existing_columns:
            await self._conn.execute("ALTER TABLE campaign_leads ADD COLUMN claude_reason TEXT")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteCampaignLeadStore.connect() must be called before use")
        return self._conn

    async def add(self, campaign_lead: CampaignLead) -> None:
        await self._connection.execute(
            """
            INSERT OR IGNORE INTO campaign_leads
                (campaign_id, lead_id, status, added_at, claude_score, claude_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_lead.campaign_id,
                campaign_lead.lead_id,
                campaign_lead.status.value,
                campaign_lead.added_at.isoformat(),
                campaign_lead.claude_score,
                campaign_lead.claude_reason,
            ),
        )
        await self._connection.commit()

    async def list_for_campaign(self, campaign_id: str) -> list[CampaignLead]:
        cursor = await self._connection.execute(
            "SELECT campaign_id, lead_id, status, added_at, claude_score, claude_reason "
            "FROM campaign_leads WHERE campaign_id = ?",
            (campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_model(row) for row in rows]

    async def list_for_lead(self, lead_id: str) -> list[CampaignLead]:
        cursor = await self._connection.execute(
            "SELECT campaign_id, lead_id, status, added_at, claude_score, claude_reason "
            "FROM campaign_leads WHERE lead_id = ?",
            (lead_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: aiosqlite.Row) -> CampaignLead:
        return CampaignLead(
            campaign_id=row["campaign_id"],
            lead_id=row["lead_id"],
            status=CampaignLeadStatus(row["status"]),
            added_at=row["added_at"],
            claude_score=row["claude_score"],
            claude_reason=row["claude_reason"],
        )
