"""
SQLite-backed LumaRegistrationStore. Same JSON-blob-plus-denormalized-
columns convention as SQLiteCrmContactStore: the full LumaRegistration is
stored via `model_dump_json()`, with `luma_event_id`/`crm_contact_id`
pulled out as real indexed columns since those are exactly what a future
"registrations for this event" / "registrations for this contact" lookup
(and Astro tooling, later) will filter on.
"""

from pathlib import Path

import aiosqlite

from app.models.luma import LumaRegistration
from app.repositories.luma_registration_store import LumaRegistrationStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS luma_registrations (
    luma_guest_id TEXT PRIMARY KEY,
    luma_event_id TEXT NOT NULL,
    crm_contact_id TEXT,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_EVENT_SQL = """
CREATE INDEX IF NOT EXISTS idx_luma_registrations_event ON luma_registrations(luma_event_id)
"""

CREATE_INDEX_CONTACT_SQL = """
CREATE INDEX IF NOT EXISTS idx_luma_registrations_contact ON luma_registrations(crm_contact_id)
"""


class SQLiteLumaRegistrationStore(LumaRegistrationStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_INDEX_EVENT_SQL)
        await self._conn.execute(CREATE_INDEX_CONTACT_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteLumaRegistrationStore.connect() must be called before use")
        return self._conn

    async def save(self, registration: LumaRegistration) -> None:
        await self._connection.execute(
            """
            INSERT INTO luma_registrations (luma_guest_id, luma_event_id, crm_contact_id, updated_at, data)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(luma_guest_id) DO UPDATE SET
                luma_event_id = excluded.luma_event_id,
                crm_contact_id = excluded.crm_contact_id,
                updated_at = excluded.updated_at,
                data = excluded.data
            """,
            (
                registration.luma_guest_id,
                registration.luma_event_id,
                registration.crm_contact_id,
                registration.updated_at.isoformat(),
                registration.model_dump_json(),
            ),
        )
        await self._connection.commit()

    async def get(self, luma_guest_id: str) -> LumaRegistration | None:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_registrations WHERE luma_guest_id = ?", (luma_guest_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return LumaRegistration.model_validate_json(row["data"]) if row else None

    async def list_for_event(self, luma_event_id: str) -> list[LumaRegistration]:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_registrations WHERE luma_event_id = ?", (luma_event_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [LumaRegistration.model_validate_json(row["data"]) for row in rows]

    async def list_for_contact(self, crm_contact_id: str) -> list[LumaRegistration]:
        cursor = await self._connection.execute(
            "SELECT data FROM luma_registrations WHERE crm_contact_id = ?", (crm_contact_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [LumaRegistration.model_validate_json(row["data"]) for row in rows]

    async def list(self) -> list[LumaRegistration]:
        cursor = await self._connection.execute("SELECT data FROM luma_registrations")
        rows = await cursor.fetchall()
        await cursor.close()
        return [LumaRegistration.model_validate_json(row["data"]) for row in rows]
