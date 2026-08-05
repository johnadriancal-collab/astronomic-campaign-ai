"""
SQLite-backed CrmContactStore. Same JSON-blob convention as
SQLiteCampaignStore -- CrmContact stored whole via `model_dump_json()` so
new fields (more thesis questions, anything else) never need a migration.
`email_normalized`/`linkedin_normalized`/`apollo_contact_id` are
denormalized, nullable-UNIQUE columns (SQLite allows multiple NULLs in a
UNIQUE column, same convention as EmailMessageEvent.apollo_event_id) --
this is what makes the three confident dedup tiers fast lookups instead of
a full-table deserialize-and-scan, AND a real backstop against a duplicate
slipping through if a caller ever bypasses the service-level check.
`name_company_normalized` is indexed but NOT unique -- the fallback tier
can legitimately have more than one match, since it's never a confident
merge target.
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.models.crm import CrmContact, normalize_email, normalize_linkedin_url, normalize_name_company
from app.repositories.crm_contact_store import CrmContactNotFoundError, CrmContactStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crm_contacts (
    crm_contact_id TEXT PRIMARY KEY,
    email_normalized TEXT UNIQUE,
    apollo_contact_id TEXT UNIQUE,
    linkedin_normalized TEXT UNIQUE,
    name_company_normalized TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crm_contacts_name_company ON crm_contacts(name_company_normalized)
"""


class SQLiteCrmContactStore(CrmContactStore):
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
            raise RuntimeError("SQLiteCrmContactStore.connect() must be called before use")
        return self._conn

    async def create(self, contact: CrmContact) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._connection.execute(
                """
                INSERT INTO crm_contacts
                    (crm_contact_id, email_normalized, apollo_contact_id, linkedin_normalized,
                     name_company_normalized, created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact.crm_contact_id,
                    normalize_email(contact.email),
                    contact.apollo_contact_id,
                    normalize_linkedin_url(contact.linkedin_url),
                    normalize_name_company(contact.first_name, contact.last_name, contact.company),
                    now,
                    now,
                    contact.model_dump_json(),
                ),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"CrmContact already exists (duplicate email/apollo_contact_id/linkedin_url): {e}") from e

    async def get(self, crm_contact_id: str) -> CrmContact | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contacts WHERE crm_contact_id = ?", (crm_contact_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmContact.model_validate_json(row["data"]) if row else None

    async def save(self, contact: CrmContact) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = await self._connection.execute(
                """
                UPDATE crm_contacts
                SET email_normalized = ?, apollo_contact_id = ?, linkedin_normalized = ?,
                    name_company_normalized = ?, updated_at = ?, data = ?
                WHERE crm_contact_id = ?
                """,
                (
                    normalize_email(contact.email),
                    contact.apollo_contact_id,
                    normalize_linkedin_url(contact.linkedin_url),
                    normalize_name_company(contact.first_name, contact.last_name, contact.company),
                    now,
                    contact.model_dump_json(),
                    contact.crm_contact_id,
                ),
            )
            await self._connection.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"Update would duplicate an existing email/apollo_contact_id/linkedin_url: {e}") from e
        if cursor.rowcount == 0:
            raise CrmContactNotFoundError(contact.crm_contact_id)

    async def get_by_email(self, normalized_email: str) -> CrmContact | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contacts WHERE email_normalized = ?", (normalized_email,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmContact.model_validate_json(row["data"]) if row else None

    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> CrmContact | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contacts WHERE apollo_contact_id = ?", (apollo_contact_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmContact.model_validate_json(row["data"]) if row else None

    async def get_by_linkedin_url(self, normalized_linkedin_url: str) -> CrmContact | None:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contacts WHERE linkedin_normalized = ?", (normalized_linkedin_url,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return CrmContact.model_validate_json(row["data"]) if row else None

    async def find_by_name_and_company(self, normalized_name_company: str) -> list[CrmContact]:
        cursor = await self._connection.execute(
            "SELECT data FROM crm_contacts WHERE name_company_normalized = ?", (normalized_name_company,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [CrmContact.model_validate_json(row["data"]) for row in rows]

    async def list(self) -> list[CrmContact]:
        # Declared LAST -- see crm_contact_store.py's module docstring for why
        # (a method literally named `list` shadows the builtin within the
        # rest of this class body once defined).
        cursor = await self._connection.execute("SELECT data FROM crm_contacts ORDER BY created_at")
        rows = await cursor.fetchall()
        await cursor.close()
        return [CrmContact.model_validate_json(row["data"]) for row in rows]
