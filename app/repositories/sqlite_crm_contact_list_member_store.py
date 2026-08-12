"""
SQLite-backed CrmContactListMemberStore. `crm_contact_list_members` has a
composite primary key (list_id, crm_contact_id) -- add() uses INSERT OR
IGNORE, so re-adding an existing membership is a harmless no-op rather than
an error, matching CrmContactListMemberStore's documented contract (same
pattern as sqlite_campaign_lead_store.py).
"""

from pathlib import Path

import aiosqlite

from app.models.crm import CrmContactListMembership
from app.repositories.crm_contact_list_member_store import CrmContactListMemberStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crm_contact_list_members (
    list_id TEXT NOT NULL,
    crm_contact_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, crm_contact_id)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crm_contact_list_members_contact
    ON crm_contact_list_members(crm_contact_id)
"""


class SQLiteCrmContactListMemberStore(CrmContactListMemberStore):
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
            raise RuntimeError("SQLiteCrmContactListMemberStore.connect() must be called before use")
        return self._conn

    async def add(self, membership: CrmContactListMembership) -> bool:
        cursor = await self._connection.execute(
            "INSERT OR IGNORE INTO crm_contact_list_members (list_id, crm_contact_id, added_at) VALUES (?, ?, ?)",
            (membership.list_id, membership.crm_contact_id, membership.added_at.isoformat()),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def remove(self, list_id: str, crm_contact_id: str) -> bool:
        cursor = await self._connection.execute(
            "DELETE FROM crm_contact_list_members WHERE list_id = ? AND crm_contact_id = ?",
            (list_id, crm_contact_id),
        )
        await self._connection.commit()
        return cursor.rowcount > 0

    async def remove_all_for_list(self, list_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM crm_contact_list_members WHERE list_id = ?", (list_id,)
        )
        await self._connection.commit()

    async def list_contact_ids_for_list(self, list_id: str) -> list[str]:
        cursor = await self._connection.execute(
            "SELECT crm_contact_id FROM crm_contact_list_members WHERE list_id = ?", (list_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["crm_contact_id"] for row in rows]

    async def list_ids_for_contact(self, crm_contact_id: str) -> list[str]:
        cursor = await self._connection.execute(
            "SELECT list_id FROM crm_contact_list_members WHERE crm_contact_id = ?", (crm_contact_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["list_id"] for row in rows]

    async def count_by_list(self) -> dict[str, int]:
        cursor = await self._connection.execute(
            "SELECT list_id, COUNT(*) AS n FROM crm_contact_list_members GROUP BY list_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {row["list_id"]: row["n"] for row in rows}
