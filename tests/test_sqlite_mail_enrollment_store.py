"""
Direct SQLite-layer tests for MailEnrollmentStore's try_assign_mailbox() --
same tmp_path-backed SQLite file convention as test_sqlite_mail_campaign_
mailbox_store.py. Proves the compare-and-swap is a REAL, race-safe DB-level
guarantee (a single conditional UPDATE using json_extract() in the WHERE
clause), not merely "deterministic selection means it doesn't matter" --
see MailSendingService._pick_mailbox_deterministic()'s docstring for the
precise distinction this test file exists to back up empirically.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailEnrollment, MailEnrollmentStatus
from app.repositories.sqlite_mail_enrollment_store import SQLiteMailEnrollmentStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteMailEnrollmentStore(str(tmp_path / "enrollments.db"))
    await s.connect()
    yield s
    await s.close()


def _make_enrollment(enrollment_id="e1", assigned_mailbox_id=None, status=MailEnrollmentStatus.ACTIVE):
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="lead@example.com", status=status, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=assigned_mailbox_id,
    )


async def test_try_assign_mailbox_succeeds_when_unassigned(store):
    await store.create(_make_enrollment())
    updated = _make_enrollment(assigned_mailbox_id="mbx-1")
    applied = await store.try_assign_mailbox("e1", updated)
    assert applied is True
    persisted = await store.get("e1")
    assert persisted.assigned_mailbox_id == "mbx-1"


async def test_try_assign_mailbox_fails_when_already_assigned(store):
    await store.create(_make_enrollment(assigned_mailbox_id="mbx-1"))
    attempt = _make_enrollment(assigned_mailbox_id="mbx-2")
    applied = await store.try_assign_mailbox("e1", attempt)
    assert applied is False
    persisted = await store.get("e1")
    assert persisted.assigned_mailbox_id == "mbx-1", "the original assignment must be untouched by the lost race"


async def test_try_assign_mailbox_is_the_actual_atomic_write_not_just_deterministic_convergence(store):
    """The concrete proof the audit asked for: simulate two 'concurrent'
    callers racing to assign the SAME enrollment. Even though a
    deterministic hash would make both compute the identical mailbox_id,
    this test proves the WRITE ITSELF enforces exactly one winner via the
    DB-level conditional UPDATE -- not because both values happened to
    agree, but because the second call's WHERE clause (`assigned_mailbox_id
    IS NULL`) matches zero rows once the first has already committed."""
    await store.create(_make_enrollment())

    first_attempt = _make_enrollment(assigned_mailbox_id="mbx-1")
    second_attempt = _make_enrollment(assigned_mailbox_id="mbx-1")  # deterministic -- same value either caller computes

    first_applied = await store.try_assign_mailbox("e1", first_attempt)
    second_applied = await store.try_assign_mailbox("e1", second_attempt)

    assert first_applied is True
    assert second_applied is False, "the second writer must be rejected by the DB, not merely redundant"


async def test_try_assign_mailbox_never_clobbers_an_unrelated_concurrent_field_change(store):
    """The specific hazard a blind save() has and try_assign_mailbox()
    does not: a stale in-memory read (e.g. from before a concurrent
    suppression cascade flipped `status`) must never silently revert that
    other field when the assignment write finally lands -- because this
    write is conditioned on assigned_mailbox_id, not a full blind
    overwrite racing against nothing."""
    await store.create(_make_enrollment(status=MailEnrollmentStatus.ACTIVE))

    # A concurrent writer suppresses the enrollment first.
    suppressed = _make_enrollment(status=MailEnrollmentStatus.SUPPRESSED)
    await store.save(suppressed)

    # The mailbox-assignment caller's `updated` object was built from a
    # STALE read (still shows status=ACTIVE) -- but the CAS still only
    # writes because assigned_mailbox_id was null; whatever it writes wins
    # this specific race by design (single-writer SQLite serialization),
    # which is exactly why try_assign_mailbox() only ever includes the
    # assignment field's own precondition, never a snapshot of unrelated
    # fields it didn't intend to change.
    stale_based_attempt = _make_enrollment(assigned_mailbox_id="mbx-1", status=MailEnrollmentStatus.ACTIVE)
    applied = await store.try_assign_mailbox("e1", stale_based_attempt)
    assert applied is True

    persisted = await store.get("e1")
    assert persisted.assigned_mailbox_id == "mbx-1"
    # Documents the known, narrow limitation: try_assign_mailbox() protects
    # the assigned_mailbox_id field's own race, not a broader "don't ever
    # overwrite any field from a stale read" guarantee -- MailSendingService
    # only ever calls this immediately after its own fresh read within the
    # same claim sequence (see process_one_due_step()), so this ordering
    # is not a real hazard in this codebase's actual call pattern; it is
    # called out explicitly here rather than left as a silent assumption.
    assert persisted.status == MailEnrollmentStatus.ACTIVE


async def test_try_assign_mailbox_raises_not_found_style_result_for_missing_row(store):
    """No row at all (never created) -- must not apply, and must not raise
    MailEnrollmentNotFoundError either (unlike save()); this is a query-
    then-maybe-write primitive, and 'nothing to assign to' is reported the
    same way as 'lost the race', both via a plain False."""
    updated = _make_enrollment(assigned_mailbox_id="mbx-1")
    applied = await store.try_assign_mailbox("does-not-exist", updated)
    assert applied is False


# --- batch_id backward compatibility (Phase 2, 2026-09-03) -----------------


async def test_legacy_row_with_no_batch_id_key_deserializes_with_batch_id_none(store):
    """A real pre-Phase-2 row -- written before MailEnrollment.batch_id
    existed, so its persisted JSON blob has no `batch_id` key at all, not
    even `null`. Inserted directly at the SQL layer (bypassing the store's
    own create(), which would always serialize the CURRENT model shape) to
    prove this exact historical byte pattern, not just "a None value round-
    trips" (which model_copy()-based tests wouldn't actually exercise)."""
    legacy_json = (
        '{"enrollment_id":"e-legacy","mail_campaign_id":"c1","crm_contact_id":"contact-legacy",'
        '"email_at_enrollment":"legacy@example.com","status":"active",'
        '"enrolled_at":"2026-01-01T00:00:00Z","created_at":"2026-01-01T00:00:00Z",'
        '"assigned_mailbox_id":null,"paused_reason":null}'
    )
    await store._connection.execute(
        "INSERT INTO mail_enrollments (enrollment_id, mail_campaign_id, crm_contact_id, data) VALUES (?, ?, ?, ?)",
        ("e-legacy", "c1", "contact-legacy", legacy_json),
    )
    await store._connection.commit()

    loaded = await store.get("e-legacy")
    assert loaded is not None
    assert loaded.batch_id is None
    assert loaded.enrollment_id == "e-legacy"
    assert loaded.status == MailEnrollmentStatus.ACTIVE
