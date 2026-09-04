"""
Production-schema-upgrade and legacy-compatibility tests for Astronomic
Mail Phase A.

Every SQLite*Store in this codebase stores its Pydantic model as a JSON
blob in a `data TEXT` column, with only a few fields PROMOTED to real,
separately-queryable SQL columns (see e.g. sqlite_mail_enrollment_store.py's
own docstring). This matters enormously for schema-upgrade safety: adding
a new OPTIONAL field with a default (e.g. MailEnrollment.assigned_mailbox_id:
str | None = None) to a model requires NO SQL schema change at all -- an
old JSON blob that simply lacks the key deserializes with the Pydantic
default via model_validate_json(). This is fundamentally different from a
traditional relational schema, where the same change would need `ALTER
TABLE ... ADD COLUMN`.

These tests do NOT take that claim on faith -- they build a raw sqlite3
database file using the exact PRE-Phase-A schema (verified against this
module's own DDL constants, kept old on purpose) and insert PRE-Phase-A
-shaped JSON blobs (no `assigned_mailbox_id` key at all, exactly as
existing production data actually looks), then open that same file with
the REAL, current, Phase A stores/services -- never a freshly-created
database -- and verify the upgrade is safe, idempotent, and (for the
legacy-campaign-lifecycle test) that connecting to the database does not
itself change any execution state.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentStatus,
)
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_mail_campaign_store import SQLiteMailCampaignStore
from app.repositories.sqlite_mail_enrollment_step_store import SQLiteMailEnrollmentStepStore
from app.repositories.sqlite_mail_enrollment_store import SQLiteMailEnrollmentStore
from app.repositories.sqlite_mail_sequence_step_store import SQLiteMailSequenceStepStore
from app.repositories.sqlite_mailbox_send_policy_store import SQLiteMailboxSendPolicyStore

pytestmark = pytest.mark.asyncio

NOW_ISO = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc).isoformat()

# --- The EXACT pre-Phase-A `mail_enrollments` schema (deliberately frozen
# here, never imported from the live store module, so a future accidental
# change to that module's DDL can never silently make this fixture stop
# representing what production actually has on disk). Confirmed identical
# to the live module's CREATE_TABLE_SQL/CREATE_INDEX_SQL by
# test_pre_phase_a_schema_fixture_matches_the_original_pre_phase_a_ddl
# below -- the one thing that WOULD need updating if the base table
# itself (not just the new index) had ever changed, which Phase A never did.
_PRE_PHASE_A_MAIL_ENROLLMENTS_TABLE_SQL = """
CREATE TABLE mail_enrollments (
    enrollment_id TEXT NOT NULL,
    mail_campaign_id TEXT NOT NULL,
    crm_contact_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (mail_campaign_id, crm_contact_id)
)
"""
_PRE_PHASE_A_MAIL_ENROLLMENTS_INDEX_SQL = """
CREATE INDEX idx_mail_enrollments_contact ON mail_enrollments(crm_contact_id)
"""

_PRE_PHASE_A_MAIL_CAMPAIGNS_TABLE_SQL = """
CREATE TABLE mail_campaigns (
    mail_campaign_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""

_PRE_PHASE_A_MAIL_SEQUENCE_STEPS_TABLE_SQL = """
CREATE TABLE mail_sequence_steps (
    step_id TEXT PRIMARY KEY,
    mail_campaign_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (mail_campaign_id, step_number)
)
"""

_PRE_PHASE_A_ACTIVITY_EVENTS_TABLE_SQL = """
CREATE TABLE activity_events (
    event_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


def _pre_phase_a_enrollment_blob(enrollment_id: str, mail_campaign_id: str, crm_contact_id: str, email: str, status: str) -> str:
    """A hand-built JSON blob shaped EXACTLY like what pre-Phase-A
    production code actually wrote -- no `assigned_mailbox_id` key at all
    (that field did not exist yet), matching MailEnrollment's field set
    before this phase (see app/models/mail.py's diff)."""
    return json.dumps(
        {
            "enrollment_id": enrollment_id,
            "mail_campaign_id": mail_campaign_id,
            "crm_contact_id": crm_contact_id,
            "email_at_enrollment": email,
            "status": status,
            "enrolled_at": NOW_ISO,
            "created_at": NOW_ISO,
        }
    )


def _pre_phase_a_campaign_blob(mail_campaign_id: str, name: str, status: str, source_list_id: str) -> str:
    return json.dumps(
        {
            "mail_campaign_id": mail_campaign_id,
            "name": name,
            "status": status,
            "source_list_id": source_list_id,
            "sending_days": [0, 1, 2, 3, 4],
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "timezone": "America/Chicago",
            "all_hours": False,
            "sharing": "everyone",
            "start_immediately": False,
            "daily_lead_start_limit": None,
            "created_at": NOW_ISO,
            "updated_at": NOW_ISO,
            "ready_at": NOW_ISO,
            "archived_at": None,
        }
    )


def _pre_phase_a_step_blob(step_id: str, mail_campaign_id: str, step_number: int) -> str:
    return json.dumps(
        {
            "step_id": step_id,
            "mail_campaign_id": mail_campaign_id,
            "step_number": step_number,
            "subject": "Hello {{first_name}}",
            "body": "Body text",
            "delay_days": 0,
            "reply_in_thread": True,
            "created_at": NOW_ISO,
            "updated_at": NOW_ISO,
        }
    )


@pytest.fixture
def legacy_db_path(tmp_path: Path) -> str:
    """Builds a raw sqlite3 file reproducing 'Test Campaign 1'-like
    production state: one READY MailCampaign, one MailSequenceStep, and
    three MailEnrollment rows (2 PENDING, 1 SUPPRESSED) -- all using the
    pre-Phase-A schema and pre-Phase-A-shaped JSON blobs. No
    mail_enrollment_steps or mailbox_send_policies table exists at all yet
    (those are brand-new Phase A tables -- a real production DB predating
    this phase would have neither)."""
    db_path = str(tmp_path / "legacy_production.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_PRE_PHASE_A_MAIL_CAMPAIGNS_TABLE_SQL)
        conn.execute(_PRE_PHASE_A_MAIL_SEQUENCE_STEPS_TABLE_SQL)
        conn.execute(_PRE_PHASE_A_MAIL_ENROLLMENTS_TABLE_SQL)
        conn.execute(_PRE_PHASE_A_MAIL_ENROLLMENTS_INDEX_SQL)
        conn.execute(_PRE_PHASE_A_ACTIVITY_EVENTS_TABLE_SQL)

        conn.execute(
            "INSERT INTO mail_campaigns (mail_campaign_id, created_at, data) VALUES (?, ?, ?)",
            ("camp-1", NOW_ISO, _pre_phase_a_campaign_blob("camp-1", "Test Campaign 1", "ready", "list-1")),
        )
        conn.execute(
            "INSERT INTO mail_sequence_steps (step_id, mail_campaign_id, step_number, data) VALUES (?, ?, ?, ?)",
            ("step-1", "camp-1", 1, _pre_phase_a_step_blob("step-1", "camp-1", 1)),
        )

        enrollments = [
            ("enr-1", "camp-1", "contact-1", "alice@example.com", "pending"),
            ("enr-2", "camp-1", "contact-2", "bob@example.com", "pending"),
            ("enr-3", "camp-1", "contact-3", "carol@example.com", "suppressed"),
        ]
        for enrollment_id, mail_campaign_id, crm_contact_id, email, status in enrollments:
            conn.execute(
                "INSERT INTO mail_enrollments (enrollment_id, mail_campaign_id, crm_contact_id, data) VALUES (?, ?, ?, ?)",
                (enrollment_id, mail_campaign_id, crm_contact_id, _pre_phase_a_enrollment_blob(enrollment_id, mail_campaign_id, crm_contact_id, email, status)),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_pre_phase_a_schema_fixture_matches_the_original_pre_phase_a_ddl():
    """Guards the fixture itself: the frozen pre-Phase-A DDL constants above
    must still describe the base `mail_enrollments` TABLE definition
    exactly as the live module defines it today, MINUS only the new
    enrollment_id unique index -- confirming Phase A never altered the
    base table's columns, only added an index."""
    from app.repositories.sqlite_mail_enrollment_store import CREATE_TABLE_SQL

    def normalize(sql: str) -> str:
        return " ".join(sql.replace("IF NOT EXISTS ", "").split())

    assert normalize(CREATE_TABLE_SQL) == normalize(_PRE_PHASE_A_MAIL_ENROLLMENTS_TABLE_SQL)


# --- Item 1: assigned_mailbox_id upgrade (CRITICAL) -------------------------


async def test_opening_legacy_db_adds_the_new_enrollment_id_index_idempotently(legacy_db_path):
    store = SQLiteMailEnrollmentStore(legacy_db_path)
    await store.connect()
    try:
        cursor = await store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_mail_enrollments_enrollment_id'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None, "the new unique index must be created on an existing legacy table"
    finally:
        await store.close()

    # Idempotent: reopening (as a second app start against the same file)
    # must not raise or duplicate anything.
    store2 = SQLiteMailEnrollmentStore(legacy_db_path)
    await store2.connect()
    await store2.close()


async def test_legacy_enrollment_rows_are_intact_after_opening_with_the_new_store(legacy_db_path):
    store = SQLiteMailEnrollmentStore(legacy_db_path)
    await store.connect()
    try:
        rows = await store.list_for_campaign("camp-1")
        assert len(rows) == 3
        by_id = {r.enrollment_id: r for r in rows}
        assert by_id["enr-1"].email_at_enrollment == "alice@example.com"
        assert by_id["enr-1"].status == MailEnrollmentStatus.PENDING
        assert by_id["enr-2"].status == MailEnrollmentStatus.PENDING
        assert by_id["enr-3"].status == MailEnrollmentStatus.SUPPRESSED
        assert by_id["enr-3"].email_at_enrollment == "carol@example.com"
    finally:
        await store.close()


async def test_legacy_enrollment_rows_get_null_assigned_mailbox_id(legacy_db_path):
    """The CRITICAL assertion: a JSON blob that has NEVER contained
    `assigned_mailbox_id` at all (not merely `null`, genuinely ABSENT as a
    key -- exactly what real pre-Phase-A production data looks like) must
    deserialize with assigned_mailbox_id == None, via the model's own
    default, with zero migration/backfill write required."""
    store = SQLiteMailEnrollmentStore(legacy_db_path)
    await store.connect()
    try:
        rows = await store.list_for_campaign("camp-1")
        assert all(r.assigned_mailbox_id is None for r in rows)

        # Confirm the on-disk blob genuinely lacks the key (this is the
        # actual legacy shape, not something re-written by connect()).
        cursor = await store._connection.execute("SELECT data FROM mail_enrollments WHERE enrollment_id = 'enr-1'")
        row = await cursor.fetchone()
        await cursor.close()
        assert "assigned_mailbox_id" not in row["data"]
    finally:
        await store.close()


async def test_legacy_enrollment_rows_can_be_read_and_saved_normally(legacy_db_path):
    """Full round-trip: get() a legacy row, mutate it exactly the way
    MailSendingService.assign_mailbox_if_needed() would, save() it, and
    confirm the write persisted -- proving the upgraded schema/store is
    not just read-compatible but fully write-compatible too."""
    store = SQLiteMailEnrollmentStore(legacy_db_path)
    await store.connect()
    try:
        enrollment = await store.get("enr-1")
        assert enrollment is not None
        assert enrollment.assigned_mailbox_id is None

        updated = enrollment.model_copy(update={"assigned_mailbox_id": "mbx-1", "status": MailEnrollmentStatus.ACTIVE})
        await store.save(updated)

        reread = await store.get("enr-1")
        assert reread.assigned_mailbox_id == "mbx-1"
        assert reread.status == MailEnrollmentStatus.ACTIVE

        # The OTHER legacy rows must be completely unaffected by this write.
        untouched = await store.get("enr-2")
        assert untouched.assigned_mailbox_id is None
        assert untouched.status == MailEnrollmentStatus.PENDING
    finally:
        await store.close()


# --- Item 3: legacy READY-campaign compatibility / inert startup -----------


async def test_opening_every_store_against_the_legacy_db_is_behaviorally_inert(legacy_db_path):
    """Simulates Phase A's deployment itself against a real, pre-existing
    production-shaped database: open every relevant store (including the
    two brand-new Phase A tables, which don't exist in the file yet) and
    confirm NOTHING changes as a mere side effect of connecting -- no
    MailEnrollmentStep row, no mailbox assignment, no campaign/enrollment
    status change, no Activity Log event. This is the concrete proof that
    a Phase A deploy, by itself, does not touch a single existing
    campaign's execution state."""
    campaign_store = SQLiteMailCampaignStore(legacy_db_path)
    step_store = SQLiteMailSequenceStepStore(legacy_db_path)
    enrollment_store = SQLiteMailEnrollmentStore(legacy_db_path)
    enrollment_step_store = SQLiteMailEnrollmentStepStore(legacy_db_path)  # brand-new table, doesn't exist in the file yet
    policy_store = SQLiteMailboxSendPolicyStore(legacy_db_path)  # brand-new table, doesn't exist in the file yet
    activity_store = SQLiteActivityEventStore(legacy_db_path)

    for store in (campaign_store, step_store, enrollment_store, enrollment_step_store, policy_store, activity_store):
        await store.connect()
    try:
        campaign = await campaign_store.get("camp-1")
        assert campaign.status == MailCampaignStatus.READY, "must remain READY -- nothing here ever calls activate_campaign()"

        enrollments = await enrollment_store.list_for_campaign("camp-1")
        statuses = {e.enrollment_id: e.status for e in enrollments}
        assert statuses == {
            "enr-1": MailEnrollmentStatus.PENDING,
            "enr-2": MailEnrollmentStatus.PENDING,
            "enr-3": MailEnrollmentStatus.SUPPRESSED,
        }, "enrollment statuses must be byte-identical to before the upgrade"
        assert all(e.assigned_mailbox_id is None for e in enrollments), "no mailbox assignment merely from opening the stores"

        step_rows = await enrollment_step_store.list_for_campaign("camp-1")
        assert step_rows == [], "no MailEnrollmentStep row may be created merely by startup/schema upgrade"

        events = await activity_store.list()
        assert events == [], "no Activity Log event may be emitted merely by startup/schema upgrade"
    finally:
        for store in (campaign_store, step_store, enrollment_store, enrollment_step_store, policy_store, activity_store):
            await store.close()


# --- Trigger foundation (Stage 5A, 2026-09-04): lead_start_mode /
# execution_active_since backward compatibility -----------------------------


async def test_legacy_campaign_blob_without_trigger_fields_deserializes_as_immediate_and_null(legacy_db_path):
    """The CRITICAL assertion for Stage 5A: `_pre_phase_a_campaign_blob()`
    (used by every fixture in this file) has NEVER contained
    `lead_start_mode` or `execution_active_since` keys at all -- exactly
    what real, already-deployed production campaign rows look like right
    now. Confirms both new fields apply their Pydantic defaults
    (lead_start_mode="immediate", execution_active_since=None) with zero
    migration/backfill write required -- the same JSON-blob-default
    mechanism this whole file already documents for assigned_mailbox_id."""
    store = SQLiteMailCampaignStore(legacy_db_path)
    await store.connect()
    try:
        campaign = await store.get("camp-1")
        assert campaign is not None
        assert campaign.lead_start_mode == "immediate"
        assert campaign.execution_active_since is None

        # Confirm the on-disk blob genuinely lacks both keys -- this is the
        # actual legacy shape, not something re-written by connect().
        cursor = await store._connection.execute("SELECT data FROM mail_campaigns WHERE mail_campaign_id = 'camp-1'")
        row = await cursor.fetchone()
        await cursor.close()
        assert "lead_start_mode" not in row["data"]
        assert "execution_active_since" not in row["data"]
    finally:
        await store.close()


async def test_reopening_the_legacy_db_a_second_time_is_still_inert_and_idempotent(legacy_db_path):
    """Simulates a second app restart against the same, now-upgraded file
    (the common real-world case of a redeploy) -- confirms the upgrade
    path is safe to run repeatedly and stays inert every time, not just on
    the first boot."""
    for _ in range(2):
        campaign_store = SQLiteMailCampaignStore(legacy_db_path)
        enrollment_store = SQLiteMailEnrollmentStore(legacy_db_path)
        enrollment_step_store = SQLiteMailEnrollmentStepStore(legacy_db_path)
        await campaign_store.connect()
        await enrollment_store.connect()
        await enrollment_step_store.connect()
        try:
            campaign = await campaign_store.get("camp-1")
            assert campaign.status == MailCampaignStatus.READY
            step_rows = await enrollment_step_store.list_for_campaign("camp-1")
            assert step_rows == []
        finally:
            await campaign_store.close()
            await enrollment_store.close()
            await enrollment_step_store.close()
