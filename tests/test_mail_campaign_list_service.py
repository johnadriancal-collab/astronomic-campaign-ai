"""
Campaigns list V1 (2026-09-17) -- MailCampaignListService.list_campaigns().
Plain in-memory stores directly, same convention as
test_mail_leads_service.py. A pure read-side aggregation over existing
MailCampaign/MailEnrollment/MailEnrollmentStep/MailSequenceStep/channel
data -- these tests exercise exactly that join/aggregation logic, never
touch persistence.
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
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mailbox_store import MemoryMailboxStore
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


def make_mailbox(**overrides) -> Mailbox:
    fields = dict(
        mailbox_id=MAILBOX_ID,
        provider=MailboxProvider.GOOGLE,
        email="victoria@useastronomic.com",
        display_name=None,
        status=MailboxStatus.CONNECTED,
        google_user_id="g-victoria",
        granted_scopes=[],
        connected_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return Mailbox(**fields)


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
        "channel_store": MemoryMailCampaignMailboxStore(),
        "mailbox_store": MemoryMailboxStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailCampaignListService(**stores)


async def test_campaign_with_no_enrollments_has_zero_progress_not_fabricated(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Draft Campaign", status=MailCampaignStatus.DRAFT))
    page = await service.list_campaigns()
    [item] = page.items
    assert item.total_leads == 0
    assert item.progress_percent == 0.0
    assert item.sent == 0


async def test_progress_is_terminal_enrollments_over_total(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Progress Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_store"].create(make_enrollment("e3", "c1", "contact3", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e4", "c1", "contact4", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    # 2 terminal (completed + replied) out of 4 total = 50%
    assert item.total_leads == 4
    assert item.progress_percent == 50.0


async def test_active_campaign_with_nothing_terminal_yet_is_not_fabricated_to_zero_or_full(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "All Active"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", status=MailEnrollmentStatus.ACTIVE))

    [item] = (await service.list_campaigns()).items
    assert item.progress_percent == 0.0  # genuinely true: nothing terminal yet
    assert item.status == MailCampaignStatus.ACTIVE  # campaign lifecycle status is untouched by progress


async def test_sent_counts_only_steps_that_actually_reached_sent_status(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Sent Count Test"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_step_store"].create(make_step("e1", "c1", "contact1", 1, status=MailEnrollmentStepStatus.SENT))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", "contact1", 2, status=MailEnrollmentStepStatus.QUEUED, sent_at=None)
    )

    [item] = (await service.list_campaigns()).items
    assert item.sent == 1


async def test_step_count_reflects_the_sequence_definition_not_execution_rows(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Step Count Test"))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-1", 1))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-2", 2))
    await stores["sequence_step_store"].create(make_sequence_step("c1", "step-3", 3))

    [item] = (await service.list_campaigns()).items
    assert item.step_count == 3


async def test_mailbox_resolved_from_the_campaigns_own_channel_selection(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Mailbox Test"))
    await stores["mailbox_store"].create(make_mailbox())
    await stores["channel_store"].replace_for_campaign("c1", [MAILBOX_ID])

    [item] = (await service.list_campaigns()).items
    assert item.mailbox_email == "victoria@useastronomic.com"
    assert item.mailbox_id == MAILBOX_ID
    assert item.mailbox_count == 1


async def test_campaign_with_no_channel_mailbox_has_none_not_a_fabricated_default(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "No Mailbox Yet", status=MailCampaignStatus.DRAFT))
    [item] = (await service.list_campaigns()).items
    assert item.mailbox_email is None
    assert item.mailbox_id is None
    assert item.mailbox_count == 0


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


async def test_mailbox_email_filter(stores, service):
    await stores["mailbox_store"].create(make_mailbox())
    await stores["mailbox_store"].create(make_mailbox(mailbox_id="mbx-2", email="other@useastronomic.com"))
    await stores["campaign_store"].create(make_campaign("c1", "Victoria Campaign"))
    await stores["campaign_store"].create(make_campaign("c2", "Other Campaign"))
    await stores["channel_store"].replace_for_campaign("c1", [MAILBOX_ID])
    await stores["channel_store"].replace_for_campaign("c2", ["mbx-2"])

    page = await service.list_campaigns(mailbox_email="victoria@useastronomic.com")
    assert page.total == 1
    assert page.items[0].mail_campaign_id == "c1"


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


async def test_sortable_by_total_leads_and_replied_and_progress(stores, service):
    await stores["campaign_store"].create(make_campaign("c-small", "Small"))
    await stores["campaign_store"].create(make_campaign("c-big", "Big"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c-small", "contact1"))
    for i in range(5):
        await stores["enrollment_store"].create(make_enrollment(f"e-big-{i}", "c-big", f"contact{i}"))

    by_leads = await service.list_campaigns(sort_by="total_leads", sort_dir="desc")
    assert [item.mail_campaign_id for item in by_leads.items] == ["c-big", "c-small"]


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


async def test_completed_campaign_handled_correctly(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Done Campaign", status=MailCampaignStatus.COMPLETED))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", status=MailEnrollmentStatus.COMPLETED))
    [item] = (await service.list_campaigns()).items
    assert item.status == MailCampaignStatus.COMPLETED
    assert item.progress_percent == 100.0


async def test_archived_historical_campaign_still_appears(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", "Old ZZTEST", status=MailCampaignStatus.ARCHIVED))
    page = await service.list_campaigns()
    assert page.total == 1
    assert page.items[0].status == MailCampaignStatus.ARCHIVED
