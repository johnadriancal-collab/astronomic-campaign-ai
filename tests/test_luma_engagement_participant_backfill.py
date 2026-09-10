"""
Tests for app/services/luma_engagement_participant_backfill.py -- the
historical, dry-run-capable, operator-scoped driver. Reuses
LumaEngagementParticipantSyncService (Stage 1H-B) verbatim; these tests
focus on orchestration, counting, dry-run/write parity, deterministic
ordering, deduplication, and failure isolation -- not re-deriving the
eligibility/RSVP-mapping matrix already exhaustively covered by
test_luma_engagement_participant_sync_service.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.activity import ActivityCategory
from app.models.client_crm import (
    Engagement,
    EngagementParticipant,
    EngagementStatus,
    EngagementType,
    ParticipantAttendanceStatus,
    ParticipantRole,
    ParticipantRsvpStatus,
    ParticipantSource,
)
from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaRegistration
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.luma_engagement_participant_backfill import (
    ParticipantBackfillInvalidTarget,
    run_luma_engagement_participant_backfill,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _engagement(engagement_id="e1", luma_event_id="evt-1", status=EngagementStatus.PLANNED, archived=False, **overrides) -> Engagement:
    return Engagement(
        engagement_id=engagement_id, client_id="c1", title="SF Investor Dinner", engagement_type=EngagementType.DINNER,
        luma_event_id=luma_event_id, status=status, archived=archived, created_at=NOW, updated_at=NOW, **overrides,
    )


def _contact(crm_contact_id=None, **overrides) -> CrmContact:
    return CrmContact(crm_contact_id=crm_contact_id or str(uuid.uuid4()), created_at=NOW, updated_at=NOW, **overrides)


def _registration(
    luma_guest_id=None, luma_event_id="evt-1", crm_contact_id=None, match_status=LumaMatchStatus.MATCHED,
    approval_status=LumaApprovalStatus.APPROVED, registered_at=NOW, **overrides,
) -> LumaRegistration:
    return LumaRegistration(
        luma_guest_id=luma_guest_id or str(uuid.uuid4()), luma_event_id=luma_event_id, crm_contact_id=crm_contact_id,
        match_status=match_status, approval_status=approval_status, registered_at=registered_at,
        synced_at=NOW, updated_at=NOW, **overrides,
    )


def _participant(participant_id="p1", engagement_id="e1", crm_contact_id=None, **overrides) -> EngagementParticipant:
    overrides.setdefault("first_name", "Existing")
    return EngagementParticipant(
        participant_id=participant_id, engagement_id=engagement_id, client_id="c1", crm_contact_id=crm_contact_id,
        created_at=NOW, updated_at=NOW, **overrides,
    )


@pytest.fixture
def stores():
    return (
        MemoryEngagementStore(),
        MemoryEngagementParticipantStore(),
        MemoryCrmContactStore(),
        MemoryLumaRegistrationStore(),
        ActivityLogService(MemoryActivityEventStore()),
    )


async def _seed(stores, *, engagement=None, contact=None, participant=None, registrations=None):
    engagement_store, participant_store, contact_store, registration_store, _activity_log = stores
    if engagement is not None:
        await engagement_store.create(engagement)
    if contact is not None:
        await contact_store.create(contact)
    if participant is not None:
        await participant_store.create(participant)
    for r in registrations or []:
        await registration_store.save(r)


async def _run(stores, engagement_id="e1", dry_run=True):
    engagement_store, participant_store, contact_store, registration_store, activity_log = stores
    return await run_luma_engagement_participant_backfill(
        engagement_store, participant_store, contact_store, registration_store, activity_log,
        engagement_id=engagement_id, dry_run=dry_run,
    )


# =====================================================================
# Target validation
# =====================================================================


async def test_nonexistent_engagement_is_rejected(stores):
    with pytest.raises(ParticipantBackfillInvalidTarget):
        await _run(stores, engagement_id="does-not-exist")


async def test_unlinked_engagement_is_rejected(stores):
    await _seed(stores, engagement=_engagement(luma_event_id=None))
    with pytest.raises(ParticipantBackfillInvalidTarget):
        await _run(stores)


async def test_archived_engagement_registrations_report_the_1h_b_skip_outcome(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(archived=True), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    report = await _run(stores)
    assert report.counts.skipped_engagement_archived == 1
    assert report.counts.created == 0


async def test_cancelled_engagement_registrations_report_the_1h_b_skip_outcome(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(status=EngagementStatus.CANCELLED), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    report = await _run(stores)
    assert report.counts.skipped_engagement_cancelled == 1


# =====================================================================
# Dry run: zero writes, and matches subsequent write result
# =====================================================================


async def test_dry_run_makes_zero_writes_to_the_real_stores(stores):
    engagement_store, participant_store, contact_store, _reg_store, activity_log = stores
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    report = await _run(stores, dry_run=True)

    assert report.counts.created == 1  # predicted
    # But the REAL participant store passed in received nothing.
    assert await participant_store.list_for_engagement("e1") == []
    # And the REAL activity log received nothing either.
    page = await activity_log.list_events(category=ActivityCategory.LUMA)
    assert page.items == []


async def test_dry_run_never_advances_a_real_contacts_engagement_stage(stores):
    """Contacts CRM Stage 3B: a dry run's own signal service is bound to
    the throwaway replica contact store, never the real one -- a
    positive-interest registration (APPROVED) must not advance the REAL
    Contact's engagement_stage, structurally, not just by convention."""
    engagement_store, participant_store, contact_store, _reg_store, activity_log = stores
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.APPROVED)],
    )
    await _run(stores, dry_run=True)

    real_contact = await contact_store.get(contact.crm_contact_id)
    assert real_contact.custom_fields.get("engagement_stage") is None
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"] == []


async def test_dry_run_prediction_matches_subsequent_write_result(stores):
    contact = _contact(first_name="Ethan", last_name="Wong")
    regs = [_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.INVITED)]
    await _seed(stores, engagement=_engagement(), contact=contact, registrations=regs)

    dry_report = await _run(stores, dry_run=True)

    write_report = await _run(stores, dry_run=False)

    assert dry_report.counts.created == write_report.counts.created == 1
    assert dry_report.counts.participants_after == write_report.counts.participants_after == 1

    engagement_store, participant_store, _c, _r, _a = stores
    written = await participant_store.list_for_engagement("e1")
    assert len(written) == 1
    assert written[0].rsvp_status == ParticipantRsvpStatus.INVITED
    assert written[0].source == ParticipantSource.LUMA


# =====================================================================
# Create / update / unresolved
# =====================================================================


async def test_matched_contact_creates_one_participant(stores):
    contact = _contact()
    await _seed(stores, engagement=_engagement(), contact=contact, registrations=[_registration(crm_contact_id=contact.crm_contact_id)])
    report = await _run(stores, dry_run=False)
    assert report.counts.created == 1
    assert report.counts.participants_before == 0
    assert report.counts.participants_after == 1


async def test_unresolved_registration_is_skipped_not_matched(stores):
    await _seed(
        stores, engagement=_engagement(),
        registrations=[_registration(crm_contact_id=None, match_status=LumaMatchStatus.NEEDS_REVIEW)],
    )
    report = await _run(stores, dry_run=False)
    assert report.counts.skipped_not_matched == 1
    assert report.counts.created == 0


async def test_registration_pointing_at_a_vanished_contact_is_skipped_no_contact(stores):
    await _seed(stores, engagement=_engagement(), registrations=[_registration(crm_contact_id="does-not-exist")])
    report = await _run(stores, dry_run=False)
    assert report.counts.skipped_no_contact == 1


async def test_existing_participant_produces_unchanged_not_a_duplicate(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        participant=_participant(crm_contact_id=contact.crm_contact_id, rsvp_status=ParticipantRsvpStatus.CONFIRMED, source=ParticipantSource.LUMA),
        registrations=[_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.APPROVED)],
    )
    report = await _run(stores, dry_run=False)
    assert report.counts.unchanged == 1
    assert report.counts.created == 0
    assert report.counts.participants_before == report.counts.participants_after == 1


async def test_blank_snapshot_fields_can_be_filled_on_an_existing_participant(stores):
    contact = _contact(first_name="Ethan", title="Co-CEO")
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        participant=_participant(crm_contact_id=contact.crm_contact_id, first_name="", title=None, source=ParticipantSource.MANUAL),
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    report = await _run(stores, dry_run=False)
    assert report.counts.updated == 1
    _e, participant_store, _c, _r, _a = stores
    participant = (await participant_store.list_for_engagement("e1"))[0]
    assert participant.first_name == "Ethan"
    assert participant.title == "Co-CEO"


# =====================================================================
# Manual participant protections
# =====================================================================


@pytest.mark.parametrize("role", [ParticipantRole.HOST, ParticipantRole.CLIENT, ParticipantRole.SPEAKER_PANELIST])
async def test_manual_participants_role_is_preserved(stores, role):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        participant=_participant(crm_contact_id=contact.crm_contact_id, role=role, source=ParticipantSource.MANUAL),
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    participant = (await participant_store.list_for_engagement("e1"))[0]
    assert participant.role == role
    assert participant.source == ParticipantSource.MANUAL


async def test_attendance_status_is_never_touched(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        participant=_participant(
            crm_contact_id=contact.crm_contact_id, attendance_status=ParticipantAttendanceStatus.ATTENDED, source=ParticipantSource.MANUAL
        ),
        registrations=[_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.DECLINED)],
    )
    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    participant = (await participant_store.list_for_engagement("e1"))[0]
    assert participant.attendance_status == ParticipantAttendanceStatus.ATTENDED


async def test_archived_participant_is_not_unarchived_or_duplicated(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        participant=_participant(crm_contact_id=contact.crm_contact_id, archived=True, source=ParticipantSource.MANUAL),
        registrations=[_registration(crm_contact_id=contact.crm_contact_id)],
    )
    report = await _run(stores, dry_run=False)
    assert report.counts.skipped_archived_participant == 1
    _e, participant_store, _c, _r, _a = stores
    participants = await participant_store.list_for_engagement("e1")
    assert len(participants) == 1
    assert participants[0].archived is True


# =====================================================================
# RSVP mapping (spot-check delegation, not the full matrix -- already
# exhaustively covered by test_luma_engagement_participant_sync_service.py)
# =====================================================================


async def test_rsvp_approved_maps_to_confirmed_on_create(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.APPROVED)],
    )
    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    assert (await participant_store.list_for_engagement("e1"))[0].rsvp_status == ParticipantRsvpStatus.CONFIRMED


async def test_rsvp_pending_approval_is_null_on_create(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        registrations=[_registration(crm_contact_id=contact.crm_contact_id, approval_status=LumaApprovalStatus.PENDING_APPROVAL)],
    )
    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    assert (await participant_store.list_for_engagement("e1"))[0].rsvp_status is None


# =====================================================================
# Deduplication + deterministic ordering
# =====================================================================


async def test_two_registrations_same_contact_resolve_to_one_participant_and_are_flagged_as_a_duplicate_group(stores):
    contact = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact,
        registrations=[
            _registration(luma_guest_id="gst-1", crm_contact_id=contact.crm_contact_id, registered_at=NOW),
            _registration(luma_guest_id="gst-2", crm_contact_id=contact.crm_contact_id, registered_at=NOW + timedelta(days=1)),
        ],
    )
    report = await _run(stores, dry_run=False)
    assert report.duplicate_contact_group_sizes == [2]
    assert report.counts.created == 1
    _e, participant_store, _c, _r, _a = stores
    assert len(await participant_store.list_for_engagement("e1")) == 1


async def test_deterministic_ordering_applies_the_later_dated_registration_last_regardless_of_storage_order(stores):
    """Feeds the LATER registration (registered_at further in the future,
    approval_status=approved) into the store FIRST -- the driver must
    still process the EARLIER one (invited) first and the later one
    (approved) second, so the final rsvp_status reflects the later one,
    proving it sorts by registered_at rather than trusting storage/
    insertion order."""
    contact = _contact()
    earlier = _registration(
        luma_guest_id="gst-earlier", crm_contact_id=contact.crm_contact_id,
        registered_at=NOW, approval_status=LumaApprovalStatus.INVITED,
    )
    later = _registration(
        luma_guest_id="gst-later", crm_contact_id=contact.crm_contact_id,
        registered_at=NOW + timedelta(days=1), approval_status=LumaApprovalStatus.APPROVED,
    )
    # Seed the LATER one first -- MemoryLumaRegistrationStore.list_for_event()
    # returns dict-insertion order, so this would fail if the driver ever
    # trusted storage order instead of sorting explicitly.
    await _seed(stores, engagement=_engagement(), contact=contact, registrations=[later, earlier])

    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    assert (await participant_store.list_for_engagement("e1"))[0].rsvp_status == ParticipantRsvpStatus.CONFIRMED


async def test_registrations_with_no_registered_at_sort_last(stores):
    contact = _contact()
    undated = _registration(
        luma_guest_id="gst-undated", crm_contact_id=contact.crm_contact_id, registered_at=None, approval_status=LumaApprovalStatus.INVITED,
    )
    dated = _registration(
        luma_guest_id="gst-dated", crm_contact_id=contact.crm_contact_id, registered_at=NOW, approval_status=LumaApprovalStatus.APPROVED,
    )
    # Seed the undated one first -- must still be processed LAST.
    await _seed(stores, engagement=_engagement(), contact=contact, registrations=[undated, dated])

    await _run(stores, dry_run=False)
    _e, participant_store, _c, _r, _a = stores
    # If undated were processed last (i.e. sorted last, correct), its
    # INVITED approval_status would never overwrite the already-CONFIRMED
    # rsvp_status (invited doesn't map back down from confirmed... but per
    # the locked mapping, `invited` IS a recognized status that DOES
    # overwrite an existing rsvp_status. Use a value that only takes
    # effect via the same recognized-mapping path to prove ordering.
    assert (await participant_store.list_for_engagement("e1"))[0].rsvp_status == ParticipantRsvpStatus.INVITED


# =====================================================================
# Idempotency
# =====================================================================


async def test_write_mode_run_twice_is_idempotent(stores):
    contact = _contact()
    await _seed(stores, engagement=_engagement(), contact=contact, registrations=[_registration(crm_contact_id=contact.crm_contact_id)])

    first = await _run(stores, dry_run=False)
    assert first.counts.created == 1

    second = await _run(stores, dry_run=False)
    assert second.counts.created == 0
    assert second.counts.unchanged == 1
    assert second.counts.participants_after == 1


# =====================================================================
# Activity Log
# =====================================================================


async def test_activity_log_only_records_real_create_and_update(stores):
    engagement_store, participant_store, contact_store, registration_store, activity_log = stores
    contact_created = _contact()
    contact_unchanged = _contact()
    contact_unresolved_reg_contact_id = "does-not-exist"

    await _seed(
        stores, engagement=_engagement(), contact=contact_created,
        participant=None,
        registrations=[_registration(crm_contact_id=contact_created.crm_contact_id, luma_guest_id="gst-create")],
    )
    await contact_store.create(contact_unchanged)
    await participant_store.create(_participant(crm_contact_id=contact_unchanged.crm_contact_id, participant_id="p-existing", rsvp_status=ParticipantRsvpStatus.CONFIRMED, source=ParticipantSource.LUMA))
    await registration_store.save(_registration(crm_contact_id=contact_unchanged.crm_contact_id, luma_guest_id="gst-unchanged", approval_status=LumaApprovalStatus.APPROVED))
    await registration_store.save(_registration(crm_contact_id=contact_unresolved_reg_contact_id, luma_guest_id="gst-no-contact"))

    await _run(stores, dry_run=False)

    page = await activity_log.list_events(category=ActivityCategory.LUMA, page_size=100)
    event_types = [e.event_type for e in page.items]
    assert event_types.count("luma.engagement_participant.created") == 1
    assert "luma.engagement_participant.updated" not in event_types  # the unchanged one produced no update event
    assert len(event_types) == 1  # nothing for the no-contact skip either


# =====================================================================
# Failure isolation -- one bad registration never aborts the cohort
# =====================================================================


async def test_one_registration_error_is_isolated_and_reported_others_still_succeed(stores, monkeypatch):
    contact_ok = _contact()
    contact_broken = _contact()
    await _seed(
        stores, engagement=_engagement(), contact=contact_ok,
        registrations=[
            _registration(luma_guest_id="gst-broken", crm_contact_id=contact_broken.crm_contact_id, registered_at=NOW),
            _registration(luma_guest_id="gst-ok", crm_contact_id=contact_ok.crm_contact_id, registered_at=NOW + timedelta(days=1)),
        ],
    )
    engagement_store, participant_store, contact_store, _r, _a = stores
    await contact_store.create(contact_broken)

    real_get = contact_store.get

    async def _broken_get(crm_contact_id):
        if crm_contact_id == contact_broken.crm_contact_id:
            raise RuntimeError("simulated contact-store failure")
        return await real_get(crm_contact_id)

    monkeypatch.setattr(contact_store, "get", _broken_get)

    report = await _run(stores, dry_run=False)

    assert report.counts.errors == 1
    assert len(report.errors) == 1
    assert "gst-broken" in report.errors[0]
    assert "RuntimeError" in report.errors[0]
    assert report.counts.created == 1  # the OTHER registration still succeeded
    assert len(await participant_store.list_for_engagement("e1")) == 1


# =====================================================================
# Structural: no "process every Engagement" path exists anywhere
# =====================================================================


def test_engagement_id_has_no_default_and_is_keyword_only():
    import inspect

    from app.services.luma_engagement_participant_backfill import run_luma_engagement_participant_backfill

    sig = inspect.signature(run_luma_engagement_participant_backfill)
    param = sig.parameters["engagement_id"]
    assert param.default is inspect.Parameter.empty
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_cli_script_requires_engagement_id_flag():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "run_luma_engagement_participant_backfill.py").read_text()
    assert '"--engagement-id", required=True' in source


def test_driver_module_never_lists_or_iterates_every_engagement():
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "luma_engagement_participant_backfill.py"
    ).read_text()
    assert "list_for_client(" not in source
    assert ".list()" not in source  # never calls engagement_store.list() / a whole-table scan
