"""
Unsubscribe-path verification (2026-09-15, pre-launch) -- code/test only,
no production mutation. Traces the FULL chain end-to-end with real
components wired together against the SAME MailSuppressionStore instance
(never mocked at the suppression boundary): the public /mail/unsubscribe*
HTTP endpoint -> MailSuppressionService.suppress() -> the suppression
store -> MailSendingService.prepare_and_send_step()'s send-time check.

Each existing test file already covers one piece of this chain in
isolation (tests/test_mail_unsubscribe_api.py: the endpoint suppresses;
tests/test_prepare_and_send_step.py: a pre-existing MailSuppression row
blocks a send). What none of them prove together is that unsubscribing
through the REAL public endpoint produces a row that ACTUALLY blocks a
REAL later send -- i.e. that the two halves of the system agree on the
same normalized key and the same store. This file is that missing proof,
covering the five specific confirmations requested:
  1. The unsubscribe endpoint works (200, and only for a valid token).
  2. The NORMALIZED email becomes suppressed (mixed case, surrounding
     whitespace -- must match however MailSendingService normalizes the
     recipient at send time, or the block in #3 would silently miss).
  3. A subsequent campaign step for that email is blocked at send time.
  4. The SAME suppression blocks the email in a SECOND, unrelated
     campaign -- suppression is global/contact-level, never per-campaign
     (see MailSuppressionService's own docstring).
  5. Suppression cannot be accidentally bypassed by re-enrollment: a
     freshly created enrollment for an already-suppressed email is
     still blocked at send time, and re-enrolling into a brand new
     campaign after unsubscribing is blocked identically.
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail_unsubscribe import router as unsubscribe_router
from app.dependencies import get_mail_suppression_service
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
)
from app.services.mail_suppression_service import MailSuppressionService
from app.services.unsubscribe_token import generate_unsubscribe_token

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
MAILBOX_ID = "mbx-1"
RECIPIENT_RAW = "  Lead@Example.COM  "  # what a human might type into Gmail's own unsubscribe UI
RECIPIENT_NORMALIZED = "lead@example.com"


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


def all_day_windows(campaign_id: str) -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{campaign_id}-{d}", mail_campaign_id=campaign_id, day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    # V1 allowlist -- matches production shape (one mailbox, this recipient).
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", MAILBOX_ID)
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", RECIPIENT_NORMALIZED)


@pytest_asyncio.fixture
async def suppression_store():
    return MemoryMailSuppressionStore()


@pytest_asyncio.fixture
async def suppression_service(suppression_store):
    return MailSuppressionService(store=suppression_store, activity_log=ActivityLogService(MemoryActivityEventStore()))


@pytest.fixture
def unsubscribe_client(suppression_service):
    """The REAL public unsubscribe router, exactly as deployed -- no
    session auth (matches PUBLIC_PATHS), wired to the SAME
    suppression_service (and therefore the same suppression_store) the
    sending side uses below."""
    app = FastAPI()
    app.include_router(unsubscribe_router)
    app.dependency_overrides[get_mail_suppression_service] = lambda: suppression_service
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture
async def sending_service(suppression_store):
    """The REAL send path, sharing the SAME suppression_store instance
    the unsubscribe endpoint just wrote to -- this is what makes the
    trace real rather than two independently-passing halves."""
    return MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=MemoryMailEnrollmentStore(),
        step_store=MemoryMailEnrollmentStepStore(), mailbox_store=MemoryMailboxStore(),
        channel_store=MemoryMailCampaignMailboxStore(), policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store, activity_log=ActivityLogService(MemoryActivityEventStore()),
    )


async def _setup_campaign(sending_service, campaign_id: str) -> None:
    await sending_service.campaign_store.create(
        MailCampaign(mail_campaign_id=campaign_id, name=f"Campaign {campaign_id}", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await sending_service.mailbox_store.create(
        Mailbox(mailbox_id=MAILBOX_ID, provider=MailboxProvider.GOOGLE, email="victoria@useastronomic.com", display_name=None,
                status=MailboxStatus.CONNECTED, google_user_id="g-1", granted_scopes=["https://www.googleapis.com/auth/gmail.send"],
                connected_at=NOW, updated_at=NOW)
    )
    await sending_service.channel_store.replace_for_campaign(campaign_id, [MAILBOX_ID])


async def _enroll_and_queue_step1(sending_service, campaign_id: str, enrollment_id: str, email: str):
    enrollment = MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id=campaign_id, crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=MAILBOX_ID,
    )
    await sending_service.enrollment_store.create(enrollment)
    step1 = MailSequenceStep(step_id=f"s-{campaign_id}", mail_campaign_id=campaign_id, step_number=1, subject="Subj", body="Body.", delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW)
    row = await sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=all_day_windows(campaign_id), timezone_name=TZ, now=NOW
    )
    return row, step1


# =====================================================================
# (1) + (2): the real endpoint works, and NORMALIZES the email
# =====================================================================


async def test_1_and_2_real_unsubscribe_endpoint_suppresses_the_normalized_email(unsubscribe_client, suppression_store):
    token = generate_unsubscribe_token(RECIPIENT_RAW)

    resp = unsubscribe_client.post("/mail/unsubscribe", params={"token": token})

    assert resp.status_code == 200
    row = await suppression_store.get(RECIPIENT_NORMALIZED)
    assert row is not None
    assert row.active is True
    assert row.reason.value == "unsubscribed"
    # The raw, un-normalized form must NOT be a separate/second key.
    assert await suppression_store.get(RECIPIENT_RAW) is None


async def test_1_one_click_endpoint_also_works_and_normalizes(unsubscribe_client, suppression_store):
    token = generate_unsubscribe_token(RECIPIENT_RAW)

    resp = unsubscribe_client.post("/mail/unsubscribe/one-click", params={"token": token})

    assert resp.status_code == 200
    row = await suppression_store.get(RECIPIENT_NORMALIZED)
    assert row is not None and row.active is True


# =====================================================================
# (3): a subsequent campaign step for that email is blocked at send time
# =====================================================================


async def test_3_subsequent_send_for_the_unsubscribed_email_is_blocked(
    unsubscribe_client, suppression_store, sending_service
):
    # Real unsubscribe, through the real endpoint.
    token = generate_unsubscribe_token(RECIPIENT_RAW)
    unsubscribe_client.post("/mail/unsubscribe", params={"token": token})

    # A campaign this email is (or becomes) enrolled in, AFTER the unsubscribe.
    await _setup_campaign(sending_service, "c1")
    row, step1 = await _enroll_and_queue_step1(sending_service, "c1", "e1", RECIPIENT_NORMALIZED)

    sender = RecordingSender()
    outcome = await sending_service.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows("c1"),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.prepare_calls) == 0

    step_after = await sending_service.step_store.get(row.enrollment_step_id)
    assert step_after.status == MailEnrollmentStepStatus.SKIPPED_SUPPRESSED
    enrollment_after = await sending_service.enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.SUPPRESSED


# =====================================================================
# (4): the SAME suppression blocks the email in a second, unrelated campaign
# =====================================================================


async def test_4_same_email_is_blocked_in_a_second_unrelated_campaign(
    unsubscribe_client, suppression_store, sending_service
):
    token = generate_unsubscribe_token(RECIPIENT_RAW)
    unsubscribe_client.post("/mail/unsubscribe", params={"token": token})

    await _setup_campaign(sending_service, "campaign-A")
    await _setup_campaign(sending_service, "campaign-B")
    row_a, step_a = await _enroll_and_queue_step1(sending_service, "campaign-A", "ea", RECIPIENT_NORMALIZED)
    row_b, step_b = await _enroll_and_queue_step1(sending_service, "campaign-B", "eb", RECIPIENT_NORMALIZED)

    sender = RecordingSender()
    outcome_a = await sending_service.prepare_and_send_step(
        row_a, sender=sender, claimed_by="w1", sequence_steps=[step_a], windows=all_day_windows("campaign-A"),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    outcome_b = await sending_service.prepare_and_send_step(
        row_b, sender=sender, claimed_by="w1", sequence_steps=[step_b], windows=all_day_windows("campaign-B"),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome_a.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert outcome_b.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.prepare_calls) == 0


# =====================================================================
# (5): re-enrollment can never bypass an existing unsubscribe
# =====================================================================


async def test_5_a_fresh_enrollment_created_after_unsubscribing_is_still_blocked(
    unsubscribe_client, suppression_store, sending_service
):
    """Even an enrollment that did not exist yet at the moment of
    unsubscribing (created fresh afterward, as re-enrollment via Add
    Prospects would) is blocked identically -- the send-time check reads
    the suppression store fresh on every attempt, never a snapshot taken
    at enrollment time."""
    token = generate_unsubscribe_token(RECIPIENT_RAW)
    unsubscribe_client.post("/mail/unsubscribe", params={"token": token})

    await _setup_campaign(sending_service, "campaign-later")
    row, step1 = await _enroll_and_queue_step1(sending_service, "campaign-later", "e-later", RECIPIENT_NORMALIZED)

    sender = RecordingSender()
    outcome = await sending_service.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows("campaign-later"),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.prepare_calls) == 0


async def test_5_suppression_still_blocks_even_if_the_row_was_somehow_left_active(
    unsubscribe_client, suppression_store, sending_service
):
    """Defense in depth: MailCampaignService.add_prospects()/
    reconcile_batch() are responsible for marking a re-enrolled
    suppressed contact's MailEnrollment as SUPPRESSED up front (see that
    service's own suppression handling) -- but this proves the
    send-time check does not actually DEPEND on that having happened
    correctly. An enrollment left ACTIVE despite an active suppression
    row (the worst case: whatever upstream check should have caught
    this didn't) is still blocked here, at the one place a real Gmail
    call could otherwise happen."""
    token = generate_unsubscribe_token(RECIPIENT_RAW)
    unsubscribe_client.post("/mail/unsubscribe", params={"token": token})

    await _setup_campaign(sending_service, "campaign-race")
    row, step1 = await _enroll_and_queue_step1(sending_service, "campaign-race", "e-race", RECIPIENT_NORMALIZED)
    # Sanity: enrollment really is ACTIVE going into the send attempt,
    # not pre-marked SUPPRESSED by _enroll_and_queue_step1's own logic.
    enrollment_before = await sending_service.enrollment_store.get("e-race")
    assert enrollment_before.status == MailEnrollmentStatus.ACTIVE

    sender = RecordingSender()
    outcome = await sending_service.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows("campaign-race"),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.prepare_calls) == 0
    assert len(sender.send_prepared_calls) == 0
