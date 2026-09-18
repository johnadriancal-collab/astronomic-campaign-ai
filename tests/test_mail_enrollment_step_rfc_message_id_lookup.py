"""
MailEnrollmentStepStore.get_by_rfc_message_id() -- bounce detection
(2026-09-18). Same tests run against both the Memory and SQLite
implementations, plus a dedicated migration/backfill test proving a
PRE-EXISTING row (created before rfc_message_id was a promoted column,
simulating real production data) is still findable after connect()'s
migration runs.
"""

from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.sqlite_mail_enrollment_step_store import SQLiteMailEnrollmentStepStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def make_step(enrollment_step_id: str, rfc_message_id: str | None, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=enrollment_step_id,
        mail_campaign_id="c1",
        enrollment_id="e1",
        crm_contact_id="contact1",
        step_id="step-1",
        step_number=1,
        subject="Quick hello",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        rfc_message_id=rfc_message_id,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryMailEnrollmentStepStore()
    else:
        s = SQLiteMailEnrollmentStepStore(str(tmp_path / "test.db"))
        await s.connect()
        yield s
        await s.close()


async def test_get_by_rfc_message_id_finds_the_right_step(store):
    await store.create(make_step("s1", "abc@domain.com"))
    await store.create(make_step("s2", "def@domain.com", enrollment_id="e2", step_id="step-2"))

    found = await store.get_by_rfc_message_id("def@domain.com")
    assert found.enrollment_step_id == "s2"


async def test_get_by_rfc_message_id_returns_none_for_unrelated_id(store):
    await store.create(make_step("s1", "abc@domain.com"))
    assert await store.get_by_rfc_message_id("never-sent@elsewhere.com") is None


async def test_get_by_rfc_message_id_returns_none_when_null(store):
    await store.create(make_step("s1", None))
    assert await store.get_by_rfc_message_id("abc@domain.com") is None


# --- Migration / backfill (SQLite only) -------------------------------------


async def test_pre_existing_row_created_before_the_column_existed_is_backfilled_and_findable(tmp_path):
    """Simulates real production data: a row written to the OLD schema
    (no rfc_message_id column at all, the value only ever inside the
    JSON blob) must still be found by get_by_rfc_message_id() once
    connect() runs its migration -- proving the backfill, not just the
    column's existence, actually works."""
    db_path = str(tmp_path / "test.db")

    # Write directly against the OLD schema shape (pre-migration) --
    # deliberately bypassing SQLiteMailEnrollmentStepStore itself, which
    # would already create the new column.
    old_step = make_step("s1", "already-sent@domain.com")
    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        """
        CREATE TABLE mail_enrollment_steps (
            enrollment_step_id TEXT PRIMARY KEY,
            mail_campaign_id TEXT NOT NULL,
            enrollment_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            step_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            next_send_at TEXT,
            mailbox_id TEXT,
            sent_at TEXT,
            claimed_at TEXT,
            data TEXT NOT NULL,
            UNIQUE (enrollment_id, step_id)
        )
        """
    )
    await conn.execute(
        "INSERT INTO mail_enrollment_steps "
        "(enrollment_step_id, mail_campaign_id, enrollment_id, step_id, step_number, status, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s1", "c1", "e1", "step-1", 1, "sent", old_step.model_dump_json()),
    )
    await conn.commit()
    await conn.close()

    # NOW connect through the real store -- its migration must add both
    # new columns AND backfill rfc_message_id from the existing blob.
    store = SQLiteMailEnrollmentStepStore(db_path)
    await store.connect()

    found = await store.get_by_rfc_message_id("already-sent@domain.com")
    assert found is not None
    assert found.enrollment_step_id == "s1"

    await store.close()


async def test_migration_is_idempotent_across_repeated_connects(tmp_path):
    db_path = str(tmp_path / "test.db")
    store1 = SQLiteMailEnrollmentStepStore(db_path)
    await store1.connect()
    await store1.create(make_step("s1", "abc@domain.com"))
    await store1.close()

    # Reconnect (simulating a process restart) -- migration must be a
    # safe no-op the second time, never destroying the row just written.
    store2 = SQLiteMailEnrollmentStepStore(db_path)
    await store2.connect()
    found = await store2.get_by_rfc_message_id("abc@domain.com")
    assert found is not None
    await store2.close()
