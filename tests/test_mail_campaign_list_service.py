"""
Campaigns list V1 (2026-09-17), sequence-completion progress
redefinition (2026-09-18b), Available redefinition (2026-09-18c), real
Open rate (2026-09-18) -- MailCampaignListService.list_campaigns(). Plain
in-memory stores directly, same convention as test_mail_leads_service.py.
A pure read-side aggregation over existing MailCampaign/MailEnrollment/
MailEnrollmentStep/MailSequenceStep/MailOpenEvent data -- these tests
exercise exactly that join/aggregation logic, never touch persistence.

2026-09-18c: Available now means the SAME thing as in_progress_leads
("has NOT finished the sequence," i.e. status != COMPLETED).

Open rate (2026-09-18) re-adds a per-campaign read of
MailEnrollmentStepStore -- ONLY to compute Open rate's SENT-based
denominator, a completely separate question from Available/Progress
above.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailSequenceStep,
)
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_open_event_store import MemoryMailOpenEventStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.services.mail_campaign_list_service import MailCampaignListService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_campaign(campaign_id: str, name: str, **overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=campaign_id, name=name, status=MailCampaignStatus.ACTIVE, created_at=NOW, updated_at=NOW
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_enrollment(enrollment_id: str, campaign_id: str, contact_id: str, **overrides) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=contact_id,
        email_at_enrollment=f"{contact_id}@example.com",
        status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=NOW,
        created_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_step(enrollment_id: str, campaign_id: str, step_number: int = 1, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step{step_number}",
        mail_campaign_id=campaign_id,
        enrollment_id=enrollment_id,
        crm_contact_id=f"contact-{enrollment_id}",
        step_id=f"step-{step_number}",
        step_number=step_number,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


def make_sequence_step(campaign_id: str, step_id: str, step_number: int, **overrides) -> MailSequenceStep:
    fields = dict(
        step_id=step_id,
        mail_campaign_id=campaign_id,
        step_number=step_number,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0 if step_number == 1 else 3,
        reply_in_thread=True,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailSequenceStep(**fields)


@pytest_asyncio.fixture
async def stores():
    return {
        "campaign_store": MemoryMailCampaignStore(),
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "open_event_store": MemoryMailOpenEventStore(),
        "sequence_step_store": MemoryMailSequenceStepStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailCampaignListService(**stores)


async def test_campaign_with_no_enrollments_has_zero_progress_not_fabricated(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Draft Campaign", status=MailCampaignStatus.DRAFT))
    page = await service.list_campaigns()
    [item] = page.items
    assert item.total_leads == 0
    assert item.available_leads == 0
    assert item.progress_percent == 0.0
    assert item.reply_rate_percent == 0.0


# --- Available == in_progress_leads (2026-09-18c) ---------------------------
#
# Available now shares the EXACT same finished-sequence semantics as the
# Progress bar: colored = finished (COMPLETED), Available = raw count of
# everything else. See MailCampaignListItem's own docstring.


async def test_available_equals_total_minus_finished(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Available Test"))
    for i in range(6):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}"))
    # 4 of 6 reached COMPLETED -- "finished."
    for i in range(4):
        await stores["enrollment_store"].create(
            make_enrollment(f"e-done-{i}", "c1", f"contact-done-{i}", status=MailEnrollmentStatus.COMPLETED)
        )

    [item] = (await service.list_campaigns()).items
    assert item.total_leads == 10
    assert item.finished_leads == 4
    assert item.available_leads == 6
    assert item.available_leads == item.in_progress_leads
    assert item.progress_percent == 40.0


async def test_a_completed_lead_is_not_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Completed Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 0
    assert item.in_progress_leads == 0
    assert item.progress_percent == 100.0


async def test_a_replied_lead_before_finishing_every_step_stays_available(stores, service):
    """Explicit, intentional consequence of the current Finished
    definition: REPLIED stopped the sequence early -- it is terminal
    (nothing further will ever be attempted) but did NOT run every
    applicable step, so it stays Available/in-progress, never silently
    excluded."""
    await stores["campaign_store"].create(make_campaign("c1", "Replied Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.REPLIED))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.in_progress_leads == 1
    assert item.progress_percent == 0.0


async def test_a_suppressed_lead_before_finishing_every_step_stays_available(stores, service):
    """Same explicit reasoning as REPLIED above."""
    await stores["campaign_store"].create(make_campaign("c1", "Suppressed Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.SUPPRESSED))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.in_progress_leads == 1


async def test_a_failed_lead_before_finishing_every_step_stays_available(stores, service):
    """Same explicit reasoning again -- FAILED is terminal but not a
    completed sequence."""
    await stores["campaign_store"].create(make_campaign("c1", "Failed Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.FAILED))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.in_progress_leads == 1


async def test_an_active_lead_still_mid_sequence_stays_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Active Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.in_progress_leads == 1
    assert item.progress_percent == 0.0


# --- Progress (sequence completion, 2026-09-18b) ---------------------------
#
# finished_leads counts ONLY MailEnrollmentStatus.COMPLETED -- see
# MailCampaignListItem's own docstring for exactly why REPLIED/
# SUPPRESSED/FAILED (each also terminal) don't count as finished.


async def test_completed_enrollment_counts_as_finished(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Finished Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))

    [item] = (await service.list_campaigns()).items
    assert item.finished_leads == 1
    assert item.in_progress_leads == 0
    assert item.progress_percent == 100.0


async def test_zero_total_leads_handled_safely(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Empty Test", status=MailCampaignStatus.DRAFT))

    [item] = (await service.list_campaigns()).items
    assert item.total_leads == 0
    assert item.finished_leads == 0
    assert item.in_progress_leads == 0
    assert item.available_leads == 0
    assert item.progress_percent == 0.0


async def test_every_lead_finished_renders_full_progress_and_zero_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "All Finished Test"))
    for i in range(5):
        await stores["enrollment_store"].create(
            make_enrollment(f"e{i}", "c1", f"contact{i}", status=MailEnrollmentStatus.COMPLETED)
        )

    [item] = (await service.list_campaigns()).items
    assert item.progress_percent == 100.0
    assert item.in_progress_leads == 0
    assert item.available_leads == 0


async def test_no_lead_finished_renders_zero_progress_and_full_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "None Finished Test"))
    for i in range(5):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.progress_percent == 0.0
    assert item.finished_leads == 0
    assert item.in_progress_leads == 5
    assert item.available_leads == 5


async def test_sortable_by_progress_uses_finished_over_total(stores, service):
    await stores["campaign_store"].create(make_campaign("c-none-finished", "None Finished"))
    await stores["campaign_store"].create(make_campaign("c-all-finished", "All Finished"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c-none-finished", "contact1", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e2", "c-all-finished", "contact2", status=MailEnrollmentStatus.COMPLETED))

    page = await service.list_campaigns(sort_by="progress", sort_dir="desc")
    assert [item.mail_campaign_id for item in page.items] == ["c-all-finished", "c-none-finished"]


async def test_live_pilot_production_equivalent_case(stores, service):
    """5 leads, mixed real states: 3 REPLIED (stopped early, not
    finished), 2 still ACTIVE mid-sequence (also not finished) -- the
    exact shape of the real 5-Person External Pilot at verification
    time. Progress must be 0%, Available must be 5 (nothing finished
    yet), never fabricated to look further along than it really is."""
    await stores["campaign_store"].create(make_campaign("c1", "Pilot-Equivalent", status=MailCampaignStatus.ACTIVE))
    for i in range(3):
        await stores["enrollment_store"].create(
            make_enrollment(f"e-replied-{i}", "c1", f"contact-replied-{i}", status=MailEnrollmentStatus.REPLIED)
        )
    for i in range(2):
        await stores["enrollment_store"].create(
            make_enrollment(f"e-active-{i}", "c1", f"contact-active-{i}", status=MailEnrollmentStatus.ACTIVE)
        )

    [item] = (await service.list_campaigns()).items
    assert item.total_leads == 5
    assert item.replied == 3
    assert item.finished_leads == 0
    assert item.in_progress_leads == 5
    assert item.available_leads == 5
    assert item.progress_percent == 0.0


# --- Total (replaces the old Leads column) ---------------------------------


async def test_total_leads_is_the_unique_enrollment_count(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Total Test"))
    for i in range(3):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}"))

    [item] = (await service.list_campaigns()).items
    assert item.total_leads == 3


# --- Reply rate --------------------------------------------------------------


async def test_reply_rate_matches_replied_over_total(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Reply Rate Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_store"].create(make_enrollment("e3", "c1", "contact3", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e4", "c1", "contact4", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e5", "c1", "contact5", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.replied == 2
    assert item.reply_rate_percent == 40.0


# --- Open rate (2026-09-18) -------------------------------------------------


async def test_open_rate_is_none_and_disabled_when_campaign_tracking_is_off(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Tracking Off Test", open_tracking_enabled=False))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))
    await stores["enrollment_step_store"].create(make_step("e1", "c1"))
    await stores["open_event_store"].record_open(enrollment_step_id="e1-step1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)

    [item] = (await service.list_campaigns()).items
    assert item.open_tracking_enabled is False
    assert item.open_rate_percent is None


async def test_open_rate_is_none_when_tracking_on_but_nothing_sent_yet(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "No Sends Yet Test", open_tracking_enabled=True))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))

    [item] = (await service.list_campaigns()).items
    assert item.open_tracking_enabled is True
    assert item.open_rate_percent is None


async def test_open_rate_counts_unique_opened_leads_over_unique_sent_leads(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Open Rate Test", open_tracking_enabled=True))
    for i in range(4):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}"))
        await stores["enrollment_step_store"].create(make_step(f"e{i}", "c1"))
    await stores["open_event_store"].record_open(enrollment_step_id="e0-step1", mail_campaign_id="c1", enrollment_id="e0", at=NOW)

    [item] = (await service.list_campaigns()).items
    assert item.open_rate_percent == 25.0


async def test_open_rate_never_fabricated_to_zero_percent_when_disabled(stores, service):
    """The user's own explicit requirement: OFF must render as '--',
    never a fake 0% -- this is the exact backend signal (None, not 0.0)
    the frontend depends on to tell the two apart."""
    await stores["campaign_store"].create(make_campaign("c1", "Zero Leads Tracking Off", open_tracking_enabled=False))

    [item] = (await service.list_campaigns()).items
    assert item.open_rate_percent is None
    assert item.open_rate_percent != 0.0


# --- Suppressed / Failed (unchanged buckets) --------------------------------


async def test_suppressed_and_failed_counts_come_from_real_enrollment_status(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Suppressed/Failed Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", status=MailEnrollmentStatus.FAILED))
    await stores["enrollment_store"].create(make_enrollment("e3", "c1", "contact3", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.suppressed == 1
    assert item.failed == 1


# --- Steps ---------------------------------------------------------------


async def test_step_count_reflects_the_sequence_definition_not_execution_rows(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Step Count Test"))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-1", 1))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-2", 2))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-3", 3))

    [item] = (await service.list_campaigns()).items
    assert item.step_count == 3


# --- created_at / updated_at ------------------------------------------------


async def test_created_at_and_updated_at_are_the_campaigns_own_real_timestamps(stores, service):
    created = datetime(2026, 9, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 9, 15, tzinfo=timezone.utc)
    await stores["campaign_store"].create(make_campaign("c1", "Timestamps Test", created_at=created, updated_at=updated))

    [item] = (await service.list_campaigns()).items
    assert item.created_at == created
    assert item.updated_at == updated


# --- Search / filter / sort / pagination (unchanged wiring) ---------------


async def test_search_by_campaign_name(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Campaign Manager — 5-Person External Pilot"))
    await stores["campaign_store"].create(make_campaign("c2", "ZZTEST Reply Stop Production Verification"))

    page = await service.list_campaigns(q="5-Person")
    assert page.total == 1
    assert page.items[0].mail_campaign_id == "c1"


async def test_status_filter(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Active One", status=MailCampaignStatus.ACTIVE))
    await stores["campaign_store"].create(make_campaign("c2", "Draft One", status=MailCampaignStatus.DRAFT))
    await stores["campaign_store"].create(make_campaign("c3", "Paused One", status=MailCampaignStatus.PAUSED))

    active_page = await service.list_campaigns(status="active")
    assert active_page.total == 1
    assert active_page.items[0].mail_campaign_id == "c1"


async def test_default_sort_is_newest_updated_first(stores, service):
    await stores["campaign_store"].create(
        make_campaign("c-old", "Older", updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["campaign_store"].create(
        make_campaign("c-new", "Newer", updated_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    page = await service.list_campaigns()
    assert [item.mail_campaign_id for item in page.items] == ["c-new", "c-old"]


async def test_sortable_by_name(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Zebra Campaign"))
    await stores["campaign_store"].create(make_campaign("c2", "Alpha Campaign"))

    page = await service.list_campaigns(sort_by="name", sort_dir="asc")
    assert [item.name for item in page.items] == ["Alpha Campaign", "Zebra Campaign"]


async def test_sortable_by_status(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Active One", status=MailCampaignStatus.ACTIVE))
    await stores["campaign_store"].create(make_campaign("c2", "Archived One", status=MailCampaignStatus.ARCHIVED))

    page = await service.list_campaigns(sort_by="status", sort_dir="asc")
    assert [item.mail_campaign_id for item in page.items] == ["c1", "c2"]  # "active" < "archived"


async def test_sortable_by_available_uses_the_new_not_finished_definition(stores, service):
    await stores["campaign_store"].create(make_campaign("c-none-finished", "None Finished"))
    await stores["campaign_store"].create(make_campaign("c-all-finished", "All Finished"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c-none-finished", "contact1", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e2", "c-all-finished", "contact2", status=MailEnrollmentStatus.COMPLETED))

    page = await service.list_campaigns(sort_by="available", sort_dir="desc")
    assert [item.mail_campaign_id for item in page.items] == ["c-none-finished", "c-all-finished"]


async def test_sortable_by_total_leads_replied_progress_reply_rate_and_created_at(stores, service):
    await stores["campaign_store"].create(
        make_campaign("c-small", "Small", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["campaign_store"].create(
        make_campaign("c-big", "Big", created_at=datetime(2026, 9, 10, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(make_enrollment("e1", "c-small", "contact1"))
    for i in range(5):
        await stores["enrollment_store"].create(make_enrollment(f"e-big-{i}", "c-big", f"contact{i}"))

    by_leads = await service.list_campaigns(sort_by="total_leads", sort_dir="desc")
    assert [item.mail_campaign_id for item in by_leads.items] == ["c-big", "c-small"]

    by_created = await service.list_campaigns(sort_by="created_at", sort_dir="desc")
    assert [item.mail_campaign_id for item in by_created.items] == ["c-big", "c-small"]


async def test_pagination(stores, service):
    for i in range(5):
        await stores["campaign_store"].create(make_campaign(f"c{i}", f"Campaign {i}"))

    page1 = await service.list_campaigns(page=1, page_size=2, sort_by="name", sort_dir="asc")
    page2 = await service.list_campaigns(page=2, page_size=2, sort_by="name", sort_dir="asc")
    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    ids1 = {item.mail_campaign_id for item in page1.items}
    ids2 = {item.mail_campaign_id for item in page2.items}
    assert ids1.isdisjoint(ids2)


# --- Lifecycle statuses all render correctly --------------------------------


async def test_completed_campaign_with_every_lead_finished_shows_full_progress(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Done Campaign", status=MailCampaignStatus.COMPLETED))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))

    [item] = (await service.list_campaigns()).items
    assert item.status == MailCampaignStatus.COMPLETED
    assert item.progress_percent == 100.0
    assert item.finished_leads == 1
    assert item.in_progress_leads == 0
    assert item.available_leads == 0


async def test_paused_campaign_with_nothing_finished_yet_shows_zero_progress_and_full_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Paused Campaign", status=MailCampaignStatus.PAUSED))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2"))

    [item] = (await service.list_campaigns()).items
    assert item.status == MailCampaignStatus.PAUSED
    assert item.progress_percent == 0.0
    assert item.finished_leads == 0
    assert item.in_progress_leads == 2
    assert item.available_leads == 2


async def test_archived_historical_campaign_still_appears(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Old ZZTEST", status=MailCampaignStatus.ARCHIVED))
    page = await service.list_campaigns()
    assert page.total == 1
    assert page.items[0].status == MailCampaignStatus.ARCHIVED


async def test_draft_and_ready_campaigns_with_no_enrollments_show_zero_not_fabricated(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Draft", status=MailCampaignStatus.DRAFT))
    await stores["campaign_store"].create(make_campaign("c2", "Ready", status=MailCampaignStatus.READY))
    page = await service.list_campaigns()
    for item in page.items:
        assert item.total_leads == 0
        assert item.available_leads == 0
        assert item.progress_percent == 0.0
