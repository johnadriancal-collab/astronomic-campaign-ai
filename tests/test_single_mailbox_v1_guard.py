"""
P0-3 -- single-mailbox V1 guard (2026-09-15).

Product decision: V1 sends through exactly ONE mailbox
(victoria@useastronomic.com in production; MAIL_SENDING_MAILBOX_ALLOWLIST
is set to exactly that mailbox's id -- see the Campaign Manager production
audit). The explicit worry this file exists to settle: "don't rely merely
on 'the other mailboxes will fail' -- prove the guard holds even when a
second mailbox is fully valid and connected, not just absent/broken."

tests/test_prepare_and_send_step.py's own "Controlled-test gate" section
already proves the allowlist gate fails closed for a single mailbox/
missing-allowlist scenario. This file adds the one thing that section
does NOT cover: a campaign with TWO simultaneously connected, fully
scoped (gmail.send-granted) mailboxes as channels -- proving the
allowlist, not connectivity or scope, is what actually decides which one
can ever send, and that this holds through real campaign ACTIVATION, not
just at the isolated prepare_and_send_step() call level.

No new architecture is added here (per instruction: "if the current
allowlist already guarantees this strongly, prove it with tests rather
than adding redundant architecture") -- this is proof-only, exercising
controlled_test_send_allowed() and prepare_and_send_step() exactly as
they already exist.
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timezone

import pytest
import pytest_asyncio

from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
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
from app.services.mail_sending_service import (
    MailSenderPort,
    MailSendingService,
    MailSendRequest,
    SendBlockReason,
    SendResult,
    controlled_test_send_allowed,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)

# The one mailbox V1 is allowed to send through -- stands in for
# victoria@useastronomic.com's real mailbox_id (never hardcoded here; the
# production value itself is not secret, but this file proves the
# MECHANISM, which is independent of which specific id is configured).
ALLOWED_MAILBOX_ID = "mbx-allowed"
OTHER_MAILBOX_ID = "mbx-other-connected"
RECIPIENT = "lead@example.com"


class RecordingSender(MailSenderPort):
    def __init__(self):
        self.prepare_calls: list[MailSendRequest] = []
        self.send_prepared_calls: list[MailSendRequest] = []

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.prepare_calls.append(request)
        return request

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        self.send_prepared_calls.append(prepared)
        return SendResult(
            provider_message_id=f"msg-{len(self.send_prepared_calls)}",
            provider_thread_id=f"thr-{len(self.send_prepared_calls)}",
            rfc_message_id=prepared.rfc_message_id,
        )


async def always_leader() -> bool:
    return True


def all_day_windows() -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{d}", mail_campaign_id="c1", day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


def make_connected_mailbox(mailbox_id: str) -> Mailbox:
    """A FULLY VALID, connected mailbox with the gmail.send scope granted
    -- deliberately indistinguishable from ALLOWED_MAILBOX_ID except for
    its id, so a passing test here can only be explained by the allowlist
    check itself, never by some other mailbox being broken/disconnected/
    unscoped."""
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name="Victoria Bennett", status=MailboxStatus.CONNECTED, google_user_id=f"g-{mailbox_id}",
        granted_scopes=["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.send"],
        connected_at=NOW, updated_at=NOW,
    )


@pytest_asyncio.fixture
async def mailbox_store():
    return MemoryMailboxStore()


@pytest_asyncio.fixture
async def channel_store():
    return MemoryMailCampaignMailboxStore()


@pytest_asyncio.fixture
async def svc(mailbox_store, channel_store):
    return MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=MemoryMailEnrollmentStore(),
        step_store=MemoryMailEnrollmentStepStore(), mailbox_store=mailbox_store,
        channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(), activity_log=ActivityLogService(MemoryActivityEventStore()),
    )


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _v1_allowlist_configured(monkeypatch):
    """Exactly the V1 product decision: one mailbox, one recipient
    allowlisted -- matching production's MAIL_SENDING_MAILBOX_ALLOWLIST
    being set to exactly victoria@useastronomic.com's mailbox_id."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", ALLOWED_MAILBOX_ID)
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", RECIPIENT)


async def _setup_two_mailbox_campaign(svc, mailbox_store, channel_store) -> None:
    await svc.campaign_store.create(
        MailCampaign(mail_campaign_id="c1", name="Multi-mailbox Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await mailbox_store.create(make_connected_mailbox(ALLOWED_MAILBOX_ID))
    await mailbox_store.create(make_connected_mailbox(OTHER_MAILBOX_ID))
    # BOTH mailboxes are configured as this campaign's send channels --
    # exactly the shape an operator accidentally leaving a second mailbox
    # attached to a campaign would produce.
    await channel_store.replace_for_campaign("c1", [ALLOWED_MAILBOX_ID, OTHER_MAILBOX_ID])


async def _enroll_assigned_to(svc, enrollment_id: str, mailbox_id: str) -> "MailEnrollmentStep":
    enrollment = MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=RECIPIENT, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=mailbox_id,
    )
    await svc.enrollment_store.create(enrollment)
    step1 = MailSequenceStep(step_id=f"s-{enrollment_id}", mail_campaign_id="c1", step_number=1, subject="Subj", body="Body.", delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW)
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    return row, step1


# =====================================================================
# The core P0-3 proof
# =====================================================================


async def test_allowlisted_mailbox_sends_other_connected_mailbox_in_same_campaign_cannot(svc, mailbox_store, channel_store):
    """The central P0-3 claim: a campaign with TWO fully valid, connected,
    gmail.send-scoped mailboxes -- one allowlisted, one not -- only ever
    sends through the allowlisted one. The other is blocked at the
    controlled-test gate, never reaches sender.prepare(), and is
    requeued (not permanently failed) rather than silently dropped."""
    await _setup_two_mailbox_campaign(svc, mailbox_store, channel_store)

    allowed_row, allowed_step = await _enroll_assigned_to(svc, "e-allowed", ALLOWED_MAILBOX_ID)
    other_row, other_step = await _enroll_assigned_to(svc, "e-other", OTHER_MAILBOX_ID)

    sender = RecordingSender()
    allowed_outcome = await svc.prepare_and_send_step(
        allowed_row, sender=sender, claimed_by="w1", sequence_steps=[allowed_step], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    other_outcome = await svc.prepare_and_send_step(
        other_row, sender=sender, claimed_by="w1", sequence_steps=[other_step], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert allowed_outcome.sent is True
    assert other_outcome.sent is False
    assert other_outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED

    # Exactly one provider call ever happened, and it was for the
    # allowlisted mailbox -- not "the other one happened to also fail for
    # an unrelated reason."
    assert len(sender.prepare_calls) == 1
    assert sender.prepare_calls[0].mailbox.mailbox_id == ALLOWED_MAILBOX_ID

    other_row_after = await svc.step_store.get(other_row.enrollment_step_id)
    assert other_row_after.status == MailEnrollmentStepStatus.QUEUED  # requeued, not FAILED/dropped


async def test_blocked_mailbox_row_has_no_side_effects_on_the_allowlisted_one(svc, mailbox_store, channel_store):
    """Processing order independence: blocking the non-allowlisted
    mailbox's row first must not affect the allowlisted mailbox's
    ability to send right after -- the gate is a pure per-attempt check,
    not shared mutable state."""
    await _setup_two_mailbox_campaign(svc, mailbox_store, channel_store)

    other_row, other_step = await _enroll_assigned_to(svc, "e-other", OTHER_MAILBOX_ID)
    allowed_row, allowed_step = await _enroll_assigned_to(svc, "e-allowed", ALLOWED_MAILBOX_ID)

    sender = RecordingSender()
    other_outcome = await svc.prepare_and_send_step(
        other_row, sender=sender, claimed_by="w1", sequence_steps=[other_step], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    allowed_outcome = await svc.prepare_and_send_step(
        allowed_row, sender=sender, claimed_by="w1", sequence_steps=[allowed_step], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert other_outcome.sent is False
    assert allowed_outcome.sent is True
    assert len(sender.prepare_calls) == 1


async def test_activation_does_not_bypass_the_mailbox_allowlist(svc, mailbox_store, channel_store):
    """Real campaign activation (MailCampaignStatus.ACTIVE, materialized
    Step 1 rows) is entirely upstream of prepare_and_send_step() -- it
    performs no allowlist check of its own and cannot be relied on to
    keep a non-allowlisted mailbox from sending. This test proves the
    gate, not activation, is what's actually load-bearing: an ACTIVE
    campaign whose ONLY channel is a non-allowlisted (but otherwise
    perfectly valid) mailbox still cannot send anything."""
    await svc.campaign_store.create(
        MailCampaign(mail_campaign_id="c1", name="Wrong Mailbox Only", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await mailbox_store.create(make_connected_mailbox(OTHER_MAILBOX_ID))
    await channel_store.replace_for_campaign("c1", [OTHER_MAILBOX_ID])

    row, step1 = await _enroll_assigned_to(svc, "e1", OTHER_MAILBOX_ID)
    sender = RecordingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED
    assert len(sender.prepare_calls) == 0


def test_controlled_test_send_allowed_is_exact_match_not_prefix_or_substring():
    """Defense against a subtle allowlist bug: 'mbx-allowed-2' must NOT
    match an allowlist of 'mbx-allowed', and 'lead@example.com.evil.com'
    must not match an allowlist of 'lead@example.com' -- exact string
    equality only, never startswith/in."""
    import app.services.mail_sending_service as mss

    original_mailbox = mss.settings.mail_sending_mailbox_allowlist
    original_recipient = mss.settings.mail_sending_recipient_allowlist
    try:
        mss.settings.mail_sending_mailbox_allowlist = "mbx-allowed"
        mss.settings.mail_sending_recipient_allowlist = "lead@example.com"

        assert controlled_test_send_allowed("mbx-allowed", "lead@example.com") is True
        assert controlled_test_send_allowed("mbx-allowed-2", "lead@example.com") is False
        assert controlled_test_send_allowed("mbx-allowed", "lead@example.com.evil.com") is False
        assert controlled_test_send_allowed("prefix-mbx-allowed", "lead@example.com") is False
    finally:
        mss.settings.mail_sending_mailbox_allowlist = original_mailbox
        mss.settings.mail_sending_recipient_allowlist = original_recipient
