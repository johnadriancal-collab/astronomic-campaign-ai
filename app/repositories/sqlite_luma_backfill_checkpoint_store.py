"""
SQLite-backed LumaBackfillCheckpointStore. Single row per checkpoint_id
(always "default" today, single-calendar scope) -- JSON-blob convention,
same as every other Luma store.
"""

from pathlib import Path

import aiosqlite

from app.models.luma import LumaBackfillCheckpoint
from app.repositories.luma_backfill_checkpoint_store import DEFAULT_CHECKPOINT_ID, LumaBackfillCheckpointStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS luma_backfill_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""


class SQLiteLumaBackfillCheckpointStore(LumaBackfillCheckpointStore):
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
            raise RuntimeError("SQLiteLumaBackfillCheckpointStore.connect() must be called before use")
        return self._conn

    async def save(self, checkpoint: LumaBackfillCheckpoint) -> None:
        await self._connection.execute(
            """
            INSERT INTO luma_backfill_checkpoints (checkpoint_id, data) VALUES (?, ?)
            ON CONFLICT(checkpoint_id) DO UPDATE SET data = excluded.data
            """,
            (checkpoint.checkpoint_id, checkpoint.model_dump_json()),
        )
        await self._connection.commit()

    async def get(self, checkpoint_id: str = DEFAULT_CHECKPOINT_ID) -> LumaBackfillCheckpoint | None:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_backfill_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return LumaBackfillCheckpoint.model_validate_json(row["data"]) if row else None
