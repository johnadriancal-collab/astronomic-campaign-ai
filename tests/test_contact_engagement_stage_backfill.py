"""
Contacts CRM Stage 3C (2026-09-11) -- historical reconciliation driver.
Exercised directly against Memory stores; NEVER touches production. See
app/services/contact_engagement_stage_backfill.py for the driver, which
reuses Stage 3B's ContactEngagementSignalService verbatim.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.activity import ActivityCategory
from app.models.client_crm import Client, Engagement, EngagementParticipant, EngagementType
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.services.activity_log_service import ActivityLogService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.contact_engagement_stage_backfill import (
    HistoricalWriteOutcome,
    ReconciliationBucket,
    apply_historical_reconciliation,
    compute_historical_reconciliation_report,
    select_trigger_participant,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _client(client_id="c1", name="Hive ASMBLD", **overrides) -> Client:
    return Client(client_id=client_id, name=name, created_at=NOW, updated_at=NOW, **overrides)


def _engagement(engagement_id="e1", client_id="c1", event_date=date(2026, 9, 22), **overrides) -> Engagement:
    overrides.setdefault("title", "SF Investor Dinner")
    overrides.setdefault("engagement_type", EngagementType.DINNER)
    return Engagement(
        engagement_id=engagement_id, client_id=client_id, engagement_date=event_date, created_at=NOW, updated_at=NOW, **overrides
    )


def _contact(crm_contact_id="contact-1", **overrides) -> CrmContact:
    overrides.setdefault("first_name", "Kevin")
    overrides.setdefault("last_name", "Przybocki")
    return CrmContact(crm_contact_id=crm_contact_id, created_at=NOW, updated_at=NOW, **overrides)


def _participant(participant_id="p1", engagement_id="e1", client_id="c1", crm_contact_id="contact-1", **overrides) -> EngagementParticipant:
    overrides.setdefault("first_name", "Jane")
    overrides.setdefault("role", "guest")
    overrides.setdefault("updated_at", NOW)
    return EngagementParticipant(
        participant_id=participant_id, engagement_id=engagement_id, client_id=client_id,
        crm_contact_id=crm_contact_id, created_at=NOW, **overrides,
    )


@pytest.fixture
def stores():
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    crm_contact_store = MemoryCrmContactStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    return client_store, engagement_store, engagement_participant_store, crm_contact_store, activity_log, signal_service


async def _seed_basic(stores, *, contact_overrides=None, participant_overrides=None):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement())
    await crm_contact_store.create(_contact(**(contact_overrides or {})))
    participant = _participant(**(participant_overrides or {}))
    await engagement_participant_store.create(participant)
    return participant


async def _dry_run(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    return await compute_historical_reconciliation_report(client_store, engagement_store, engagement_participant_store, crm_contact_store)


# =====================================================================
# 1-2: dry run makes zero writes / zero Activity
# =====================================================================


async def test_dry_run_makes_zero_persistent_writes(stores):
    _cs, _es, _eps, crm_contact_store, _al, _sig = stores
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Cold"}}, participant_overrides={"rsvp_status": "confirmed"})
    before = await crm_contact_store.get("contact-1")

    await _dry_run(stores)

    after = await crm_contact_store.get("contact-1")
    assert after == before
    assert after.custom_fields.get("engagement_stage") == "Cold"


async def test_dry_run_makes_zero_activity_entries(stores):
    _cs, _es, _eps, _ccs, activity_log, _sig = stores
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed"})
    await _dry_run(stores)
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert page.items == []


# =====================================================================
# 3-7: eligibility buckets
# =====================================================================


@pytest.mark.parametrize("stage,bucket", [(None, ReconciliationBucket.ELIGIBLE_UNSET), ("Cold", ReconciliationBucket.ELIGIBLE_COLD)])
async def test_only_null_or_cold_are_eligible(stores, stage, bucket):
    custom_fields = {"engagement_stage": stage} if stage is not None else {}
    await _seed_basic(stores, contact_overrides={"custom_fields": custom_fields}, participant_overrides={"rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows[0].bucket == bucket
    assert report.rows[0].proposed_stage == "Interested"


async def test_interested_is_excluded_from_write_cohort(stores):
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Interested"}}, participant_overrides={"rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows[0].bucket == ReconciliationBucket.ALREADY_INTERESTED
    assert report.write_candidates == []


async def test_replied_is_protected(stores):
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Replied"}}, participant_overrides={"rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows[0].bucket == ReconciliationBucket.PROTECTED_REPLIED
    assert report.write_candidates == []


async def test_unresponsive_is_protected(stores):
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Unresponsive"}}, participant_overrides={"rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows[0].bucket == ReconciliationBucket.PROTECTED_UNRESPONSIVE
    assert report.write_candidates == []


async def test_unexpected_value_is_protected(stores):
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Some Future Value"}}, participant_overrides={"rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows[0].bucket == ReconciliationBucket.PROTECTED_UNEXPECTED
    assert report.write_candidates == []


# =====================================================================
# 8-12: role/signal eligibility
# =====================================================================


async def test_guest_confirmed_qualifies(stores):
    await _seed_basic(stores, participant_overrides={"role": "guest", "rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert len(report.rows) == 1


async def test_speaker_panelist_confirmed_qualifies(stores):
    await _seed_basic(stores, participant_overrides={"role": "speaker_panelist", "rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert len(report.rows) == 1


async def test_guest_attended_qualifies(stores):
    await _seed_basic(stores, participant_overrides={"role": "guest", "attendance_status": "attended"})
    report = await _dry_run(stores)
    assert len(report.rows) == 1


@pytest.mark.parametrize("role", ["client", "host", "astronomic_team", "other"])
async def test_ineligible_roles_excluded(stores, role):
    await _seed_basic(stores, participant_overrides={"role": role, "rsvp_status": "confirmed"})
    report = await _dry_run(stores)
    assert report.rows == []
    assert report.counts.total_positive_signal_contacts == 0


async def test_archived_participant_excluded(stores):
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed", "archived": True})
    report = await _dry_run(stores)
    assert report.rows == []


# =====================================================================
# 13-17: multiple qualifying participants / deterministic trigger selection
# =====================================================================


async def test_multiple_qualifying_participants_produce_one_proposed_write(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e1", "c1", date(2026, 9, 1)))
    await engagement_store.create(_engagement("e2", "c1", date(2026, 10, 1)))
    await crm_contact_store.create(_contact())
    await engagement_participant_store.create(_participant("p1", "e1", crm_contact_id="contact-1", rsvp_status="confirmed"))
    await engagement_participant_store.create(_participant("p2", "e2", crm_contact_id="contact-1", rsvp_status="confirmed"))

    report = await _dry_run(stores)
    assert len(report.rows) == 1
    assert report.rows[0].qualifying_participant_count == 2


async def test_deterministic_trigger_selection_is_reproducible(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e1", "c1", date(2026, 9, 1)))
    await engagement_store.create(_engagement("e2", "c1", date(2026, 10, 1)))
    await crm_contact_store.create(_contact())
    await engagement_participant_store.create(_participant("p1", "e1", crm_contact_id="contact-1", rsvp_status="confirmed"))
    await engagement_participant_store.create(_participant("p2", "e2", crm_contact_id="contact-1", rsvp_status="confirmed"))

    report_a = await _dry_run(stores)
    report_b = await _dry_run(stores)
    assert report_a.rows[0].trigger_participant_id == report_b.rows[0].trigger_participant_id


async def test_attended_beats_confirmed_regardless_of_date(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e-older", "c1", date(2026, 1, 1)))
    await engagement_store.create(_engagement("e-newer", "c1", date(2026, 12, 1)))
    await crm_contact_store.create(_contact())
    await engagement_participant_store.create(_participant("p-confirmed-newer", "e-newer", crm_contact_id="contact-1", rsvp_status="confirmed"))
    await engagement_participant_store.create(_participant("p-attended-older", "e-older", crm_contact_id="contact-1", attendance_status="attended"))

    report = await _dry_run(stores)
    assert report.rows[0].trigger_participant_id == "p-attended-older"
    assert report.rows[0].signal_type == "attendance_attended"


async def test_newest_event_date_wins_among_same_signal_strength(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e-older", "c1", date(2026, 1, 1)))
    await engagement_store.create(_engagement("e-newer", "c1", date(2026, 12, 1)))
    await crm_contact_store.create(_contact())
    await engagement_participant_store.create(_participant("p-older", "e-older", crm_contact_id="contact-1", rsvp_status="confirmed"))
    await engagement_participant_store.create(_participant("p-newer", "e-newer", crm_contact_id="contact-1", rsvp_status="confirmed"))

    report = await _dry_run(stores)
    assert report.rows[0].trigger_participant_id == "p-newer"


async def test_participant_id_stable_tiebreak_across_engagements(stores):
    """Same event date, same updated_at, different engagements -- only
    participant_id differs, which must be the final deterministic
    tie-break (ascending)."""
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, _sig = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e1", "c1", date(2026, 9, 1)))
    await engagement_store.create(_engagement("e2", "c1", date(2026, 9, 1)))  # identical date
    await crm_contact_store.create(_contact())
    await engagement_participant_store.create(_participant("p-zzz", "e1", crm_contact_id="contact-1", rsvp_status="confirmed", updated_at=NOW))
    await engagement_participant_store.create(_participant("p-aaa", "e2", crm_contact_id="contact-1", rsvp_status="confirmed", updated_at=NOW))

    report = await _dry_run(stores)
    assert report.rows[0].trigger_participant_id == "p-aaa"  # ascending, deterministic


# =====================================================================
# 18-21: frozen target write-mode safety
# =====================================================================


async def test_write_mode_only_processes_frozen_target_ids(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed"})
    await crm_contact_store.create(_contact(crm_contact_id="contact-2", first_name="Other"))
    await engagement_participant_store.create(_participant("p2", crm_contact_id="contact-2", rsvp_status="confirmed"))

    write_report = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert [r.crm_contact_id for r in write_report.results] == ["contact-1"]
    assert (await crm_contact_store.get("contact-2")).custom_fields.get("engagement_stage") is None


async def test_newly_qualifying_unapproved_id_is_not_written(stores):
    """A Contact not in the frozen target list is never touched, even if
    it would ALSO qualify."""
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed"})
    await crm_contact_store.create(_contact(crm_contact_id="contact-2", first_name="Other"))
    await engagement_participant_store.create(_participant("p2", crm_contact_id="contact-2", rsvp_status="confirmed"))

    await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert (await crm_contact_store.get("contact-1")).custom_fields.get("engagement_stage") == "Interested"
    assert (await crm_contact_store.get("contact-2")).custom_fields.get("engagement_stage") is None


async def test_duplicate_target_ids_are_deduped(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, activity_log, signal_service = stores
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed"})

    write_report = await apply_historical_reconciliation(
        ["contact-1", "contact-1", "contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service
    )

    assert write_report.duplicate_ids_deduped == 2
    assert len(write_report.results) == 1
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert len([e for e in page.items if e.event_type == "contact.engagement_stage.advanced"]) == 1


async def test_unknown_target_id_fails_safe(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    write_report = await apply_historical_reconciliation(
        ["does-not-exist"], engagement_store, engagement_participant_store, crm_contact_store, signal_service
    )
    assert write_report.results[0].outcome == HistoricalWriteOutcome.MISSING_CONTACT
    assert "does-not-exist" in write_report.unknown_ids


# =====================================================================
# 22-25: live re-evaluation at write time (race safety)
# =====================================================================


async def test_stage_changed_to_replied_before_write_is_a_no_op(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Cold"}}, participant_overrides={"rsvp_status": "confirmed"})
    # Simulate a human editing the Contact between dry-run and write.
    contact = await crm_contact_store.get("contact-1")
    await crm_contact_store.save(contact.model_copy(update={"custom_fields": {"engagement_stage": "Replied"}}))

    write_report = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert write_report.results[0].outcome == HistoricalWriteOutcome.STAGE_NOT_ADVANCEABLE
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Replied"


async def test_stage_already_advanced_to_interested_before_write_is_a_no_op(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Cold"}}, participant_overrides={"rsvp_status": "confirmed"})
    # Simulate a live Stage 3B event advancing them first (e.g. a fresh manual create).
    contact = await crm_contact_store.get("contact-1")
    await crm_contact_store.save(contact.model_copy(update={"custom_fields": {"engagement_stage": "Interested"}}))

    write_report = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert write_report.results[0].outcome == HistoricalWriteOutcome.ALREADY_INTERESTED


async def test_participant_changed_to_declined_before_write_with_no_other_positive_is_no_op(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    participant = await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Cold"}}, participant_overrides={"rsvp_status": "confirmed"})
    await engagement_participant_store.save(participant.model_copy(update={"rsvp_status": "declined"}))

    write_report = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert write_report.results[0].outcome == HistoricalWriteOutcome.NO_LONGER_POSITIVE
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Cold"


async def test_another_qualifying_participant_still_advances_using_live_state(stores):
    client_store, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await client_store.create(_client())
    await engagement_store.create(_engagement("e1", "c1"))
    await engagement_store.create(_engagement("e2", "c1"))
    await crm_contact_store.create(_contact(custom_fields={"engagement_stage": "Cold"}))
    p1 = _participant("p1", "e1", crm_contact_id="contact-1", rsvp_status="confirmed")
    await engagement_participant_store.create(p1)
    await engagement_participant_store.create(_participant("p2", "e2", crm_contact_id="contact-1", rsvp_status="confirmed"))
    # p1 goes stale between dry-run and write; p2 is still positive.
    await engagement_participant_store.save(p1.model_copy(update={"rsvp_status": "declined"}))

    write_report = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    assert write_report.results[0].outcome == HistoricalWriteOutcome.ADVANCED
    assert (await crm_contact_store.get("contact-1")).custom_fields["engagement_stage"] == "Interested"


# =====================================================================
# 26-29: idempotency, Activity/provenance data safety
# =====================================================================


async def test_idempotent_second_write_run(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, activity_log, signal_service = stores
    await _seed_basic(stores, participant_overrides={"rsvp_status": "confirmed"})

    first = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)
    assert first.results[0].outcome == HistoricalWriteOutcome.ADVANCED
    after_first = await crm_contact_store.get("contact-1")

    second = await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)
    assert second.results[0].outcome == HistoricalWriteOutcome.ALREADY_INTERESTED
    after_second = await crm_contact_store.get("contact-1")

    assert after_second.updated_at == after_first.updated_at
    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert len([e for e in page.items if e.event_type == "contact.engagement_stage.advanced"]) == 1


async def test_activity_only_on_actual_advancement(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, activity_log, signal_service = stores
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Replied"}}, participant_overrides={"rsvp_status": "confirmed"})

    await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    assert [e for e in page.items if e.event_type == "contact.engagement_stage.advanced"] == []


async def test_provenance_only_on_actual_advancement(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(stores, contact_overrides={"custom_fields": {"engagement_stage": "Unresponsive"}}, participant_overrides={"rsvp_status": "confirmed"})

    await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    contact = await crm_contact_store.get("contact-1")
    assert "field_provenance" not in contact.custom_fields


async def test_sibling_custom_fields_preserved_on_write(stores):
    _cs, engagement_store, engagement_participant_store, crm_contact_store, _al, signal_service = stores
    await _seed_basic(
        stores,
        contact_overrides={"custom_fields": {"investor_type": "Angel Investor", "engagement_stage": "Cold"}},
        participant_overrides={"rsvp_status": "confirmed"},
    )

    await apply_historical_reconciliation(["contact-1"], engagement_store, engagement_participant_store, crm_contact_store, signal_service)

    contact = await crm_contact_store.get("contact-1")
    assert contact.custom_fields["investor_type"] == "Angel Investor"
    assert contact.custom_fields["engagement_stage"] == "Interested"


# =====================================================================
# 30: Kevin-like acceptance case
# =====================================================================


async def test_kevin_like_null_rsvp_manual_participant_is_excluded(stores):
    """The exact data-integrity acceptance case: a manually-added Guest
    participant with NO rsvp_status and NO attendance_status set must
    never appear in the write cohort, regardless of any earlier
    assumption about what their RSVP 'should' be."""
    await _seed_basic(
        stores,
        contact_overrides={"first_name": "Kevin", "last_name": "Przybocki"},
        participant_overrides={"role": "guest", "source": "manual", "rsvp_status": None, "attendance_status": None},
    )
    report = await _dry_run(stores)
    assert report.rows == []
    assert report.counts.total_positive_signal_contacts == 0
