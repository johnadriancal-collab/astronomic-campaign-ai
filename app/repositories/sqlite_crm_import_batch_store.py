"""SQLite-backed CrmImportBatchStore -- JSON blob, same convention as SQLiteCampaignStore."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.crm import CrmImportBatch
from app.repositories.crm_import_batch_store import CrmImportBatchNotFoundError, CrmImportBatchStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crm_import_batches (
    import_batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteCrmImportBatchStore(CrmImportBatchStore):
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
            raise RuntimeError("SQLiteCrmImportBatchStore.connect() must be called before use")
        return self._conn

    async def create(self, batch: CrmImportBatch) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.execute(
                "INSERT INTO crm_import_batches (import_batch_id, status, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch.import_batch_id, batch.status.value, now, now, batch.model_dump_json()),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"CrmImportBatch already exists: {batch.import_batch_id}") from e

    async def get(self, import_batch_id: str) -> CrmImportBatch | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_import_batches WHERE import_batch_id = ?", (import_batch_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmImportBatch.model_validate_json(row["data"]) if row else None

    async def save(self, batch: CrmImportBatch) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._connection.execute(
            "UPDATE crm_import_batches SET status = ?, updated_at = ?, data = ? WHERE import_batch_id = ?",
            (batch.status.value, now, batch.model_dump_json(), batch.import_batch_id),
        )
        await self._connection.commit()
        if cursor.rowcount == 0:
            raise CrmImportBatchNotFoundError(batch.import_batch_id)
