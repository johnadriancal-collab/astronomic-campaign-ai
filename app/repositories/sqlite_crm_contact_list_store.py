"""SQLite-backed CrmContactListStore -- JSON blob, no unique constraint on name."""

from pathlib import Path

import aiosqlite

from app.models.crm import CrmContactList
from app.repositories.crm_contact_list_store import CrmContactListNotFoundError, CrmContactListStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crm_contact_lists (
    list_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteCrmContactListStore(CrmContactListStore):
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
            raise RuntimeError("SQLiteCrmContactListStore.connect() must be called before use")
        return self._conn

    async def create(self, contact_list: CrmContactList) -> None:
        try:
            await self._connection.execute(
                "INSERT INTO crm_contact_lists (list_id, created_at, updated_at, data) VALUES (?, ?, ?, ?)",
                (
                    contact_list.list_id,
                    contact_list.created_at.isoformat(),
                    contact_list.updated_at.isoformat(),
                    contact_list.model_dump_json(),
                ),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"CrmContactList already exists: {e}") from e

    async def get(self, list_id: str) -> CrmContactList | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contact_lists WHERE list_id = ?", (list_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmContactList.model_validate_json(row["data"]) if row else None

    async def save(self, contact_list: CrmContactList) -> None:
        cursor = await self._connection.execute(
            "UPDATE crm_contact_lists SET updated_at = ?, data = ? WHERE list_id = ?",
            (contact_list.updated_at.isoformat(), contact_list.model_dump_json(), contact_list.list_id),
        )
        await self._connection.commit()
        if cursor.rowcount == 0:
            raise CrmContactListNotFoundError(contact_list.list_id)

    async def delete(self, list_id: str) -> None:
        await self._connection.execute("DELETE FROM crm_contact_lists WHERE list_id = ?", (list_id,))
        await self._connection.commit()

    async def list(self) -> list[CrmContactList]:
        # Declared LAST -- see crm_contact_store.py's module docstring for why.
        cursor = await self._connection.execute("SELECT data FROM crm_contact_lists ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [CrmContactList.model_validate_json(row["data"]) for row in rows]
