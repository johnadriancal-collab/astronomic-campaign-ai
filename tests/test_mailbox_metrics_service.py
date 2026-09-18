"""
MailboxMetricsService -- real per-mailbox Campaigns/Emails Sent Today/
Queue counts for the Emails page (2026-09-18). See that module's own
docstring, in particular _attribute_queue_step() for exactly which Queue
steps count against which mailbox and why.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
)
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.services.mailbox_metrics_service import MailboxMetricsService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def make_campaign(campaign_id: str = "c1", status: MailCampaignStatus = MailCampaignStatus.ACTIVE, **overrides) -> MailCampaign:
    fields = dict(mail_campaign_id=campaign_id, name="Test Campaign", status=status, created_at=NOW, updated_at=NOW)
    fields.update(overrides)
    return MailCampaign(**fields)


def make_enrollment(
    enrollment_id: str,
    campaign_id: str,
    assigned_mailbox_id: str | None = None,
    status: MailEnrollmentStatus = MailEnrollmentStatus.ACTIVE,
    **overrides,
) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment="lead@example.com",
        status=status,
        enrolled_at=NOW,
        created_at=NOW,
        assigned_mailbox_id=assigned_mailbox_id,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_step(
    enrollment_id: str,
    campaign_id: str,
    step_number: int = 1,
    status: MailEnrollmentStepStatus = MailEnrollmentStepStatus.QUEUED,
    mailbox_id: str | None = None,
    sent_at: datetime | None = None,
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
        sent_at=sent_at,
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
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailboxMetricsService(**stores)


# --- Campaigns -----------------------------------------------------------


async def test_campaign_count_counts_a_mailbox_used_by_one_campaign(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].campaigns_count == 1


async def test_campaign_count_sums_distinct_campaigns_for_one_mailbox(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["campaign_store"].create(make_campaign("c2"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["channel_store"].replace_for_campaign("c2", ["mb-1"])

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].campaigns_count == 2


async def test_campaign_count_never_double_counts_within_one_campaign(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].campaigns_count == 1


@pytest.mark.parametrize(
    "status",
    [
        MailCampaignStatus.DRAFT,
        MailCampaignStatus.READY,
        MailCampaignStatus.ACTIVE,
        MailCampaignStatus.PAUSED,
        MailCampaignStatus.COMPLETED,
        MailCampaignStatus.ARCHIVED,
    ],
)
async def test_campaign_count_counts_a_channel_regardless_of_campaign_lifecycle_status(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1", status=status))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].campaigns_count == 1


async def test_campaign_count_is_zero_for_a_mailbox_with_no_channel_assignment(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])

    metrics = await service.compute_for_mailboxes(["mb-2"], NOW)
    assert metrics["mb-2"].campaigns_count == 0


# --- Emails Sent Today -----------------------------------------------------


async def test_emails_sent_today_counts_sent_steps_since_utc_midnight(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.SENT, mailbox_id="mb-1", sent_at=NOW - timedelta(hours=2))
    )

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].emails_sent_today == 1


async def test_emails_sent_today_aggregates_across_multiple_campaigns_for_one_mailbox(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["campaign_store"].create(make_campaign("c2"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.SENT, mailbox_id="mb-1", sent_at=NOW - timedelta(hours=1))
    )
    await stores["enrollment_step_store"].create(
        make_step("e2", "c2", status=MailEnrollmentStepStatus.SENT, mailbox_id="mb-1", sent_at=NOW - timedelta(hours=2))
    )

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].emails_sent_today == 2


async def test_emails_sent_today_excludes_a_send_from_before_utc_midnight(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    yesterday_utc_evening = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.SENT, mailbox_id="mb-1", sent_at=yesterday_utc_evening)
    )

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].emails_sent_today == 0


@pytest.mark.parametrize(
    "status",
    [
        MailEnrollmentStepStatus.QUEUED,
        MailEnrollmentStepStatus.CLAIMED,
        MailEnrollmentStepStatus.FAILED,
        MailEnrollmentStepStatus.SKIPPED_SUPPRESSED,
    ],
)
async def test_emails_sent_today_excludes_non_sent_statuses(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=status, mailbox_id="mb-1", next_send_at=NOW)
    )

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].emails_sent_today == 0


async def test_emails_sent_today_never_leaks_a_different_mailboxs_sends(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.SENT, mailbox_id="mb-2", sent_at=NOW - timedelta(hours=1))
    )

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].emails_sent_today == 0


async def test_emails_sent_today_is_zero_for_a_never_used_mailbox(stores, service):
    metrics = await service.compute_for_mailboxes(["mb-unused"], NOW)
    assert metrics["mb-unused"].emails_sent_today == 0


# --- Queue -----------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [MailEnrollmentStepStatus.PENDING, MailEnrollmentStepStatus.QUEUED, MailEnrollmentStepStatus.CLAIMED],
)
async def test_queue_counts_waiting_statuses_for_single_channel_campaign(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(make_step("e1", "c1", status=status, mailbox_id=None))

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].queue_count == 1


@pytest.mark.parametrize(
    "status",
    [
        MailEnrollmentStepStatus.SENT,
        MailEnrollmentStepStatus.FAILED,
        MailEnrollmentStepStatus.SKIPPED_REPLIED,
        MailEnrollmentStepStatus.SKIPPED_SUPPRESSED,
        MailEnrollmentStepStatus.SENDING,
        MailEnrollmentStepStatus.UNKNOWN,
    ],
)
async def test_queue_excludes_terminal_and_in_flight_statuses(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(make_step("e1", "c1", status=status, mailbox_id="mb-1"))

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].queue_count == 0


async def test_queue_attributes_a_claimed_enrollments_later_steps_via_sticky_assigned_mailbox(stores, service):
    """Step 2/3 waiting on an enrollment whose Step 1 already claimed a
    mailbox -- the step row's own mailbox_id is still null (only set at
    claim time), but the enrollment's sticky assigned_mailbox_id already
    identifies the sender confidently."""
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", step_number=2, status=MailEnrollmentStepStatus.QUEUED, mailbox_id=None)
    )

    metrics = await service.compute_for_mailboxes(["mb-1", "mb-2"], NOW)
    assert metrics["mb-1"].queue_count == 1
    assert metrics["mb-2"].queue_count == 0


async def test_queue_does_not_guess_for_an_unassigned_enrollment_on_a_multi_mailbox_campaign(stores, service):
    """The one genuinely ambiguous case: an enrollment that hasn't been
    assigned a mailbox yet, on a campaign with more than one channel
    mailbox -- never attributed to any single mailbox."""
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.PENDING, mailbox_id=None)
    )

    metrics = await service.compute_for_mailboxes(["mb-1", "mb-2"], NOW)
    assert metrics["mb-1"].queue_count == 0
    assert metrics["mb-2"].queue_count == 0


async def test_queue_aggregates_across_multiple_campaigns_for_one_mailbox(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["campaign_store"].create(make_campaign("c2"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["channel_store"].replace_for_campaign("c2", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_store"].create(make_enrollment("e2", "c2", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED))
    await stores["enrollment_step_store"].create(make_step("e2", "c2", status=MailEnrollmentStepStatus.QUEUED))

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].queue_count == 2


async def test_queue_is_zero_for_a_campaign_with_no_channel_mailboxes(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED))

    metrics = await service.compute_for_mailboxes(["mb-1"], NOW)
    assert metrics["mb-1"].queue_count == 0
