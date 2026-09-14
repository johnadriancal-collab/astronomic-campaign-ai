from datetime import datetime, timezone

import pytest

from app.models.client_crm import DIRECT_EVENTS_CLIENT_NAME, ParticipantRole
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.austin_forward_event_history_backfill import (
    ENGAGEMENT_TITLE,
    RowOutcome,
    apply_backfill,
    compute_dry_run_report,
)
from app.services.austin_forward_reconciliation_cohort import EXISTING_CONTACTS, NEW_CONTACTS, TOTAL_COHORT_SIZE
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _service():
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    luma_event_store = MemoryLumaEventStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        engagement_store=engagement_store,
        engagement_closeout_store=engagement_closeout_store,
        engagement_participant_store=engagement_participant_store,
        luma_event_store=luma_event_store,
        client_touchpoint_store=client_touchpoint_store,
        contact_engagement_signal_service=signal_service,
    )
    return service, activity_store


def _bare_contact(crm_contact_id: str, **overrides) -> CrmContact:
    defaults = dict(crm_contact_id=crm_contact_id, created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


async def _seed_full_cohort(service: ClientCrmService) -> None:
    """Seeds a CrmContact for every one of the 166 frozen cohort rows -- the
    55 EXISTING_CONTACTS get their real crm_contact_id directly, the 111
    NEW_CONTACTS get a freshly-made CrmContact carrying their cohort email/
    linkedin_url so the backfill's own email/LinkedIn resolution succeeds,
    exactly mirroring how those 111 people really were created by the
    earlier Austin Forward Contact reconciliation stage."""
    for row in EXISTING_CONTACTS:
        await service.crm_contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    for i, row in enumerate(NEW_CONTACTS):
        await service.crm_contact_store.create(
            _bare_contact(
                f"new-contact-{i}",
                first_name=row.first_name,
                last_name=row.last_name,
                email=row.email,
                linkedin_url=row.linkedin_url,
            )
        )


# --- dry run ---------------------------------------------------------------


async def test_dry_run_with_nothing_seeded_reports_everything_unresolved_except_would_link():
    service, _ = await _service()
    report = await compute_dry_run_report(service)
    assert report.client_id is None
    assert report.engagement_id is None
    assert report.counts.cohort_total == TOTAL_COHORT_SIZE
    # existing-cohort rows use their crm_contact_id directly (no Contact needed to classify
    # them as "would link" at dry-run time); new-cohort rows with no matching Contact yet
    # are correctly unresolved.
    assert report.counts.would_link == len(EXISTING_CONTACTS)
    assert report.counts.unresolved_contact == len(NEW_CONTACTS)


async def test_dry_run_after_seeding_contacts_resolves_new_contacts_too():
    service, _ = await _service()
    await _seed_full_cohort(service)
    report = await compute_dry_run_report(service)
    assert report.counts.would_link == TOTAL_COHORT_SIZE
    assert report.counts.unresolved_contact == 0


# --- write path --------------------------------------------------------------


async def test_full_backfill_links_all_166_with_correct_attendance_and_role():
    service, activity_store = await _service()
    await _seed_full_cohort(service)

    report = await apply_backfill(service)

    assert report.counts.cohort_total == TOTAL_COHORT_SIZE
    assert report.counts.linked == TOTAL_COHORT_SIZE
    assert report.counts.already_linked == 0
    assert report.counts.unresolved_contact == 0
    assert report.counts.errors == 0

    client = await service.client_store.get(report.client_id)
    assert client.name == DIRECT_EVENTS_CLIENT_NAME
    engagement = await service.engagement_store.get(report.engagement_id)
    assert engagement.title == ENGAGEMENT_TITLE
    assert engagement.location == "Austin, Texas"
    assert str(engagement.engagement_date) == "2026-09-10"

    participants = await service.engagement_participant_store.list_for_engagement(report.engagement_id)
    assert len(participants) == TOTAL_COHORT_SIZE
    for p in participants:
        assert p.attendance_status == "attended"
        assert p.source == "manual"

    role_counts: dict[str, int] = {}
    for p in participants:
        role_counts[p.role] = role_counts.get(p.role, 0) + 1
    host_count = sum(1 for r in EXISTING_CONTACTS + NEW_CONTACTS if r.role_in_event == "Host")
    sponsor_count = sum(1 for r in EXISTING_CONTACTS + NEW_CONTACTS if r.role_in_event == "Sponsors")
    guest_count = sum(1 for r in EXISTING_CONTACTS + NEW_CONTACTS if r.role_in_event == "Guest")
    assert role_counts.get(ParticipantRole.HOST.value, 0) == host_count
    assert role_counts.get(ParticipantRole.SPONSOR.value, 0) == sponsor_count
    assert role_counts.get(ParticipantRole.GUEST.value, 0) == guest_count


async def test_no_new_crm_contacts_created_by_the_backfill():
    service, _ = await _service()
    await _seed_full_cohort(service)
    before_count = len(await service.crm_contact_store.list())

    await apply_backfill(service)

    after_count = len(await service.crm_contact_store.list())
    assert after_count == before_count


async def test_existing_contact_fields_are_unmodified_by_the_backfill():
    """The one CrmContact write path this module could ever touch is the
    read-only snapshot population inside create_engagement_participant() --
    it must never write back to the Contact itself."""
    service, _ = await _service()
    await _seed_full_cohort(service)
    row = EXISTING_CONTACTS[0]
    before = await service.crm_contact_store.get(row.crm_contact_id)

    await apply_backfill(service)

    after = await service.crm_contact_store.get(row.crm_contact_id)
    assert before.email == after.email
    assert before.updated_at == after.updated_at
    assert before.custom_fields == after.custom_fields


# --- idempotency -------------------------------------------------------------


async def test_second_backfill_run_creates_zero_new_participants():
    service, activity_store = await _service()
    await _seed_full_cohort(service)

    first_report = await apply_backfill(service)
    assert first_report.counts.linked == TOTAL_COHORT_SIZE
    participants_after_first = await service.engagement_participant_store.list_for_engagement(first_report.engagement_id)
    activity_count_after_first = len(await activity_store.list())

    second_report = await apply_backfill(service)
    assert second_report.counts.linked == 0
    assert second_report.counts.already_linked == TOTAL_COHORT_SIZE
    # same Client and Engagement reused, never recreated
    assert second_report.client_id == first_report.client_id
    assert second_report.engagement_id == first_report.engagement_id

    participants_after_second = await service.engagement_participant_store.list_for_engagement(second_report.engagement_id)
    assert len(participants_after_second) == len(participants_after_first) == TOTAL_COHORT_SIZE
    assert len(await activity_store.list()) == activity_count_after_first

    all_clients = await service.client_store.list()
    assert sum(1 for c in all_clients if c.name == DIRECT_EVENTS_CLIENT_NAME) == 1
    all_engagements = await service.engagement_store.list_for_client(first_report.client_id)
    assert sum(1 for e in all_engagements if e.title == ENGAGEMENT_TITLE) == 1


async def test_dry_run_after_a_real_write_reports_everything_already_linked():
    service, _ = await _service()
    await _seed_full_cohort(service)
    await apply_backfill(service)

    report = await compute_dry_run_report(service)
    assert report.counts.would_link == 0
    assert report.counts.already_linked == TOTAL_COHORT_SIZE


# --- Event History integration: multiple sources, ordering, non-interference ---


async def test_austin_forward_engagement_history_hides_pseudo_client_and_shows_location():
    service, _ = await _service()
    row = EXISTING_CONTACTS[0]
    await service.crm_contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    report = await apply_backfill(service)
    assert report.counts.linked >= 1

    history = await service.list_contact_event_history(row.crm_contact_id)
    assert len(history) == 1
    assert history[0].event_name == ENGAGEMENT_TITLE
    assert history[0].client_name is None  # pseudo-client suppressed on this projection
    assert history[0].location == "Austin, Texas"
    assert history[0].attendance_status == "attended"


async def test_existing_luma_style_event_history_is_unaffected_by_austin_forward_backfill():
    """Regression: a Contact with an existing MANUAL/LUMA-style participation on a real
    Client dinner must keep showing it, unchanged, after Austin Forward's backfill runs --
    and that Client's name must NOT be suppressed (only the pseudo-client is)."""
    service, _ = await _service()
    real_client = await service.create_client({"name": "Hive ASMBLD"})
    real_engagement = await service.create_client_engagement(
        real_client.client_id,
        {"title": "SF Investor Dinner", "engagement_type": "dinner", "engagement_date": "2026-09-22", "location": "San Francisco, CA"},
    )
    await service.crm_contact_store.create(_bare_contact("luma-contact-1", email="investor@example.com"))
    await service.create_engagement_participant(
        real_client.client_id, real_engagement.engagement_id, {"crm_contact_id": "luma-contact-1", "attendance_status": "attended"}
    )

    before = await service.list_contact_event_history("luma-contact-1")
    assert len(before) == 1
    assert before[0].client_name == "Hive ASMBLD"

    # run Austin Forward backfill for an entirely different set of people -- must not touch this Contact
    await _seed_full_cohort(service)
    await apply_backfill(service)

    after = await service.list_contact_event_history("luma-contact-1")
    assert len(after) == 1
    assert after[0].engagement_id == before[0].engagement_id
    assert after[0].client_name == "Hive ASMBLD"
    assert after[0].attendance_status == "attended"


async def test_contact_with_both_a_client_dinner_and_austin_forward_shows_both_newest_first():
    service, _ = await _service()
    row = EXISTING_CONTACTS[0]
    await service.crm_contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))

    # an OLDER real client dinner this same person also attended
    real_client = await service.create_client({"name": "Applied Curiosity"})
    older_engagement = await service.create_client_engagement(
        real_client.client_id,
        {"title": "SF Investor Dinner", "engagement_type": "dinner", "engagement_date": "2026-01-15", "location": "San Francisco, CA"},
    )
    await service.create_engagement_participant(
        real_client.client_id, older_engagement.engagement_id, {"crm_contact_id": row.crm_contact_id, "attendance_status": "attended"}
    )

    await apply_backfill(service)  # Austin Forward is 2026-09-10 -- newer

    history = await service.list_contact_event_history(row.crm_contact_id)
    assert len(history) == 2
    assert history[0].event_name == ENGAGEMENT_TITLE  # newest first
    assert history[1].event_name == "SF Investor Dinner"
    assert history[1].client_name == "Applied Curiosity"


async def test_unresolved_new_contact_is_reported_not_silently_dropped_or_guessed():
    service, _ = await _service()
    # seed everyone EXCEPT one NEW_CONTACTS row, so it's genuinely unresolved
    for row in EXISTING_CONTACTS:
        await service.crm_contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    for i, row in enumerate(NEW_CONTACTS[1:], start=1):
        await service.crm_contact_store.create(
            _bare_contact(f"new-contact-{i}", email=row.email, linkedin_url=row.linkedin_url)
        )

    report = await apply_backfill(service)
    assert report.counts.unresolved_contact == 1
    assert report.counts.linked == TOTAL_COHORT_SIZE - 1
    unresolved = [r for r in report.results if r.outcome == RowOutcome.UNRESOLVED_CONTACT]
    assert len(unresolved) == 1
    assert unresolved[0].full_name == NEW_CONTACTS[0].full_name
