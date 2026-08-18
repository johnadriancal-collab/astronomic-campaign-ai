"""
Direct SQLite-layer tests for the Astronomic Mail Phase 1 stores -- proves
the actual DB-level constraints (not just the in-memory mirror's dict-key
behavior) enforce: UNIQUE(mail_campaign_id, step_number),
UNIQUE(mail_campaign_id, crm_contact_id), and the suppression table's
upsert-in-place-by-primary-key behavior. Same tmp_path-backed SQLite file
convention as this suite's other sqlite_*_store tests.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailCampaign, MailCampaignStatus, MailEnrollment, MailEnrollmentStatus, MailSequenceStep, MailSuppression, MailSuppressionReason
from app.repositories.mail_campaign_store import MailCampaignNotFoundError
from app.repositories.mail_sequence_step_store import DuplicateMailSequenceStepNumberError, MailSequenceStepNotFoundError
from app.repositories.sqlite_mail_campaign_store import SQLiteMailCampaignStore
from app.repositories.sqlite_mail_enrollment_store import SQLiteMailEnrollmentStore
from app.repositories.sqlite_mail_sequence_step_store import SQLiteMailSequenceStepStore
from app.repositories.sqlite_mail_suppression_store import SQLiteMailSuppressionStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def campaign_store(tmp_path):
    store = SQLiteMailCampaignStore(str(tmp_path / "mail.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def step_store(tmp_path):
    store = SQLiteMailSequenceStepStore(str(tmp_path / "mail.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def enrollment_store(tmp_path):
    store = SQLiteMailEnrollmentStore(str(tmp_path / "mail.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def suppression_store(tmp_path):
    store = SQLiteMailSuppressionStore(str(tmp_path / "mail.db"))
    await store.connect()
    yield store
    await store.close()


def _now():
    return datetime.now(timezone.utc)


# --- MailCampaignStore ----------------------------------------------------


async def test_campaign_create_get_save(campaign_store):
    now = _now()
    campaign = MailCampaign(mail_campaign_id="c1", name="Test", status=MailCampaignStatus.DRAFT, created_at=now, updated_at=now)
    await campaign_store.create(campaign)

    fetched = await campaign_store.get("c1")
    assert fetched.name == "Test"

    updated = fetched.model_copy(update={"name": "Renamed"})
    await campaign_store.save(updated)
    assert (await campaign_store.get("c1")).name == "Renamed"


async def test_campaign_save_missing_raises(campaign_store):
    now = _now()
    ghost = MailCampaign(mail_campaign_id="ghost", name="Ghost", created_at=now, updated_at=now)
    with pytest.raises(MailCampaignNotFoundError):
        await campaign_store.save(ghost)


async def test_campaign_create_duplicate_id_raises(campaign_store):
    now = _now()
    campaign = MailCampaign(mail_campaign_id="c1", name="Test", created_at=now, updated_at=now)
    await campaign_store.create(campaign)
    with pytest.raises(ValueError):
        await campaign_store.create(campaign)


# --- MailSequenceStepStore: real UNIQUE(mail_campaign_id, step_number) ---


async def test_step_unique_constraint_enforced_at_db_layer(step_store):
    now = _now()
    step1 = MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="A", body="B", created_at=now, updated_at=now)
    await step_store.create(step1)

    step2 = MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=1, subject="C", body="D", created_at=now, updated_at=now)
    with pytest.raises(DuplicateMailSequenceStepNumberError):
        await step_store.create(step2)

    # Same step_number, DIFFERENT campaign -- must be allowed (constraint is composite).
    step3 = MailSequenceStep(step_id="s3", mail_campaign_id="c2", step_number=1, subject="E", body="F", created_at=now, updated_at=now)
    await step_store.create(step3)  # must not raise


async def test_step_list_for_campaign_ordered_by_step_number(step_store):
    now = _now()
    for step_id, number in [("s3", 3), ("s1", 1), ("s2", 2)]:
        await step_store.create(
            MailSequenceStep(step_id=step_id, mail_campaign_id="c1", step_number=number, subject="x", body="y", created_at=now, updated_at=now)
        )
    ordered = await step_store.list_for_campaign("c1")
    assert [s.step_id for s in ordered] == ["s1", "s2", "s3"]


async def test_step_save_renumber_avoids_transient_collision_via_two_phase(step_store):
    """Confirms the two-phase offset renumbering pattern (used by
    MailCampaignService._renumber) works against the REAL UNIQUE constraint --
    directly swapping two step_numbers without an offset phase would violate
    the constraint transiently; this proves the offset approach avoids that."""
    now = _now()
    s1 = MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="A", body="B", created_at=now, updated_at=now)
    s2 = MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="C", body="D", created_at=now, updated_at=now)
    await step_store.create(s1)
    await step_store.create(s2)

    # Phase 1: offset both out of the way.
    await step_store.save(s1.model_copy(update={"step_number": 100_000}))
    await step_store.save(s2.model_copy(update={"step_number": 100_001}))
    # Phase 2: reassign swapped.
    await step_store.save(s1.model_copy(update={"step_number": 2}))
    await step_store.save(s2.model_copy(update={"step_number": 1}))

    ordered = await step_store.list_for_campaign("c1")
    assert [s.step_id for s in ordered] == ["s2", "s1"]


async def test_step_save_missing_raises(step_store):
    now = _now()
    ghost = MailSequenceStep(step_id="ghost", mail_campaign_id="c1", step_number=1, subject="A", body="B", created_at=now, updated_at=now)
    with pytest.raises(MailSequenceStepNotFoundError):
        await step_store.save(ghost)


async def test_step_delete_is_a_noop_if_missing(step_store):
    await step_store.delete("does-not-exist")  # must not raise


# --- MailEnrollmentStore: real composite PRIMARY KEY -----------------------


async def test_enrollment_unique_constraint_enforced_at_db_layer(enrollment_store):
    now = _now()
    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="a@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now,
    )
    first = await enrollment_store.create(enrollment)
    assert first is True

    duplicate = enrollment.model_copy(update={"enrollment_id": "e2"})
    second = await enrollment_store.create(duplicate)
    assert second is False

    rows = await enrollment_store.list_for_campaign("c1")
    assert len(rows) == 1
    assert rows[0].enrollment_id == "e1"  # the FIRST insert wins, never overwritten


async def test_enrollment_same_contact_different_campaign_is_allowed(enrollment_store):
    now = _now()
    e1 = MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now)
    e2 = MailEnrollment(enrollment_id="e2", mail_campaign_id="c2", crm_contact_id="contact-1", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now)
    assert await enrollment_store.create(e1) is True
    assert await enrollment_store.create(e2) is True  # different campaign -- allowed


async def test_enrollment_delete_for_campaign_only_touches_that_campaign(enrollment_store):
    now = _now()
    e1 = MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="x", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now)
    e2 = MailEnrollment(enrollment_id="e2", mail_campaign_id="c2", crm_contact_id="y", email_at_enrollment="b@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now)
    await enrollment_store.create(e1)
    await enrollment_store.create(e2)

    await enrollment_store.delete_for_campaign("c1")
    assert await enrollment_store.list_for_campaign("c1") == []
    assert len(await enrollment_store.list_for_campaign("c2")) == 1


async def test_enrollment_count_for_campaign(enrollment_store):
    now = _now()
    for i in range(3):
        await enrollment_store.create(
            MailEnrollment(enrollment_id=f"e{i}", mail_campaign_id="c1", crm_contact_id=f"contact-{i}", email_at_enrollment=f"{i}@example.com", status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now)
        )
    assert await enrollment_store.count_for_campaign("c1") == 3
    assert await enrollment_store.count_for_campaign("nonexistent") == 0


# --- MailSuppressionStore: email_normalized IS the primary key -----------


async def test_suppression_upsert_creates_then_updates_the_same_row(suppression_store):
    now = _now()
    row = MailSuppression(email_normalized="a@example.com", reason=MailSuppressionReason.MANUAL, created_at=now, updated_at=now, active=True)
    await suppression_store.upsert(row)

    fetched = await suppression_store.get("a@example.com")
    assert fetched.active is True

    deactivated = fetched.model_copy(update={"active": False})
    await suppression_store.upsert(deactivated)

    all_rows = await suppression_store.list()
    assert len(all_rows) == 1  # still one row, not two
    assert (await suppression_store.get("a@example.com")).active is False


async def test_suppression_get_missing_returns_none(suppression_store):
    assert await suppression_store.get("nobody@example.com") is None
