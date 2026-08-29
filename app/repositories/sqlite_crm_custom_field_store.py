"""SQLite-backed CrmCustomFieldStore -- JSON blob, UNIQUE on field_key."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.crm import CrmCustomFieldDefinition
from app.repositories.crm_custom_field_store import CrmCustomFieldNotFoundError, CrmCustomFieldStore
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crm_custom_fields (
    crm_custom_field_id TEXT PRIMARY KEY,
    field_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteCrmCustomFieldStore(CrmCustomFieldStore):
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
            raise RuntimeError("SQLiteCrmCustomFieldStore.connect() must be called before use")
        return self._conn

    async def create(self, definition: CrmCustomFieldDefinition) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO crm_custom_fields (crm_custom_field_id, field_key, created_at, updated_at, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (definition.crm_custom_field_id, definition.field_key, now, now, definition.model_dump_json()),
                )
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"CrmCustomFieldDefinition already exists (id or field_key): {e}") from e

    async def get(self, crm_custom_field_id: str) -> CrmCustomFieldDefinition | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_custom_fields WHERE crm_custom_field_id = ?", (crm_custom_field_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmCustomFieldDefinition.model_validate_json(row["data"]) if row else None

    async def get_by_field_key(self, field_key: str) -> CrmCustomFieldDefinition | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_custom_fields WHERE field_key = ?", (field_key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmCustomFieldDefinition.model_validate_json(row["data"]) if row else None

    async def save(self, definition: CrmCustomFieldDefinition) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE crm_custom_fields SET updated_at = ?, data = ? WHERE crm_custom_field_id = ?",
                (now, definition.model_dump_json(), definition.crm_custom_field_id),
            )
        if cursor.rowcount == 0:
            raise CrmCustomFieldNotFoundError(definition.crm_custom_field_id)

    async def list(self) -> list[CrmCustomFieldDefinition]:
        cursor = await self._connection.execute("SELECT data FROM crm_custom_fields ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [CrmCustomFieldDefinition.model_validate_json(row["data"]) for row in rows]
