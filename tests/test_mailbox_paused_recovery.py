"""
MailSendingService.resume_mailbox_paused_enrollments() -- the Phase C
recovery sweep for enrollments paused because their sticky mailbox
became unavailable. NEVER touches assigned_mailbox_id. NEVER resumes a
PAUSED enrollment for any other/unrecognized reason.
"""

from datetime import datetime, timezone

import pytest

from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentPauseReason,
    MailEnrollmentStatus,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_sending_service import MailSendingService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def campaign_store():
    return MemoryMailCampaignStore()


@pytest.fixture
def enrollment_store():
    return MemoryMailEnrollmentStore()


@pytest.fixture
def mailbox_store():
    return MemoryMailboxStore()


@pytest.fixture
def channel_store():
    return MemoryMailCampaignMailboxStore()


@pytest.fixture
def svc(campaign_store, enrollment_store, mailbox_store, channel_store):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=MemoryMailEnrollmentStepStore(),
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(), activity_log=ActivityLogService(MemoryActivityEventStore()),
    )


def make_campaign(status=MailCampaignStatus.ACTIVE) -> MailCampaign:
    return MailCampaign(mail_campaign_id="c1", name="Test", status=status, created_at=NOW, updated_at=NOW)


def make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED) -> Mailbox:
    return Mailbox(mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com", display_name=None, status=status, google_user_id=f"g-{mailbox_id}", connected_at=NOW, updated_at=NOW)


def make_paused_enrollment(reason=MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE, assigned_mailbox_id="mbx-1") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="lead@example.com",
        status=MailEnrollmentStatus.PAUSED, paused_reason=reason, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id=assigned_mailbox_id,
    )


async def test_resumes_when_same_mailbox_is_connected_again(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 1
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.ACTIVE
    assert enrollment.paused_reason is None
    assert enrollment.assigned_mailbox_id == "mbx-1"  # never touched


async def test_does_not_resume_when_mailbox_still_needs_reauth(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.NEEDS_REAUTH))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.PAUSED


async def test_does_not_resume_when_mailbox_disconnected(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.DISCONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    assert (await enrollment_store.get("e1")).status == MailEnrollmentStatus.PAUSED


async def test_does_not_resume_when_mailbox_no_longer_selected_in_channels(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", [])  # mbx-1 removed from channels
    await enrollment_store.create(make_paused_enrollment())

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    assert (await enrollment_store.get("e1")).status == MailEnrollmentStatus.PAUSED


async def test_does_not_resume_when_campaign_is_not_active(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign(status=MailCampaignStatus.PAUSED))
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    assert (await enrollment_store.get("e1")).status == MailEnrollmentStatus.PAUSED


async def test_never_reassigns_a_different_mailbox_even_if_a_better_candidate_exists(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    """A SECOND mailbox being CONNECTED and selected must never cause a
    PAUSED enrollment's assigned_mailbox_id to change -- only ITS OWN
    sticky mailbox coming back can resume it."""
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox("mbx-1", status=MailboxStatus.NEEDS_REAUTH))  # still broken
    await mailbox_store.create(make_mailbox("mbx-2", status=MailboxStatus.CONNECTED))  # a fine alternative
    await channel_store.replace_for_campaign("c1", ["mbx-1", "mbx-2"])
    await enrollment_store.create(make_paused_enrollment(assigned_mailbox_id="mbx-1"))

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.assigned_mailbox_id == "mbx-1"  # never reassigned to mbx-2


async def test_unrelated_pause_reason_is_never_auto_resumed(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    """Structural proof of the pause-reason distinction: a PAUSED
    enrollment whose paused_reason is anything other than
    MAILBOX_UNAVAILABLE (simulated here as None -- "unknown/other
    reason") must never be touched by this sweep, even though its
    mailbox situation looks identical to the recoverable case."""
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    enrollment = make_paused_enrollment().model_copy(update={"paused_reason": None})
    await enrollment_store.create(enrollment)

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)

    assert resumed == 0
    assert (await enrollment_store.get("e1")).status == MailEnrollmentStatus.PAUSED


async def test_active_enrollments_are_untouched(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    active = make_paused_enrollment().model_copy(update={"status": MailEnrollmentStatus.ACTIVE, "paused_reason": None})
    await enrollment_store.create(active)

    resumed = await svc.resume_mailbox_paused_enrollments(NOW)
    assert resumed == 0


async def test_idempotent_repeated_calls_are_safe(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    first = await svc.resume_mailbox_paused_enrollments(NOW)
    second = await svc.resume_mailbox_paused_enrollments(NOW)
    assert first == 1
    assert second == 0  # already ACTIVE -- nothing left to resume


async def test_logs_a_structural_activity_event(svc, campaign_store, enrollment_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox(status=MailboxStatus.CONNECTED))
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment())

    await svc.resume_mailbox_paused_enrollments(NOW)

    events = await svc.activity_log.store.list()
    assert any(e.event_type == "mail_enrollment.resumed" for e in events)
