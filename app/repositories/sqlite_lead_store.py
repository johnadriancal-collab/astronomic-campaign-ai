"""
SQLite-backed LeadStore. Same conventions as SQLiteCampaignStore: Lead is
stored as a JSON blob (model_dump_json()), not mapped into relational
columns, so it can gain fields freely with no schema migration.

`apollo_contact_id` is UNIQUE -- this is the hard, storage-level backstop
for "one Lead per Apollo contact, ever" (the application-level check is
LeadService.ensure_lead() calling get_by_apollo_contact_id() first).
"""

from datetime import datetime, timezone

import aiosqlite

from app.models.lead import Lead
from app.repositories.lead_store import LeadNotFoundError, LeadStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    workspace_id TEXT,
    apollo_contact_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteLeadStore(LeadStore):
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
            raise RuntimeError("SQLiteLeadStore.connect() must be called before use")
        return self._conn

    async def create(self, lead: Lead) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    """
                    INSERT INTO leads (lead_id, workspace_id, apollo_contact_id, created_at, updated_at, data)
                    VALUES (?, NULL, ?, ?, ?, ?)
                    """,
                    (lead.lead_id, lead.apollo_contact_id, now, now, lead.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(
                f"Lead already exists (lead_id or apollo_contact_id collision): {e}"
            ) from e

    async def get(self, lead_id: str) -> Lead | None:
        cursor = await self._connection.execute(
            "SELECT data FROM leads WHERE lead_id = ?", (lead_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Lead.model_validate_json(row["data"])

    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> Lead | None:
        cursor = await self._connection.execute(
            "SELECT data FROM leads WHERE apollo_contact_id = ?", (apollo_contact_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Lead.model_validate_json(row["data"])

    async def save(self, lead: Lead) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE leads SET updated_at = ?, data = ? WHERE lead_id = ?",
                (now, lead.model_dump_json(), lead.lead_id),
            )
        if cursor.rowcount == 0:
            raise LeadNotFoundError(lead.lead_id)

    async def list(self) -> list[Lead]:
        cursor = await self._connection.execute("SELECT data FROM leads ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [Lead.model_validate_json(row["data"]) for row in rows]
