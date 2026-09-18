"""
MailCampaignMailboxNextSendService -- proactive OAuth expiration warnings
(2026-09-17, mailbox-attribution fix 2026-09-18). Answers "what is the
earliest future send that can be confidently attributed to each mailbox
assigned to this campaign," so a campaign detail page can compare it
against that mailbox's own estimated_expires_at.

2026-09-18: the original version filtered by step.mailbox_id, which is
null until a step is actually claimed -- these tests specifically cover
the fixed attribution (shared with MailboxMetricsService's Queue count,
see app/services/mail_step_mailbox_attribution.py).
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
from app.services.mail_campaign_mailbox_next_send_service import MailCampaignMailboxNextSendService
from app.services.mail_campaign_service import MailCampaignNotFound

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


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
    next_send_at: datetime | None = None,
    eligible_at: datetime | None = None,
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
        eligible_at=eligible_at,
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
    return MailCampaignMailboxNextSendService(**stores)


async def test_unknown_campaign_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.get_next_send_by_mailbox("does-not-exist")


async def test_campaign_with_no_assigned_mailboxes_returns_empty(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    results = await service.get_next_send_by_mailbox("c1")
    assert results == []


# --- 1. step.mailbox_id null but enrollment assigned_mailbox_id present ---


async def test_attributes_via_sticky_assigned_mailbox_even_when_step_mailbox_id_is_null(stores, service):
    """The core fix: a Step 2/3 row's own mailbox_id is null until IT is
    claimed, but the enrollment's sticky assigned_mailbox_id (set once
    Step 1 was claimed) already identifies the real sender."""
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", step_number=2, status=MailEnrollmentStepStatus.QUEUED, mailbox_id=None, next_send_at=NOW + timedelta(hours=6))
    )

    results = await service.get_next_send_by_mailbox("c1")
    by_mailbox = {r.mailbox_id: r.next_send_at for r in results}
    assert by_mailbox["mb-1"] == NOW + timedelta(hours=6)
    assert by_mailbox["mb-2"] is None


# --- 2. unassigned enrollment + exactly one campaign channel ---


async def test_attributes_via_single_channel_when_enrollment_not_yet_assigned(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED, mailbox_id=None, next_send_at=NOW + timedelta(hours=2))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.mailbox_id == "mb-1"
    assert item.next_send_at == NOW + timedelta(hours=2)


# --- 3. unassigned enrollment + multiple channels -> no guess ---


async def test_does_not_guess_for_unassigned_enrollment_on_multi_mailbox_campaign(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id=None))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED, mailbox_id=None, next_send_at=NOW + timedelta(hours=1))
    )

    results = await service.get_next_send_by_mailbox("c1")
    assert {r.mailbox_id: r.next_send_at for r in results} == {"mb-1": None, "mb-2": None}


# --- 4. earliest future send selected correctly ---


async def test_earliest_send_selected_across_multiple_waiting_steps_for_one_mailbox(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW + timedelta(days=3))
    )
    await stores["enrollment_step_store"].create(
        make_step("e2", "c1", status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW + timedelta(hours=6))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at == NOW + timedelta(hours=6)


async def test_a_claimed_step_preserving_its_next_send_at_can_be_the_earliest(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.CLAIMED, next_send_at=NOW + timedelta(minutes=5))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at == NOW + timedelta(minutes=5)


async def test_pending_step_falls_back_to_eligible_at_when_next_send_at_is_unresolved(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step(
            "e1",
            "c1",
            status=MailEnrollmentStepStatus.PENDING,
            next_send_at=None,
            eligible_at=NOW + timedelta(days=1),
        )
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at == NOW + timedelta(days=1)


async def test_a_step_with_neither_next_send_at_nor_eligible_at_is_never_fabricated(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.PENDING, next_send_at=None, eligible_at=None)
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at is None


# --- 5. sent/failed/skipped steps excluded ---


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
async def test_terminal_and_in_flight_steps_never_produce_a_next_send(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=status, next_send_at=NOW + timedelta(hours=1))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at is None


# --- 6. paused/non-sending campaign handled correctly ---


@pytest.mark.parametrize(
    "status",
    [
        MailCampaignStatus.DRAFT,
        MailCampaignStatus.READY,
        MailCampaignStatus.PAUSED,
        MailCampaignStatus.COMPLETED,
        MailCampaignStatus.ARCHIVED,
    ],
)
async def test_non_active_campaign_never_reports_a_next_send_even_with_queued_work(stores, service, status):
    await stores["campaign_store"].create(make_campaign("c1", status=status))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-1"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW + timedelta(hours=1))
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.next_send_at is None


# --- 7. active pilot production-equivalent case ---


async def test_active_single_mailbox_pilot_with_mixed_step_states_reports_the_real_earliest_send(stores, service):
    """Production-equivalent shape: one ACTIVE campaign, one channel
    mailbox, several enrollments in different real states (already sent
    Step 1 and waiting on Step 2, mid-sequence, already replied/stopped)."""
    await stores["campaign_store"].create(make_campaign("c1", status=MailCampaignStatus.ACTIVE))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-victoria"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-victoria"))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", assigned_mailbox_id="mb-victoria"))
    await stores["enrollment_store"].create(
        make_enrollment("e3", "c1", assigned_mailbox_id="mb-victoria", status=MailEnrollmentStatus.REPLIED)
    )
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", step_number=2, status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW + timedelta(days=2))
    )
    await stores["enrollment_step_store"].create(
        make_step("e2", "c1", step_number=1, status=MailEnrollmentStepStatus.CLAIMED, next_send_at=NOW + timedelta(minutes=10))
    )
    await stores["enrollment_step_store"].create(
        make_step("e3", "c1", step_number=1, status=MailEnrollmentStepStatus.SKIPPED_REPLIED, next_send_at=None)
    )

    [item] = await service.get_next_send_by_mailbox("c1")
    assert item.mailbox_id == "mb-victoria"
    assert item.next_send_at == NOW + timedelta(minutes=10)


# --- multi-mailbox sanity: a different mailbox's send never leaks -------


async def test_a_different_mailboxs_send_never_leaks_into_this_mailboxs_result(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["channel_store"].replace_for_campaign("c1", ["mb-1", "mb-2"])
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", assigned_mailbox_id="mb-2"))
    await stores["enrollment_step_store"].create(
        make_step("e1", "c1", status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW + timedelta(hours=1))
    )

    results = await service.get_next_send_by_mailbox("c1")
    by_mailbox = {r.mailbox_id: r.next_send_at for r in results}
    assert by_mailbox["mb-2"] == NOW + timedelta(hours=1)
    assert by_mailbox["mb-1"] is None
