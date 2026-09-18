"""
Campaigns list V1 (2026-09-17), lead-start progress redefinition
(2026-09-18) -- MailCampaignListService.list_campaigns(). Plain
in-memory stores directly, same convention as test_mail_leads_service.py.
A pure read-side aggregation over existing MailCampaign/MailEnrollment/
MailEnrollmentStep/MailSequenceStep data -- these tests exercise exactly
that join/aggregation logic, never touch persistence.
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
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.services.mail_campaign_list_service import MailCampaignListService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
MAILBOX_ID = "fd090fb1-81e1-4575-94e4-5c7c83fc0137"


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


def make_step(enrollment_id: str, campaign_id: str, contact_id: str, step_number: int, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step{step_number}",
        mail_campaign_id=campaign_id,
        enrollment_id=enrollment_id,
        crm_contact_id=contact_id,
        step_id=f"step-{step_number}",
        step_number=step_number,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0 if step_number == 1 else 3,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        mailbox_id=MAILBOX_ID,
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


# --- Available / Started / Progress (2026-09-18 redefinition) -------------


async def test_available_is_total_minus_leads_whose_step1_was_actually_sent(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Available Test"))
    for i in range(6):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}"))
    # 4 of 6 have a SENT Step 1 -- "started."
    for i in range(4):
        await stores["enrollment_step_store"].create(
            make_step(f"e{i}", "c1", f"contact{i}", 1, status=MailEnrollmentStepStatus.SENT)
        )

    [item] = (await service.list_campaigns()).items
    assert item.total_leads == 6
    assert item.available_leads == 2
    assert item.progress_percent == round((4 / 6) * 100, 1)


async def test_a_lead_who_replied_after_step1_sent_is_not_available(stores, service):
    """The user's own example: outreach already started, so this lead is
    NOT available even though it's now REPLIED, not ACTIVE."""
    await stores["campaign_store"].create(make_campaign("c1", "Replied Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.SENT)
    )

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 0
    assert item.progress_percent == 100.0


async def test_a_lead_whose_step1_never_sent_is_available_regardless_of_enrollment_status(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Not Started Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.progress_percent == 0.0


async def test_a_pending_queued_or_claimed_step1_leaves_the_lead_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Pending Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.QUEUED, sent_at=None)
    )

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1


async def test_a_failed_step1_attempt_still_counts_as_available_never_sent(stores, service):
    """A real provider-attempt failure is not a confirmed send -- the
    literal 'has the initial email been sent' rule keeps this lead
    Available, since no message was ever delivered."""
    await stores["campaign_store"].create(make_campaign("c1", "Failed Attempt Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.FAILED))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.FAILED, sent_at=None)
    )

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1
    assert item.progress_percent == 0.0


async def test_a_sent_step2_never_counts_toward_started_only_step1_does(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Step Number Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))
    # Step 1 never sent, but a (contrived) later step is -- started must
    # still be keyed on step_number == 1 specifically.
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 2, status=MailEnrollmentStepStatus.SENT)
    )

    [item] = (await service.list_campaigns()).items
    assert item.available_leads == 1


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


async def test_sortable_by_available(stores, service):
    await stores["campaign_store"].create(make_campaign("c-none-started", "None Started"))
    await stores["campaign_store"].create(make_campaign("c-all-started", "All Started"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c-none-started", "contact1"))
    await stores["enrollment_store"].create(make_enrollment("e2", "c-all-started", "contact2"))
    await stores["enrollment_step_store"].create(
        make_step("e2", "c-all-started", "contact2", 1, status=MailEnrollmentStepStatus.SENT)
    )

    page = await service.list_campaigns(sort_by="available", sort_dir="desc")
    assert [item.mail_campaign_id for item in page.items] == ["c-none-started", "c-all-started"]


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


async def test_completed_campaign_with_every_lead_started_shows_full_progress(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Done Campaign", status=MailCampaignStatus.COMPLETED))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.SENT)
    )
    [item] = (await service.list_campaigns()).items
    assert item.status == MailCampaignStatus.COMPLETED
    assert item.progress_percent == 100.0
    assert item.available_leads == 0


async def test_paused_campaign_with_partial_start_shows_real_partial_progress(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Paused Campaign", status=MailCampaignStatus.PAUSED))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1"))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.SENT)
    )
    [item] = (await service.list_campaigns()).items
    assert item.status == MailCampaignStatus.PAUSED
    assert item.progress_percent == 50.0
    assert item.available_leads == 1


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
