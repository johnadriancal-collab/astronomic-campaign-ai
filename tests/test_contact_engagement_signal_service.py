"""
Contacts CRM Stage 3B (2026-09-11) -- ContactEngagementSignalService, the
one canonical EngagementParticipant -> Contact engagement-stage
reconciliation function. Exercised directly against Memory stores,
independent of the two live call sites (ClientCrmService's manual
create/update, LumaEngagementParticipantSyncService's create/update --
see their own dedicated trigger-path tests for that half).
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory, ActivitySource
from app.models.client_crm import EngagementParticipant, ParticipantAttendanceStatus, ParticipantRole, ParticipantRsvpStatus
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.contact_engagement_signal_service import (
    ContactEngagementSignalOutcome,
    ContactEngagementSignalService,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _contact(crm_contact_id="contact-1", **overrides) -> CrmContact:
    return CrmContact(crm_contact_id=crm_contact_id, created_at=NOW, updated_at=NOW, **overrides)


def _participant(
    participant_id="p1", engagement_id="e1", client_id="c1", crm_contact_id="contact-1", **overrides
) -> EngagementParticipant:
    overrides.setdefault("first_name", "Jane")
    overrides.setdefault("role", ParticipantRole.GUEST)
    return EngagementParticipant(
        participant_id=participant_id, engagement_id=engagement_id, client_id=client_id,
        crm_contact_id=crm_contact_id, created_at=NOW, updated_at=NOW, **overrides,
    )


@pytest_asyncio.fixture
async def stores():
    crm_contact_store = MemoryCrmContactStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return crm_contact_store, activity_log


@pytest_asyncio.fixture
async def service(stores):
    crm_contact_store, activity_log = stores
    return ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)


# =====================================================================
# Positive signal (1-5)
# =====================================================================


async def test_guest_confirmed_stage_null_advances_to_interested(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.ADVANCED
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


async def test_guest_confirmed_stage_cold_advances_to_interested(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Cold"}))
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.ADVANCED
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


async def test_guest_attended_rsvp_null_advances_to_interested(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(attendance_status="attended", rsvp_status=None))
    assert result.outcome == ContactEngagementSignalOutcome.ADVANCED


async def test_speaker_panelist_confirmed_advances(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(role="speaker_panelist", rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.ADVANCED


async def test_speaker_panelist_attended_advances(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(role="speaker_panelist", attendance_status="attended"))
    assert result.outcome == ContactEngagementSignalOutcome.ADVANCED


# =====================================================================
# Role exclusions (6-9)
# =====================================================================


@pytest.mark.parametrize("role", ["client", "host", "astronomic_team", "other"])
async def test_ineligible_roles_never_advance_even_when_confirmed(service, stores, role):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(role=role, rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_INELIGIBLE_ROLE
    assert (await crm_contact_store.get("contact-1")).custom_fields.get("engagement_stage") is None


# =====================================================================
# Negative / non-positive (10-14)
# =====================================================================


async def test_guest_invited_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(rsvp_status="invited"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL


async def test_guest_declined_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(rsvp_status="declined"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL


async def test_guest_no_show_only_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(attendance_status="no_show"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL


async def test_guest_cancelled_only_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(attendance_status="cancelled"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL


async def test_null_rsvp_and_null_attendance_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(rsvp_status=None, attendance_status=None))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL


# =====================================================================
# Never downgrade (15-18)
# =====================================================================


async def test_already_interested_confirmed_is_a_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Interested"}))
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


async def test_replied_confirmed_is_a_no_op_never_touched(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Replied"}))
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_STAGE_NOT_ADVANCEABLE
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Replied"


async def test_unresponsive_confirmed_is_a_no_op_never_touched(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Unresponsive"}))
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_STAGE_NOT_ADVANCEABLE
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Unresponsive"


async def test_unexpected_stage_value_confirmed_is_a_no_op_never_touched(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Some Future Value"}))
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_STAGE_NOT_ADVANCEABLE
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Some Future Value"


# =====================================================================
# Non-reversion (19-20)
# =====================================================================


async def test_advanced_then_declined_remains_interested(service, stores):
    """A later Declined isn't even a positive-signal candidate, so this
    correctly short-circuits to SKIPPED_NO_POSITIVE_SIGNAL without ever
    re-checking the Contact's stage -- the important assertion is that
    the stage itself is provably untouched afterward, never that any
    particular outcome enum fires."""
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"

    declined = _participant(rsvp_status="declined")
    result = await service.reconcile_from_participant(declined)
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


async def test_advanced_then_attendance_cancelled_remains_interested(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"

    cancelled = _participant(rsvp_status="confirmed", attendance_status="cancelled")
    result = await service.reconcile_from_participant(cancelled)
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


# =====================================================================
# Archived / unresolved / missing Contact (fail-safe conditions)
# =====================================================================


async def test_archived_participant_is_skipped(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed", archived=True))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_ARCHIVED_PARTICIPANT
    assert (await crm_contact_store.get("contact-1")).custom_fields.get("engagement_stage") is None


async def test_unresolved_participant_no_crm_contact_id_is_skipped(service):
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed", crm_contact_id=None))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_NO_CONTACT_LINK


async def test_missing_contact_fails_safe(service):
    """crm_contact_id is set but no such Contact exists in the store."""
    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed", crm_contact_id="does-not-exist"))
    assert result.outcome == ContactEngagementSignalOutcome.SKIPPED_CONTACT_NOT_FOUND


# =====================================================================
# Idempotency
# =====================================================================


async def test_repeated_processing_of_the_same_positive_participant_is_idempotent(service, stores):
    crm_contact_store, activity_log = stores
    await crm_contact_store.create(_contact())
    participant = _participant(rsvp_status="confirmed")

    first = await service.reconcile_from_participant(participant)
    assert first.outcome == ContactEngagementSignalOutcome.ADVANCED
    after_first = await crm_contact_store.get("contact-1")

    for _ in range(9):
        result = await service.reconcile_from_participant(participant)
        assert result.outcome == ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED

    after_ten = await crm_contact_store.get("contact-1")
    assert after_ten.updated_at == after_first.updated_at  # no further write ever happened
    assert after_ten.custom_fields == after_first.custom_fields

    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    advanced_events = [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"]
    assert len(advanced_events) == 1  # exactly once, not ten times


async def test_if_contact_already_interested_before_first_processing_zero_write_zero_activity(service, stores):
    crm_contact_store, activity_log = stores
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Interested"}))
    original = await crm_contact_store.get("contact-1")

    result = await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    assert result.outcome == ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED

    unchanged = await crm_contact_store.get("contact-1")
    assert unchanged.updated_at == original.updated_at

    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"] == []


# =====================================================================
# Data safety (sibling fields, provenance, Activity)
# =====================================================================


async def test_sibling_custom_fields_are_preserved_on_advancement(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(
        _contact(custom_fields={"investor_type": "Angel Investor", "check_size_personal": "$25k-$50k"})
    )
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    contact = await crm_contact_store.get("contact-1")
    assert contact.custom_fields["investor_type"] == "Angel Investor"
    assert contact.custom_fields["check_size_personal"] == "$25k-$50k"
    assert contact.custom_fields["engagement_stage"] == "Interested"


async def test_sibling_field_provenance_entries_are_preserved_on_advancement(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(
        _contact(custom_fields={"field_provenance": {"company": {"source": "luma_self_report", "luma_guest_id": "gst-9"}}})
    )
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    contact = await crm_contact_store.get("contact-1")
    assert contact.custom_fields["field_provenance"]["company"] == {"source": "luma_self_report", "luma_guest_id": "gst-9"}
    assert contact.custom_fields["field_provenance"]["engagement_stage"]["source"] == "engagement_participation"


async def test_provenance_not_written_on_no_op(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(rsvp_status="invited"))
    contact = await crm_contact_store.get("contact-1")
    assert "field_provenance" not in contact.custom_fields


async def test_provenance_has_correct_engagement_id_and_participant_id(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(participant_id="p-42", engagement_id="e-99", rsvp_status="confirmed"))
    contact = await crm_contact_store.get("contact-1")
    entry = contact.custom_fields["field_provenance"]["engagement_stage"]
    assert entry["engagement_id"] == "e-99"
    assert entry["participant_id"] == "p-42"


async def test_deterministic_signal_type_attendance_wins_over_rsvp_when_both_positive(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed", attendance_status="attended"))
    contact = await crm_contact_store.get("contact-1")
    assert contact.custom_fields["field_provenance"]["engagement_stage"]["signal_type"] == "attendance_attended"


async def test_signal_type_is_rsvp_confirmed_when_only_rsvp_is_positive(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(rsvp_status="confirmed"))
    contact = await crm_contact_store.get("contact-1")
    assert contact.custom_fields["field_provenance"]["engagement_stage"]["signal_type"] == "rsvp_confirmed"


async def test_activity_emitted_once_on_advancement_with_structural_metadata_only(service, stores):
    crm_contact_store, activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(
        _participant(participant_id="p-1", engagement_id="e-1", rsvp_status="confirmed")
    )
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    advanced = [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"]
    assert len(advanced) == 1
    event = advanced[0]
    assert event.category == ActivityCategory.CONTACTS
    assert event.source == ActivitySource.ENGAGEMENT_SIGNAL
    assert event.metadata == {
        "crm_contact_id": "contact-1",
        "from_stage": None,
        "to_stage": "Interested",
        "engagement_id": "e-1",
        "participant_id": "p-1",
        "signal_type": "rsvp_confirmed",
    }
    # No PII -- no email, name, or note text anywhere in the event.
    assert "Jane" not in str(event.metadata)
    assert event.entity_name is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rsvp_status": "invited"},
        {"rsvp_status": "declined"},
        {"attendance_status": "no_show"},
        {"attendance_status": "cancelled"},
        {"role": "client", "rsvp_status": "confirmed"},
    ],
)
async def test_no_activity_entry_on_any_no_op_or_skip(service, stores, kwargs):
    crm_contact_store, activity_log = stores
    await crm_contact_store.create(_contact())
    await service.reconcile_from_participant(_participant(**kwargs))
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"] == []


# =====================================================================
# Source agnostic (37)
# =====================================================================


async def test_equivalent_manual_and_luma_participant_states_produce_identical_stage_result(service, stores):
    crm_contact_store, _activity_log = stores
    await crm_contact_store.create(_contact(crm_contact_id="contact-manual"))
    await crm_contact_store.create(_contact(crm_contact_id="contact-luma"))

    manual_result = await service.reconcile_from_participant(
        _participant(crm_contact_id="contact-manual", source="manual", rsvp_status="confirmed")
    )
    luma_result = await service.reconcile_from_participant(
        _participant(crm_contact_id="contact-luma", source="luma", rsvp_status="confirmed")
    )

    assert manual_result.outcome == luma_result.outcome == ContactEngagementSignalOutcome.ADVANCED
    manual_contact = await crm_contact_store.get("contact-manual")
    luma_contact = await crm_contact_store.get("contact-luma")
    assert manual_contact.custom_fields["engagement_stage"] == luma_contact.custom_fields["engagement_stage"] == "Interested"
