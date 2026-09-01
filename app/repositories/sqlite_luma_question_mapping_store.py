"""
SQLite-backed LumaQuestionMappingStore. Same JSON-blob convention as the
other Luma stores; `active` denormalized as its own column since that's
exactly what list(include_inactive=False) filters on.
"""

import aiosqlite

from app.models.luma import LumaQuestionMapping
from app.repositories.luma_question_mapping_store import LumaQuestionMappingStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS luma_question_mappings (
    luma_question_mapping_id TEXT PRIMARY KEY,
    active INTEGER NOT NULL,
    data TEXT NOT NULL
)
"""


class SQLiteLumaQuestionMappingStore(LumaQuestionMappingStore):
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
            raise RuntimeError("SQLiteLumaQuestionMappingStore.connect() must be called before use")
        return self._conn

    async def create(self, mapping: LumaQuestionMapping) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO luma_question_mappings (luma_question_mapping_id, active, data) VALUES (?, ?, ?)",
                (mapping.luma_question_mapping_id, int(mapping.active), mapping.model_dump_json()),
            )

    async def save(self, mapping: LumaQuestionMapping) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "UPDATE luma_question_mappings SET active = ?, data = ? WHERE luma_question_mapping_id = ?",
                (int(mapping.active), mapping.model_dump_json(), mapping.luma_question_mapping_id),
            )

    async def get(self, luma_question_mapping_id: str) -> LumaQuestionMapping | None:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_question_mappings WHERE luma_question_mapping_id = ?",
            (luma_question_mapping_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return LumaQuestionMapping.model_validate_json(row["data"]) if row else None

    async def list(self, include_inactive: bool = True) -> list[LumaQuestionMapping]:
        if include_inactive:
            cursor = await self._connection.execute("SELECT data FROM luma_question_mappings")
        else:
            cursor = await self._connection.execute(
                "SELECT data FROM luma_question_mappings WHERE active = 1"
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [LumaQuestionMapping.model_validate_json(row["data"]) for row in rows]
