"""SQLite-backed MailLeadStartTriggerStore -- real columns, not a JSON
blob (matching sqlite_mail_campaign_csv_prospect_link_store.py's reasoning:
small, structural, no PII). `weekdays` is the one list-shaped field; stored
as a JSON-encoded TEXT column (there is no existing "list inside an
otherwise-real-columns row" precedent in this codebase to match instead --
sqlite_mail_campaign_mailbox_store.py's list-of-mailboxes is a genuine
many-to-many join table, one row per (campaign, mailbox), which doesn't fit
here: `weekdays` is a single trigger's own field, not a separate owned
relation, per the approved Trigger design's own MailLeadStartTrigger shape).

Stage 5A (2026-09-04): a brand-new table, no prior deployed shape to
accommodate -- CREATE TABLE IF NOT EXISTS is trivially idempotent, no
separate migration step needed."""

import json
from datetime import datetime, time

import aiosqlite

from app.models.mail import MailLeadStartTrigger
from app.repositories.mail_lead_start_trigger_store import (
    MailLeadStartTriggerNotFoundError,
    MailLeadStartTriggerStore,
)
from app.repositories.sqlite_connection import open_sqlite_connection
from app.repositories.sqlite_txn import sqlite_write

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mail_lead_start_triggers (
    trigger_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    weekdays TEXT NOT NULL,
    local_time TEXT NOT NULL,
    leads_to_start INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_mail_lead_start_triggers_campaign
    ON mail_lead_start_triggers(mail_campaign_id)
"""


def _row_to_trigger(row: aiosqlite.Row) -> MailLeadStartTrigger:
    return MailLeadStartTrigger(
        trigger_id=row["trigger_id"],
        mail_campaign_id=row["mail_campaign_id"],
        weekdays=json.loads(row["weekdays"]),
        local_time=time.fromisoformat(row["local_time"]),
        leads_to_start=row["leads_to_start"],
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class SQLiteMailLeadStartTriggerStore(MailLeadStartTriggerStore):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await open_sqlite_connection(self._db_path)
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
            raise RuntimeError("SQLiteMailLeadStartTriggerStore.connect() must be called before use")
        return self._conn

    async def create(self, trigger: MailLeadStartTrigger) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute(
                "INSERT INTO mail_lead_start_triggers "
                "(trigger_id, mail_campaign_id, weekdays, local_time, leads_to_start, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trigger.trigger_id,
                    trigger.mail_campaign_id,
                    json.dumps(trigger.weekdays),
                    trigger.local_time.isoformat(),
                    trigger.leads_to_start,
                    int(trigger.enabled),
                    trigger.created_at.isoformat(),
                    trigger.updated_at.isoformat(),
                ),
            )

    async def get(self, trigger_id: str) -> MailLeadStartTrigger | None:
        cursor = await self._connection.execute(
            "SELECT * FROM mail_lead_start_triggers WHERE trigger_id = ?", (trigger_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _row_to_trigger(row) if row else None

    async def save(self, trigger: MailLeadStartTrigger) -> None:
        async with sqlite_write(self._connection):
            cursor = await self._connection.execute(
                "UPDATE mail_lead_start_triggers SET mail_campaign_id=?, weekdays=?, local_time=?, "
                "leads_to_start=?, enabled=?, updated_at=? WHERE trigger_id=?",
                (
                    trigger.mail_campaign_id,
                    json.dumps(trigger.weekdays),
                    trigger.local_time.isoformat(),
                    trigger.leads_to_start,
                    int(trigger.enabled),
                    trigger.updated_at.isoformat(),
                    trigger.trigger_id,
                ),
            )
        if cursor.rowcount == 0:
            raise MailLeadStartTriggerNotFoundError(trigger.trigger_id)

    async def delete(self, trigger_id: str) -> None:
        async with sqlite_write(self._connection):
            await self._connection.execute("DELETE FROM mail_lead_start_triggers WHERE trigger_id = ?", (trigger_id,))

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailLeadStartTrigger]:
        cursor = await self._connection.execute(
            "SELECT * FROM mail_lead_start_triggers WHERE mail_campaign_id = ? ORDER BY created_at",
            (mail_campaign_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_trigger(row) for row in rows]
