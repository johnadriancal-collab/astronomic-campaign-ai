"""
SQLite-backed EngagementStore -- JSON blob (full Engagement via
model_dump_json()) plus denormalized, indexed columns: `client_id` (every
Client detail page load needs list_for_client), `luma_event_id` (Stage
1H-A, 2026-09-09, needed for get_by_luma_event_id() and the partial
unique index enforcing at-most-one-Engagement-per-Luma-event -- the
approved product decision for the Luma <-> Engagement link), and, as of
the Sale Bot -> AstroHub onboarding integration (2026-09-15), `sale_id`
and `docusign_envelope_id` (same shape: each needs its own fast lookup --
get_by_sale_id()/get_by_docusign_envelope_id() -- and its own partial
unique index, since sale_id is the onboarding integration's primary
idempotency key and docusign_envelope_id is its secondary
one-envelope-per-Engagement safeguard).

Stage 1A (2026-09-07): a brand-new table, no prior deployed shape to
accommodate. Stage 1E.1 (2026-09-08) is the first time this DOES need to
accommodate a prior deployed shape -- see engagement_taxonomy_migration.py's
own module docstring for why that migration runs synchronously here, in
connect(), before this store (or the app) is usable. Stage 1H-A adds a
second, independent migration -- _migrate_add_luma_event_id_column() --
using the exact same "ALTER TABLE ... ADD COLUMN, checked against
PRAGMA table_info first" idempotent convention already used by
sqlite_campaign_lead_store.py and sqlite_mail_enrollment_batch_store.py.
Every existing production row's `luma_event_id` is backfilled from its own
JSON `data` blob via SQLite's JSON1 `json_extract()` (already used
elsewhere, see sqlite_mail_enrollment_store.py) -- in practice this is
always NULL today (Engagement.luma_event_id has been a reserved,
never-written field since Stage 1E), but the backfill is written
unconditionally-safe regardless, not "safe because every row is empty."
"""

import aiosqlite

from app.models.client_crm import Engagement
from app.repositories.engagement_store import (
    EngagementExternalIdConflictError,
    EngagementLumaEventAlreadyLinkedError,
    EngagementNotFoundError,
    EngagementStore,
)
from app.repositories.engagement_taxonomy_migration import migrate_legacy_dinner_taxonomy_rows
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engagements (
    engagement_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

CREATE_CLIENT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_engagements_client ON engagements(client_id)
"""


def _row_to_engagement(row: aiosqlite.Row) -> Engagement:
    return Engagement.model_validate_json(row["data"])


class SQLiteEngagementStore(EngagementStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
        await self._conn.execute(CREATE_TABLE_SQL)
        await self._conn.execute(CREATE_CLIENT_INDEX_SQL)
        await self._migrate_add_luma_event_id_column()
        await self._migrate_add_sale_onboarding_columns()
        await self._conn.commit()
        # Must run BEFORE this store (or the app) serves any request --
        # see engagement_taxonomy_migration.py's own module docstring for
        # why this can't safely be a separate, later-timed step.
        await migrate_legacy_dinner_taxonomy_rows(self._conn)

    async def _migrate_add_luma_event_id_column(self) -> None:
        """Idempotent -- same "PRAGMA table_info first" convention as
        sqlite_campaign_lead_store.py/sqlite_mail_enrollment_batch_store.py.
        Adds the column (if missing) and backfills it from each row's own
        JSON `data` in the same pass (a fresh table's column is always
        empty, so the backfill is a harmless no-op there), then creates
        the plain index and the partial unique index -- both `CREATE
        INDEX IF NOT EXISTS`, so safe to rerun on every startup regardless
        of whether the column already existed."""
        cursor = await self._conn.execute("PRAGMA table_info(engagements)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()

        if "luma_event_id" not in existing_columns:
            await self._conn.execute("ALTER TABLE engagements ADD COLUMN luma_event_id TEXT")
            await self._conn.execute(
                "UPDATE engagements SET luma_event_id = json_extract(data, '$.luma_event_id') "
                "WHERE json_extract(data, '$.luma_event_id') IS NOT NULL"
            )

        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_engagements_luma_event ON engagements(luma_event_id)")
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_engagements_unique_luma_event "
            "ON engagements(luma_event_id) WHERE luma_event_id IS NOT NULL"
        )

    async def _migrate_add_sale_onboarding_columns(self) -> None:
        """Sale Bot -> AstroHub onboarding (2026-09-15) -- same idempotent
        "PRAGMA table_info first" convention as
        _migrate_add_luma_event_id_column() above, for the same reason:
        `sale_id` (primary idempotency key) and `docusign_envelope_id`
        (secondary uniqueness safeguard) both need an indexed column and a
        partial unique index, not just a JSON-blob field, to make
        get_by_sale_id()/get_by_docusign_envelope_id() fast and to enforce
        uniqueness even under concurrent/duplicate requests. Both columns
        are always NULL on every existing row today (these fields didn't
        exist before this stage), so the backfill is a harmless no-op in
        practice, same as luma_event_id's own backfill was -- written
        unconditionally-safe regardless."""
        cursor = await self._conn.execute("PRAGMA table_info(engagements)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()

        if "sale_id" not in existing_columns:
            await self._conn.execute("ALTER TABLE engagements ADD COLUMN sale_id TEXT")
            await self._conn.execute(
                "UPDATE engagements SET sale_id = json_extract(data, '$.sale_id') "
                "WHERE json_extract(data, '$.sale_id') IS NOT NULL"
            )
        if "docusign_envelope_id" not in existing_columns:
            await self._conn.execute("ALTER TABLE engagements ADD COLUMN docusign_envelope_id TEXT")
            await self._conn.execute(
                "UPDATE engagements SET docusign_envelope_id = json_extract(data, '$.docusign_envelope_id') "
                "WHERE json_extract(data, '$.docusign_envelope_id') IS NOT NULL"
            )

        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_engagements_sale_id ON engagements(sale_id)")
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_engagements_unique_sale_id "
            "ON engagements(sale_id) WHERE sale_id IS NOT NULL"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_engagements_docusign_envelope ON engagements(docusign_envelope_id)"
        )
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_engagements_unique_docusign_envelope "
            "ON engagements(docusign_envelope_id) WHERE docusign_envelope_id IS NOT NULL"
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteEngagementStore.connect() must be called before use")
        return self._conn

    async def create(self, engagement: Engagement) -> None:
        try:
            async with sqlite_write(self._connection):
                await self._connection.execute(
                    "INSERT INTO engagements "
                    "(engagement_id, client_id, created_at, updated_at, data, luma_event_id, sale_id, docusign_envelope_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        engagement.engagement_id,
                        engagement.client_id,
                        engagement.created_at.isoformat(),
                        engagement.updated_at.isoformat(),
                        engagement.model_dump_json(),
                        engagement.luma_event_id,
                        engagement.sale_id,
                        engagement.docusign_envelope_id,
                    ),
                )
        except aiosqlite.IntegrityError as exc:
            self._raise_for_integrity_error(engagement, exc)

    async def get(self, engagement_id: str) -> Engagement | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE engagement_id = ?", (engagement_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_engagement(row) if row else None

    async def save(self, engagement: Engagement) -> None:
        try:
            async with sqlite_write(self._connection):
                cursor = await self._connection.execute(
                    "UPDATE engagements SET client_id = ?, updated_at = ?, data = ?, luma_event_id = ?, "
                    "sale_id = ?, docusign_envelope_id = ? WHERE engagement_id = ?",
                    (
                        engagement.client_id,
                        engagement.updated_at.isoformat(),
                        engagement.model_dump_json(),
                        engagement.luma_event_id,
                        engagement.sale_id,
                        engagement.docusign_envelope_id,
                        engagement.engagement_id,
                    ),
                )
        except aiosqlite.IntegrityError as exc:
            self._raise_for_integrity_error(engagement, exc)
        if cursor.rowcount == 0:
            raise EngagementNotFoundError(engagement.engagement_id)

    @staticmethod
    def _raise_for_integrity_error(engagement: Engagement, exc: aiosqlite.IntegrityError) -> None:
        """A single IntegrityError on this table can come from any of
        three independent partial unique indexes (luma_event_id, sale_id,
        docusign_envelope_id) -- SQLite's own error message names the
        column that actually collided (e.g. "UNIQUE constraint failed:
        engagements.sale_id"), so disambiguate on that rather than
        guessing which one it must have been from context. Falls back to
        re-raising the original error unchanged if the message doesn't
        name a column this store recognizes, rather than misreporting an
        unrelated integrity failure as one of these three."""
        message = str(exc)
        if "engagements.sale_id" in message and engagement.sale_id is not None:
            raise EngagementExternalIdConflictError("sale_id", engagement.sale_id) from exc
        if "engagements.docusign_envelope_id" in message and engagement.docusign_envelope_id is not None:
            raise EngagementExternalIdConflictError("docusign_envelope_id", engagement.docusign_envelope_id) from exc
        if engagement.luma_event_id is not None:
            raise EngagementLumaEventAlreadyLinkedError(engagement.luma_event_id) from exc
        raise

    async def list_for_client(self, client_id: str) -> list[Engagement]:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE client_id = ? ORDER BY created_at", (client_id,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_engagement(row) for row in rows]

    async def get_by_luma_event_id(self, luma_event_id: str) -> Engagement | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE luma_event_id = ?", (luma_event_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_engagement(row) if row else None

    async def get_by_sale_id(self, sale_id: str) -> Engagement | None:
        cursor = await self._connection.execute("SELECT * FROM engagements WHERE sale_id = ?", (sale_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_engagement(row) if row else None

    async def get_by_docusign_envelope_id(self, docusign_envelope_id: str) -> Engagement | None:
        cursor = await self._connection.execute(
            "SELECT * FROM engagements WHERE docusign_envelope_id = ?", (docusign_envelope_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_engagement(row) if row else None

    async def list_for_clients(self, client_ids: list[str]) -> list[Engagement]:
        if not client_ids:
            return []
        placeholders = ",".join("?" for _ in client_ids)
        cursor = await self._connection.execute(
            f"SELECT * FROM engagements WHERE client_id IN ({placeholders})", tuple(client_ids)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_engagement(row) for row in rows]

    async def list_by_ids(self, engagement_ids: list[str]) -> list[Engagement]:
        if not engagement_ids:
            return []
        placeholders = ",".join("?" for _ in engagement_ids)
        cursor = await self._connection.execute(
            f"SELECT * FROM engagements WHERE engagement_id IN ({placeholders})", tuple(engagement_ids)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_engagement(row) for row in rows]
