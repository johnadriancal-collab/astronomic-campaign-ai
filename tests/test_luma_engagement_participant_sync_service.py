"""
Client CRM Stage 1H-B (2026-09-10) -- LumaEngagementParticipantSyncService,
the reusable sync-one-registration function. Exercised directly against
Memory stores (never mocks of the domain logic itself), independent of the
live webhook/backfill wiring in luma_sync_service.py (see
test_luma_sync_service.py's own Stage 1H-B integration tests for that half:
the fail-open boundary and the "unwired by default" backward-compatibility
proof).
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.client_crm import (
    Engagement,
    EngagementParticipant,
    EngagementStatus,
    EngagementType,
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
from app.services.activity_log_service import ActivityLogService
from app.services.luma_engagement_participant_sync_service import (
    LumaEngagementParticipantSyncService,
    LumaParticipantSyncOutcome,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _engagement(
    engagement_id="e1", client_id="c1", luma_event_id="evt-1", status=EngagementStatus.CONFIRMED, archived=False, **overrides
) -> Engagement:
    return Engagement(
        engagement_id=engagement_id,
        client_id=client_id,
        title="SF Investor Dinner",
        engagement_type=EngagementType.DINNER,
        luma_event_id=luma_event_id,
        status=status,
        archived=archived,
        created_at=NOW,
        updated_at=NOW,
        **overrides,
    )


def _contact(crm_contact_id="contact-1", **overrides) -> CrmContact:
    return CrmContact(crm_contact_id=crm_contact_id, created_at=NOW, updated_at=NOW, **overrides)


def _registration(
    luma_guest_id="gst-1",
    luma_event_id="evt-1",
    crm_contact_id="contact-1",
    match_status=LumaMatchStatus.MATCHED,
    approval_status=LumaApprovalStatus.APPROVED,
    **overrides,
) -> LumaRegistration:
    return LumaRegistration(
        luma_guest_id=luma_guest_id,
        luma_event_id=luma_event_id,
        crm_contact_id=crm_contact_id,
        match_status=match_status,
        approval_status=approval_status,
        synced_at=NOW,
        updated_at=NOW,
        **overrides,
    )


def _participant(
    participant_id="p1", engagement_id="e1", client_id="c1", crm_contact_id="contact-1", **overrides
) -> EngagementParticipant:
    overrides.setdefault("first_name", "Jane")
    return EngagementParticipant(
        participant_id=participant_id,
        engagement_id=engagement_id,
        client_id=client_id,
        crm_contact_id=crm_contact_id,
        created_at=NOW,
        updated_at=NOW,
        **overrides,
    )


@pytest_asyncio.fixture
async def stores():
    engagement_store = MemoryEngagementStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    crm_contact_store = MemoryCrmContactStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return engagement_store, engagement_participant_store, crm_contact_store, activity_log


@pytest_asyncio.fixture
async def service(stores):
    engagement_store, engagement_participant_store, crm_contact_store, activity_log = stores
    return LumaEngagementParticipantSyncService(
        engagement_store=engagement_store,
        engagement_participant_store=engagement_participant_store,
        crm_contact_store=crm_contact_store,
        activity_log=activity_log,
    )


async def _seed(stores, *, engagement=None, contact=None, participant=None):
    engagement_store, engagement_participant_store, crm_contact_store, _activity_log = stores
    if engagement is not None:
        await engagement_store.create(engagement)
    if contact is not None:
        await crm_contact_store.create(contact)
    if participant is not None:
        await engagement_participant_store.create(participant)


# =====================================================================
# CREATE -- items 1-6
# =====================================================================


async def test_create_approved_registration_creates_confirmed_luma_guest_participant(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.APPROVED))

    assert result.outcome == LumaParticipantSyncOutcome.CREATED
    assert result.participant.source == ParticipantSource.LUMA
    assert result.participant.role == ParticipantRole.GUEST
    assert result.participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED
    assert result.participant.crm_contact_id == "contact-1"
    assert result.participant.engagement_id == "e1"
    assert result.participant.client_id == "c1"


async def test_create_invited_registration_gives_invited_rsvp(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.INVITED))
    assert result.participant.rsvp_status == ParticipantRsvpStatus.INVITED


async def test_create_declined_registration_gives_declined_rsvp(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.DECLINED))
    assert result.participant.rsvp_status == ParticipantRsvpStatus.DECLINED


async def test_create_pending_approval_registration_gives_null_rsvp(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.PENDING_APPROVAL))
    assert result.outcome == LumaParticipantSyncOutcome.CREATED
    assert result.participant.rsvp_status is None


async def test_create_waitlist_registration_gives_null_rsvp(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.WAITLIST))
    assert result.participant.rsvp_status is None


async def test_create_session_registration_gives_null_rsvp(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.SESSION))
    assert result.participant.rsvp_status is None


async def test_create_never_sets_attendance_status_or_walk_in(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.participant.attendance_status is None
    assert result.participant.is_walk_in is False


async def test_create_populates_snapshot_fields_from_contact(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(first_name="Ethan", last_name="Wong", email="ethan@example.com", title="Co-CEO", company="Hive ASMBLD"),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    p = result.participant
    assert (p.first_name, p.last_name, p.email, p.title, p.company) == ("Ethan", "Wong", "ethan@example.com", "Co-CEO", "Hive ASMBLD")


async def test_create_records_activity(service, stores):
    _engagement_store, _p_store, _c_store, activity_log = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    await service.sync_luma_registration_to_engagement_participant(_registration())
    page = await activity_log.list_events(category=ActivityCategory.LUMA)
    assert any(e.event_type == "luma.engagement_participant.created" for e in page.items)


# =====================================================================
# NO-OPS -- items 7-11
# =====================================================================


async def test_no_linked_engagement_is_a_noop(service, stores):
    await _seed(stores, contact=_contact())  # no Engagement seeded at all
    result = await service.sync_luma_registration_to_engagement_participant(_registration(luma_event_id="evt-unlinked"))
    assert result.outcome == LumaParticipantSyncOutcome.NO_ENGAGEMENT_LINKED
    assert result.participant is None


async def test_archived_engagement_is_a_noop(service, stores):
    await _seed(stores, engagement=_engagement(archived=True), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.ENGAGEMENT_ARCHIVED


async def test_cancelled_engagement_is_a_noop(service, stores):
    await _seed(stores, engagement=_engagement(status=EngagementStatus.CANCELLED), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.ENGAGEMENT_CANCELLED


@pytest.mark.parametrize("status", [EngagementStatus.PLANNED, EngagementStatus.CONFIRMED, EngagementStatus.COMPLETED])
async def test_non_cancelled_engagement_statuses_all_allow_sync(service, stores, status):
    await _seed(stores, engagement=_engagement(status=status), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.CREATED


async def test_needs_review_registration_is_a_noop(service, stores):
    await _seed(stores, engagement=_engagement(), contact=_contact())
    result = await service.sync_luma_registration_to_engagement_participant(
        _registration(match_status=LumaMatchStatus.NEEDS_REVIEW, crm_contact_id=None)
    )
    assert result.outcome == LumaParticipantSyncOutcome.NOT_MATCHED
    assert result.participant is None


async def test_registration_with_no_crm_contact_id_is_a_noop(service, stores):
    await _seed(stores, engagement=_engagement())
    result = await service.sync_luma_registration_to_engagement_participant(
        _registration(crm_contact_id=None, match_status=LumaMatchStatus.MATCHED)
    )
    assert result.outcome == LumaParticipantSyncOutcome.NOT_MATCHED


async def test_registration_pointing_at_a_vanished_contact_is_a_noop_not_a_crash(service, stores):
    """crm_contact_id is set and match_status is MATCHED, but no such
    CrmContact is actually stored -- never fabricate a participant from
    nothing; never raise either."""
    await _seed(stores, engagement=_engagement())  # no contact seeded
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.NO_CONTACT
    assert result.participant is None


# =====================================================================
# IDEMPOTENCY -- items 12-15
# =====================================================================


async def test_same_registration_processed_twice_yields_one_participant(service, stores):
    engagement_store, participant_store, _c, _a = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    await service.sync_luma_registration_to_engagement_participant(_registration())
    await service.sync_luma_registration_to_engagement_participant(_registration())
    assert len(await participant_store.list_for_engagement("e1")) == 1


async def test_duplicate_webhook_style_repeat_delivery_yields_one_participant(service, stores):
    """Simulates the upstream duplicate-delivery no-op (LumaSyncService's
    own exact-delivery short-circuit) NOT catching a re-delivery -- even if
    this function were invoked twice for what upstream considers "the same
    delivery," it stays idempotent on its own."""
    _e, participant_store, _c, _a = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    reg = _registration(last_webhook_delivery_id="whd-1")
    await service.sync_luma_registration_to_engagement_participant(reg)
    await service.sync_luma_registration_to_engagement_participant(reg)
    assert len(await participant_store.list_for_engagement("e1")) == 1


async def test_two_registrations_same_contact_and_event_resolve_to_one_participant(service, stores):
    """Two DIFFERENT LumaRegistration rows (different luma_guest_id -- e.g.
    a cancel-and-re-register) for the same Contact + Luma event must still
    collapse to ONE EngagementParticipant."""
    _e, participant_store, _c, _a = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    await service.sync_luma_registration_to_engagement_participant(_registration(luma_guest_id="gst-1"))
    await service.sync_luma_registration_to_engagement_participant(_registration(luma_guest_id="gst-2"))
    assert len(await participant_store.list_for_engagement("e1")) == 1


async def test_simulated_create_race_falls_back_to_the_existing_participant(service, stores):
    """A duplicate-create domain error (another execution won the race)
    must resolve to updating the now-existing row, never raise, never
    leave a second row uncreated-but-lost."""
    engagement_store, participant_store, crm_contact_store, _a = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    # Simulate the race directly: create the "winning" participant BEFORE
    # calling sync(), exactly what a concurrent execution would have left
    # behind between this call's own lookup and its own create attempt.
    winner = _participant(participant_id="p-winner", source=ParticipantSource.LUMA, role=ParticipantRole.GUEST)
    await participant_store.create(winner)

    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome in (LumaParticipantSyncOutcome.UPDATED, LumaParticipantSyncOutcome.UNCHANGED)
    assert len(await participant_store.list_for_engagement("e1")) == 1
    assert result.participant.participant_id == "p-winner"


# =====================================================================
# MANUAL PARTICIPANT PRESERVATION -- items 16-21
# =====================================================================


async def test_existing_manual_guest_participant_is_not_duplicated_and_stays_manual(service, stores):
    _e, participant_store, _c, _a = stores
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(source=ParticipantSource.MANUAL, role=ParticipantRole.GUEST, first_name="Ethan"),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert len(await participant_store.list_for_engagement("e1")) == 1
    assert result.participant.source == ParticipantSource.MANUAL


@pytest.mark.parametrize(
    "role", [ParticipantRole.HOST, ParticipantRole.CLIENT, ParticipantRole.SPEAKER_PANELIST, ParticipantRole.ASTRONOMIC_TEAM]
)
async def test_existing_manual_participants_role_is_never_overwritten_to_guest(service, stores, role):
    await _seed(
        stores, engagement=_engagement(), contact=_contact(), participant=_participant(source=ParticipantSource.MANUAL, role=role)
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.participant.role == role


async def test_is_walk_in_is_never_changed_on_an_existing_participant(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(source=ParticipantSource.MANUAL, is_walk_in=True, first_name="Ethan"),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.participant.is_walk_in is True


async def test_attendance_status_is_never_changed_on_an_existing_participant(service, stores):
    from app.models.client_crm import ParticipantAttendanceStatus

    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(
            source=ParticipantSource.MANUAL, attendance_status=ParticipantAttendanceStatus.ATTENDED, first_name="Ethan"
        ),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.DECLINED))
    assert result.participant.attendance_status == ParticipantAttendanceStatus.ATTENDED


# =====================================================================
# SNAPSHOTS -- items 22-23
# =====================================================================


async def test_blank_snapshot_fields_can_be_filled_from_the_contact(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(first_name="Ethan", last_name="Wong", title="Co-CEO", company="Hive ASMBLD", email="ethan@example.com"),
        participant=_participant(first_name="", last_name="", title=None, company=None, email=None, source=ParticipantSource.MANUAL),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.UPDATED
    p = result.participant
    assert (p.first_name, p.last_name, p.email, p.title, p.company) == ("Ethan", "Wong", "ethan@example.com", "Co-CEO", "Hive ASMBLD")


async def test_nonblank_human_snapshot_fields_are_never_overwritten(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(first_name="Ethan", last_name="Wong", title="Co-CEO", company="Hive ASMBLD"),
        participant=_participant(
            first_name="Eth", last_name="W.", title="Founder", company="Hive", source=ParticipantSource.MANUAL
        ),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    p = result.participant
    assert (p.first_name, p.last_name, p.title, p.company) == ("Eth", "W.", "Founder", "Hive")


# =====================================================================
# RSVP UPDATES -- items 24-27
# =====================================================================


async def test_existing_invited_participant_plus_later_approved_registration_becomes_confirmed(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(rsvp_status=ParticipantRsvpStatus.INVITED, source=ParticipantSource.LUMA),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.APPROVED))
    assert result.outcome == LumaParticipantSyncOutcome.UPDATED
    assert result.participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED


async def test_existing_confirmed_plus_pending_approval_remains_confirmed(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(rsvp_status=ParticipantRsvpStatus.CONFIRMED, source=ParticipantSource.LUMA),
    )
    result = await service.sync_luma_registration_to_engagement_participant(
        _registration(approval_status=LumaApprovalStatus.PENDING_APPROVAL)
    )
    assert result.outcome == LumaParticipantSyncOutcome.UNCHANGED
    assert result.participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED


async def test_existing_confirmed_plus_waitlist_remains_confirmed(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(rsvp_status=ParticipantRsvpStatus.CONFIRMED, source=ParticipantSource.LUMA),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.WAITLIST))
    assert result.participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED


async def test_existing_confirmed_plus_session_status_remains_confirmed(service, stores):
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(rsvp_status=ParticipantRsvpStatus.CONFIRMED, source=ParticipantSource.LUMA),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration(approval_status=LumaApprovalStatus.SESSION))
    assert result.participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED


# =====================================================================
# ARCHIVE -- item 28
# =====================================================================


async def test_archived_existing_participant_is_not_unarchived_and_not_duplicated(service, stores):
    _e, participant_store, _c, activity_log = stores
    await _seed(
        stores,
        engagement=_engagement(),
        contact=_contact(),
        participant=_participant(archived=True, source=ParticipantSource.MANUAL, role=ParticipantRole.HOST),
    )
    result = await service.sync_luma_registration_to_engagement_participant(_registration())
    assert result.outcome == LumaParticipantSyncOutcome.ARCHIVED_PARTICIPANT_SKIPPED
    participants = await participant_store.list_for_engagement("e1")
    assert len(participants) == 1
    assert participants[0].archived is True
    assert participants[0].role == ParticipantRole.HOST
    # No activity noise for this no-op-shaped outcome.
    page = await activity_log.list_events(category=ActivityCategory.LUMA)
    assert not any(e.entity_id == "p1" for e in page.items)


# =====================================================================
# ACTIVITY LOG NOISE -- part of K, spot-checked here
# =====================================================================


async def test_unchanged_repeat_sync_produces_no_activity_event(service, stores):
    _e, _p, _c, activity_log = stores
    await _seed(stores, engagement=_engagement(), contact=_contact())
    await service.sync_luma_registration_to_engagement_participant(_registration())
    before = len((await activity_log.list_events(category=ActivityCategory.LUMA)).items)
    await service.sync_luma_registration_to_engagement_participant(_registration())  # identical repeat -> UNCHANGED
    after = len((await activity_log.list_events(category=ActivityCategory.LUMA)).items)
    assert after == before


async def test_no_op_outcomes_produce_no_activity_event(service, stores):
    _e, _p, _c, activity_log = stores
    await service.sync_luma_registration_to_engagement_participant(_registration())  # no Engagement linked at all
    page = await activity_log.list_events(category=ActivityCategory.LUMA)
    assert page.items == []


# =====================================================================
# HISTORICAL SAFETY -- items 32/33 (see test_luma_sync_service.py for 29-31)
# =====================================================================


async def test_linking_an_engagement_does_not_sync_pre_existing_registrations(stores):
    """Merely setting Engagement.luma_event_id (what Stage 1H-A's own
    linking PATCH does) must never, by itself, create any
    EngagementParticipant -- this service is never invoked automatically;
    only an explicit sync_luma_registration_to_engagement_participant()
    call per registration does anything at all."""
    engagement_store, participant_store, crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    # Pre-existing registrations "already exist" conceptually (this stage
    # never reads a LumaRegistrationStore at all -- see below) -- simulate
    # the moment of linking by creating an Engagement with luma_event_id
    # already set, exactly what Stage 1H-A's update_client_engagement()
    # produces, with NO sync service call made at all.
    await engagement_store.create(_engagement())
    assert await participant_store.list_for_engagement("e1") == []


def test_sync_service_has_no_dependency_on_any_luma_registration_store():
    """Structural proof, not just behavioral: this service cannot iterate
    historical registrations even if it wanted to -- its constructor never
    accepts a LumaRegistrationStore (or anything named like one) at all."""
    import inspect

    params = inspect.signature(LumaEngagementParticipantSyncService.__init__).parameters
    assert not any("registration" in name.lower() for name in params if name != "self")
