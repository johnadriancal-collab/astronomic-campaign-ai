"""
MailCampaignMailboxNextSendService -- proactive OAuth expiration warnings
(2026-09-17). Answers "what is the earliest QUEUED send time for each
mailbox assigned to this campaign," so a campaign detail page can compare
it against that mailbox's own estimated_expires_at.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailCampaign, MailCampaignStatus, MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.services.mail_campaign_mailbox_next_send_service import MailCampaignMailboxNextSendService
from app.services.mail_campaign_service import MailCampaignNotFound

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_campaign(campaign_id: str = "c1", **overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=campaign_id, name="Test Campaign", status=MailCampaignStatus.ACTIVE, created_at=NOW, updated_at=NOW
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_step(
    enrollment_id: str,
    campaign_id: str,
    step_number: int,
    mailbox_id: str | None,
    status: MailEnrollmentStepStatus = MailEnrollmentStepStatus.QUEUED,
    next_send_at: datetime | None = None,
    **overrides,
) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step{step_number}",
        mail_campaign_id=campaign_id,
        enrollment_id=enrollment_id,
        crm_contact_id=f"contact-{enrollment_id}",
        step_id=f"step-{step_number}",
        step_number=step_number,
        subject="Quick hello",
        body="Hi {{first_name}},",
        delay_days=0 if step_number == 1 else 3,
        reply_in_thread=True,
        status=status,
        mailbox_id=mailbox_id,
        next_send_at=next_send_at,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


@pytest_asyncio.fixture
async def stores():
    return {
        "campaign_store": MemoryMailCampaignStore(),
        "channel_store": MemoryMailCampaignMailboxStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailCampaignMailboxNextSendService(**stores)


async def test_unknown_campaign_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.get_next_send_by_mailbox("does-not-exist")


async def test_campaign_with_no_assigned_mailboxes_returns_empty(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    results = await service.get_next_send_by_mailbox("c1")
    assert results == []


async def test_earliest_queued_next_send_at_is_returned_per_mailbox(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", 1, "mb-1", next_send_at=NOW + timedelta(days=3))
    )
    await stores["enrollment_step_store"].create(
        make_step("e2", "c1", 1, "mb-1", next_send_at=NOW + timedelta(hours=6))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.mailbox_id == "mb-1"
    assert item.next_send_at == NOW + timedelta(hours=6)


async def test_mailbox_with_nothing_queued_returns_none_not_fabricated(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", 1, "mb-1", status=MailEnrollmentStepStatus.SENT, next_send_at=None)
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at is None


async def test_multiple_assigned_mailboxes_each_get_their_own_entry(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", 1, "mb-1", next_send_at=NOW + timedelta(days=1))
    )
    await stores["enrollment_step_store"].create(
        make_step("e2", "c1", 1, "mb-2", next_send_at=NOW + timedelta(days=2))
    )

    results = await service.get_next_send_by_mailbox("c1")
    by_mailbox = {item.mailbox_id: item.next_send_at for item in results}
    assert by_mailbox == {"mb-1": NOW + timedelta(days=1), "mb-2": NOW + timedelta(days=2)}


async def test_a_different_mailboxs_queued_step_never_leaks_into_this_mailboxs_result(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", 1, "mb-2", next_send_at=NOW + timedelta(hours=1))  # different mailbox
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.mailbox_id == "mb-1"
    assert item.next_send_at is None
