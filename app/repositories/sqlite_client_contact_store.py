"""
SQLite-backed ClientContactStore -- JSON blob (full ClientContact via
model_dump_json()) plus TWO denormalized, indexed columns:
`client_id` (every Client detail page load needs list_for_client) and
`crm_contact_id` (Stage 1D's dedup/linking flow, and a future "this
person's Client CRM relationships" panel on the CRM contact detail page,
both need list_for_crm_contact -- see ClientContactStore's own docstring).
Both are real, described access patterns, not speculative indexes.

Stage 1A (2026-09-07): a brand-new table, no prior deployed shape to
accommodate.
"""

from datetime import datetime, timezone

import aiosqlite

from app.models.client_crm import ClientContact
from app.repositories.client_contact_store import ClientContactNotFoundError, ClientContactStore
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client_contacts (
    client_contact_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    crm_contact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CLIENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_contacts_client ON client_contacts(client_id)
"""

CREATE_CRM_CONTACT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_contacts_crm_contact ON client_contacts(crm_contact_id)
"""


def _row_to_contact(row: aiosqlite.Row) -> ClientContact:
    return ClientContact.model_validate_json(row["data"])


class SQLiteClientContactStore(ClientContactStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CLIENT_INDEX_SQL)
        await self._conn.execute(CREATE_CRM_CONTACT_INDEX_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteClientContactStore.connect() must be called before use")
        return self._conn

    async def create(self, contact: ClientContact) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO client_contacts "
                "(client_contact_id, client_id, crm_contact_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    contact.client_contact_id,
                    contact.client_id,
                    contact.crm_contact_id,
                    contact.created_at.isoformat(),
                    contact.updated_at.isoformat(),
                    contact.model_dump_json(),
                ),
            )

    async def get(self, client_contact_id: str) -> ClientContact | None:
        cursor = await self._connection.execute(
            "SELECT * FROM client_contacts WHERE client_contact_id = ?", (client_contact_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_contact(row) if row else None

    async def save(self, contact: ClientContact) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE client_contacts SET client_id = ?, crm_contact_id = ?, updated_at = ?, data = ? "
                "WHERE client_contact_id = ?",
                (
                    contact.client_id,
                    contact.crm_contact_id,
                    contact.updated_at.isoformat(),
                    contact.model_dump_json(),
                    contact.client_contact_id,
                ),
            )
        if cursor.rowcount == 0:
            raise ClientContactNotFoundError(contact.client_contact_id)

    async def list_for_client(self, client_id: str) -> list[ClientContact]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_contacts WHERE client_id = ? ORDER BY created_at", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_contact(row) for row in rows]

    async def list_for_crm_contact(self, crm_contact_id: str) -> list[ClientContact]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_contacts WHERE crm_contact_id = ? ORDER BY created_at", (crm_contact_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_contact(row) for row in rows]

    async def _other_active_primaries(self, client_id: str, exclude_client_contact_id: str | None) -> list[ClientContact]:
        cursor = await self._connection.execute(
            "SELECT * FROM client_contacts WHERE client_id = ?", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        contacts = [_row_to_contact(row) for row in rows]
        return [
            c
            for c in contacts
            if not c.archived and c.is_primary_contact and c.client_contact_id != exclude_client_contact_id
        ]

    async def create_as_primary(self, contact: ClientContact) -> None:
        # Single connection/transaction: the INSERT of the new primary and
        # the UPDATEs clearing every other active primary for this Client
        # commit or roll back together, so there is never a window where
        # two rows are simultaneously primary for the same client_id.
        async with sqlite_write(self._connection):
            others = await self._other_active_primaries(contact.client_id, exclude_client_contact_id=None)
            for other in others:
                cleared = other.model_copy(update={"is_primary_contact": False})
                await self._connection.execute(
                    "UPDATE client_contacts SET data = ? WHERE client_contact_id = ?",
                    (cleared.model_dump_json(), cleared.client_contact_id),
                )
            await self._connection.execute(
                "INSERT INTO client_contacts "
                "(client_contact_id, client_id, crm_contact_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    contact.client_contact_id,
                    contact.client_id,
                    contact.crm_contact_id,
                    contact.created_at.isoformat(),
                    contact.updated_at.isoformat(),
                    contact.model_dump_json(),
                ),
            )

    async def set_primary(self, client_id: str, client_contact_id: str) -> ClientContact:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "SELECT * FROM client_contacts WHERE client_contact_id = ?", (client_contact_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            target = _row_to_contact(row) if row else None
            if target is None or target.client_id != client_id:
                raise ClientContactNotFoundError(client_contact_id)

            others = await self._other_active_primaries(client_id, exclude_client_contact_id=client_contact_id)
            for other in others:
                cleared = other.model_copy(update={"is_primary_contact": False})
                await self._connection.execute(
                    "UPDATE client_contacts SET data = ? WHERE client_contact_id = ?",
                    (cleared.model_dump_json(), cleared.client_contact_id),
                )

            updated = target.model_copy(update={"is_primary_contact": True, "updated_at": datetime.now(timezone.utc)})
            await self._connection.execute(
                "UPDATE client_contacts SET updated_at = ?, data = ? WHERE client_contact_id = ?",
                (updated.updated_at.isoformat(), updated.model_dump_json(), updated.client_contact_id),
            )
        return updated
