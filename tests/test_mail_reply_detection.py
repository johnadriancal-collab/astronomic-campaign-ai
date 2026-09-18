"""
Reply Detection / Reply-Stop V1 (2026-09-15) -- covers the whole feature:
MailReplyDetectionService.poll_for_replies() (matching rule, resilience),
MailSendingService.mark_enrollment_replied()/list_reply_poll_candidates()
(the idempotent write side and the polling-eligibility rule), and the
load-bearing final reply check inside prepare_and_send_step() (the actual
reply-stop, independent of whatever poll_for_replies() itself managed to
skip).

Fixture conventions mirror tests/test_mail_reply_threading.py exactly
(mailbox/campaign/allowlist setup, RecordingSender, _send_step) -- this
file is self-contained rather than importing from that one, matching
this codebase's established per-test-file convention (see e.g.
test_mail_execution_worker.py's own near-identical duplicated fixtures).
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

from app.google.gmail_thread_reader_client import GmailReadProviderError
from app.google.oauth_client import GoogleRefreshTokenInvalidError
from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
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
from app.repositories.mail_reply_store import MailReply, MemoryMailReplyStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_reply_detection_service import MailReplyDetectionService
from app.services.mail_sending_service import (
    MailSenderPort,
    MailSendingService,
    MailSendRequest,
    SendBlockReason,
    SendResult,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
PACING_STEP = timedelta(seconds=60)


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


def make_mailbox(mailbox_id="mbx-1") -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=MailboxStatus.CONNECTED, google_user_id=f"g-{mailbox_id}",
        granted_scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.metadata"],
        connected_at=NOW, updated_at=NOW,
    )


def make_campaign(status: MailCampaignStatus = MailCampaignStatus.ACTIVE) -> MailCampaign:
    return MailCampaign(mail_campaign_id="c1", name="Reply Test", status=status, timezone=TZ, created_at=NOW, updated_at=NOW)


def make_sequence_step(step_id: str, step_number: int, reply_in_thread: bool = True) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number, subject=f"Subject {step_number}",
        body=f"Body {step_number}.", delay_days=0, reply_in_thread=reply_in_thread, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id="e1", email="lead@example.com", status=MailEnrollmentStatus.ACTIVE) -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=status, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )


@pytest_asyncio.fixture
async def svc():
    campaign_store = MemoryMailCampaignStore()
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=MemoryMailEnrollmentStore(),
        step_store=MemoryMailEnrollmentStepStore(), mailbox_store=mailbox_store, channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()), reply_store=MemoryMailReplyStore(),
    )
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    return service


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _controlled_test_gate_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr(
        "app.services.mail_sending_service.settings.mail_sending_recipient_allowlist",
        "lead@example.com,active@example.com,paused@example.com,broken@example.com,healthy@example.com",
    )


async def _enroll(svc, **kwargs) -> MailEnrollment:
    enrollment = make_enrollment(**kwargs)
    await svc.enrollment_store.create(enrollment)
    return enrollment


async def _send_step(svc, enrollment, step_number, sequence_step, sender=None, all_sequence_steps=None, now=None):
    """Same helper as test_mail_reply_threading.py's own -- see that
    file's docstring for the full explanation of why `all_sequence_steps`
    must be the FULL campaign sequence, not just the step being sent."""
    sender = sender or RecordingSender()
    all_sequence_steps = all_sequence_steps or [sequence_step]
    now = now or NOW
    if step_number == 1:
        row = await svc.create_step1_execution(
            enrollment=enrollment, step1=sequence_step, windows=all_day_windows(), timezone_name=TZ, now=now
        )
    else:
        row = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, sequence_step.step_id)
        assert row is not None, f"step {step_number} was not auto-materialized by the prior step's send"
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=all_sequence_steps, windows=all_day_windows(),
        timezone_name=TZ, now=now, confirm_leadership=always_leader,
    )
    sent_row = await svc.step_store.get(row.enrollment_step_id)
    return outcome, sender, sent_row


# =====================================================================
# MailSendingService.mark_enrollment_replied() / list_reply_poll_candidates()
# =====================================================================


async def test_mark_enrollment_replied_sets_status_and_timestamp(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    await _send_step(svc, enrollment, 1, step1)

    created = await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-msg-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )

    assert created is True
    updated = await svc.enrollment_store.get(enrollment.enrollment_id)
    assert updated.status == MailEnrollmentStatus.REPLIED
    assert updated.replied_at == NOW + PACING_STEP
    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply is not None
    assert reply.gmail_message_id == "reply-msg-1"


async def test_mark_enrollment_replied_skips_future_pending_step(svc):
    """(2) future step blocked -- a PENDING/QUEUED/CLAIMED row for a
    step that hasn't sent yet moves to SKIPPED_REPLIED the moment a
    reply is recorded, so the worker can never pick it up again."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    step2 = make_sequence_step("s2", 2)
    await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])
    materialized_step2 = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, "s2")
    assert materialized_step2.status == MailEnrollmentStepStatus.QUEUED

    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )

    step2_after = await svc.step_store.get(materialized_step2.enrollment_step_id)
    assert step2_after.status == MailEnrollmentStepStatus.SKIPPED_REPLIED


async def test_mark_enrollment_replied_never_touches_sent_history(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    _, _, sent1 = await _send_step(svc, enrollment, 1, step1)
    assert sent1.status == MailEnrollmentStepStatus.SENT

    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )

    step1_after = await svc.step_store.get(sent1.enrollment_step_id)
    assert step1_after.status == MailEnrollmentStepStatus.SENT
    assert step1_after == sent1


async def test_mark_enrollment_replied_is_idempotent(svc):
    """(3) duplicate processing idempotent -- a second call for the SAME
    enrollment does nothing further: no second step transition attempt
    (already-SKIPPED_REPLIED rows are simply left alone by the first
    call's own no-op iteration on a second pass), no second enrollment
    save, no second Activity Log event."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    await _send_step(svc, enrollment, 1, step1)

    first = await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )
    second = await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1-retry",
        reply_email_normalized="lead@example.com", now=NOW + 2 * PACING_STEP,
    )

    assert first is True
    assert second is False
    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply.gmail_message_id == "reply-1"  # the SECOND call never overwrote it
    events = await svc.activity_log.store.list()
    replied_events = [e for e in events if e.event_type == "mail_enrollment.replied"]
    assert len(replied_events) == 1


async def test_list_reply_poll_candidates_includes_active_and_paused_enrollments(svc):
    """(9)/(14) paused campaign/enrollment reply still recorded/polled --
    both an ACTIVE and a PAUSED enrollment (independent of the
    campaign's own lifecycle status) are real candidates."""
    active = await _enroll(svc, enrollment_id="e-active", email="active@example.com", status=MailEnrollmentStatus.ACTIVE)
    paused = await _enroll(svc, enrollment_id="e-paused", email="paused@example.com", status=MailEnrollmentStatus.PAUSED)
    step1 = make_sequence_step("s1", 1)
    for enrollment in (active, paused):
        row = await svc.step_store.create(
            MailEnrollmentStep(
                enrollment_step_id=f"sent-{enrollment.enrollment_id}", mail_campaign_id="c1",
                enrollment_id=enrollment.enrollment_id, crm_contact_id=enrollment.crm_contact_id, step_id="s1",
                step_number=1, subject="S", body="B", delay_days=0, reply_in_thread=True,
                status=MailEnrollmentStepStatus.SENT, sent_at=NOW, rfc_message_id=f"rfc-{enrollment.enrollment_id}",
                gmail_thread_id=f"thr-{enrollment.enrollment_id}", gmail_message_id=f"gm-{enrollment.enrollment_id}",
                mailbox_id="mbx-1", created_at=NOW, updated_at=NOW,
            )
        )

    candidates = await svc.list_reply_poll_candidates()

    candidate_ids = {c.enrollment.enrollment_id for c in candidates}
    assert candidate_ids == {"e-active", "e-paused"}


@pytest.mark.parametrize(
    "status",
    [MailEnrollmentStatus.REPLIED, MailEnrollmentStatus.SUPPRESSED, MailEnrollmentStatus.FAILED, MailEnrollmentStatus.COMPLETED],
)
async def test_list_reply_poll_candidates_excludes_terminal_statuses(svc, status):
    """(15) REPLIED no longer polled, (16) COMPLETED behavior explicit
    -- plus SUPPRESSED/FAILED, all four terminal-for-polling-purposes
    statuses named explicitly in list_reply_poll_candidates()'s own
    docstring."""
    enrollment = await _enroll(svc, status=status)
    await svc.step_store.create(
        MailEnrollmentStep(
            enrollment_step_id="sent-1", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
            crm_contact_id=enrollment.crm_contact_id, step_id="s1", step_number=1, subject="S", body="B",
            delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW,
            rfc_message_id="rfc-1", gmail_thread_id="thr-1", gmail_message_id="gm-1", mailbox_id="mbx-1",
            created_at=NOW, updated_at=NOW,
        )
    )

    candidates = await svc.list_reply_poll_candidates()

    assert candidates == []


async def test_list_reply_poll_candidates_excludes_enrollment_with_existing_reply(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    await _send_step(svc, enrollment, 1, step1)
    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )

    candidates = await svc.list_reply_poll_candidates()

    assert candidates == []


async def test_list_reply_poll_candidates_requires_at_least_one_sent_step(svc):
    await _enroll(svc)  # no SENT step at all -- e.g. still PENDING before Step 1 goes out
    candidates = await svc.list_reply_poll_candidates()
    assert candidates == []


async def test_list_reply_poll_candidates_does_not_require_campaign_active(svc):
    """Explicit, direct coverage of the deliberate correction: campaign
    status is NEVER checked here."""
    await svc.campaign_store.save(make_campaign(status=MailCampaignStatus.PAUSED))
    enrollment = await _enroll(svc)
    await svc.step_store.create(
        MailEnrollmentStep(
            enrollment_step_id="sent-1", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
            crm_contact_id=enrollment.crm_contact_id, step_id="s1", step_number=1, subject="S", body="B",
            delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW,
            rfc_message_id="rfc-1", gmail_thread_id="thr-1", gmail_message_id="gm-1", mailbox_id="mbx-1",
            created_at=NOW, updated_at=NOW,
        )
    )

    candidates = await svc.list_reply_poll_candidates()

    assert len(candidates) == 1
    assert candidates[0].enrollment.enrollment_id == enrollment.enrollment_id


# =====================================================================
# prepare_and_send_step()'s final, load-bearing reply gate
# =====================================================================


async def test_final_send_gate_blocks_when_reply_recorded_after_prep_succeeded(svc):
    """(11) final pre-send gate -- a reply recorded AFTER the early
    enrollment.status == ACTIVE check (which necessarily still passes,
    since mark_enrollment_replied() hasn't run yet) but BEFORE the
    provider call is still caught by the fresh, immediately-pre-send
    lookup. Same proof shape as
    test_final_suppression_check_is_load_bearing_even_after_prep_succeeded
    in test_prepare_and_send_step.py."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    row = await svc.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    sender = RecordingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        await svc.mark_enrollment_replied(
            enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
            reply_email_normalized="lead@example.com", now=NOW,
        )
        return await original_prepare(request)

    sender.prepare = wrapped_prepare

    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )

    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_REPLIED
    assert len(sender.prepare_calls) == 1  # prepared, but never sent
    assert len(sender.send_prepared_calls) == 0


async def test_reply_before_follow_up_blocks_that_follow_up(svc):
    """(7) reply before follow-up -- once a reply is recorded, Step 2's
    already-materialized row is never sent, even if something attempts
    to process it directly."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    step2 = make_sequence_step("s2", 2)
    await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])
    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )
    step2_row = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, "s2")
    assert step2_row.status == MailEnrollmentStepStatus.SKIPPED_REPLIED  # already stopped by the skip itself

    sender2 = RecordingSender()
    outcome2 = await svc.prepare_and_send_step(
        step2_row, sender=sender2, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + 2 * PACING_STEP, confirm_leadership=always_leader,
    )

    assert outcome2.sent is False
    assert outcome2.blocked_reason == SendBlockReason.ENROLLMENT_NOT_ACTIVE
    assert len(sender2.prepare_calls) == 0


async def test_reply_before_final_step_blocks_the_final_send(svc):
    """(8) reply before final -- same shape, three-step sequence, reply
    lands after step 1 and 2 have already sent, before step 3."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1)
    step2 = make_sequence_step("s2", 2)
    step3 = make_sequence_step("s3", 3)
    all_steps = [step1, step2, step3]
    await _send_step(svc, enrollment, 1, step1, all_sequence_steps=all_steps)
    await _send_step(svc, enrollment, 2, step2, all_sequence_steps=all_steps, now=NOW + PACING_STEP)

    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + 2 * PACING_STEP,
    )
    step3_row = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, "s3")
    assert step3_row.status == MailEnrollmentStepStatus.SKIPPED_REPLIED

    sender3 = RecordingSender()
    outcome3 = await svc.prepare_and_send_step(
        step3_row, sender=sender3, claimed_by="w1", sequence_steps=all_steps, windows=all_day_windows(),
        timezone_name=TZ, now=NOW + 3 * PACING_STEP, confirm_leadership=always_leader,
    )

    assert outcome3.sent is False
    assert len(sender3.send_prepared_calls) == 0


async def test_paused_then_replied_then_hypothetically_resumed_still_blocked(svc):
    """(13) paused -> reply -> resume -> still blocked -- once REPLIED,
    even a (currently hypothetical) resume back to ACTIVE can never
    reach a real send again for a step that was already
    SKIPPED_REPLIED, and mark_enrollment_replied() itself would need to
    run again to un-terminal it, which nothing in this codebase does."""
    enrollment = await _enroll(svc, status=MailEnrollmentStatus.PAUSED)
    step1 = make_sequence_step("s1", 1)
    step2 = make_sequence_step("s2", 2)
    row1 = MailEnrollmentStep(
        enrollment_step_id="sent-1", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s1", step_number=1, subject="S", body="B",
        delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW,
        rfc_message_id="rfc-1", gmail_thread_id="thr-1", gmail_message_id="gm-1", mailbox_id="mbx-1",
        created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(row1)
    step2_row = MailEnrollmentStep(
        enrollment_step_id="queued-2", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s2", step_number=2, subject=step2.subject,
        body=step2.body, delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.QUEUED,
        eligible_at=NOW, next_send_at=NOW, created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(step2_row)

    await svc.mark_enrollment_replied(
        enrollment, mailbox_id="mbx-1", gmail_thread_id="thr-1", gmail_message_id="reply-1",
        reply_email_normalized="lead@example.com", now=NOW + PACING_STEP,
    )
    # Simulate a hypothetical "resume" flipping the enrollment back to
    # ACTIVE (nothing in this codebase does this for a REPLIED
    # enrollment today -- this proves the step-level SKIPPED_REPLIED
    # state is itself durable, independent of the enrollment's status).
    resumed = (await svc.enrollment_store.get(enrollment.enrollment_id)).model_copy(
        update={"status": MailEnrollmentStatus.ACTIVE}
    )
    await svc.enrollment_store.save(resumed)

    step2_after = await svc.step_store.get(step2_row.enrollment_step_id)
    assert step2_after.status == MailEnrollmentStepStatus.SKIPPED_REPLIED

    sender = RecordingSender()
    outcome = await svc.prepare_and_send_step(
        step2_after, sender=sender, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + 2 * PACING_STEP, confirm_leadership=always_leader,
    )
    # A non-QUEUED row is never "due" in the first place; a direct call
    # against it (simulating a stale worker reference) simply loses the
    # claim race -- it is not, and can never become, SENDING.
    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.LOST_CLAIM_RACE
    assert len(sender.send_prepared_calls) == 0


# =====================================================================
# MailReplyDetectionService.poll_for_replies()
# =====================================================================


def _message(message_id: str, from_addr: str, in_reply_to: str | None = None, snippet: str | None = None) -> dict:
    headers = [{"name": "From", "value": from_addr}, {"name": "To", "value": "mbx-1@astronomic.com"}]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    message: dict = {"id": message_id, "payload": {"headers": headers}}
    if snippet is not None:
        message["snippet"] = snippet
    return message


def _thread(thread_id: str, messages: list[dict]) -> dict:
    return {"id": thread_id, "historyId": "1", "messages": messages}


class FakeMailboxService:
    def __init__(self, token: str = "tok-1", refresh_error: Exception | None = None):
        self.token = token
        self.refresh_error = refresh_error
        self.refresh_calls: list[str] = []

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        self.refresh_calls.append(mailbox_id)
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.token


class FakeGmailThreadReaderClient:
    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.errors: dict[str, Exception] = {}
        self.get_thread_calls: list[tuple[str, str]] = []

    async def get_thread(self, *, access_token: str, thread_id: str) -> dict:
        self.get_thread_calls.append((access_token, thread_id))
        if thread_id in self.errors:
            raise self.errors[thread_id]
        return self.threads.get(thread_id, _thread(thread_id, []))


async def _sent_step1(svc, enrollment, now=None, sender=None):
    """Sends Step 1 of a TWO-step sequence (Step 2 never sent) so the
    enrollment lands ACTIVE, not COMPLETED, afterward -- a single-step
    sequence would auto-complete the enrollment the moment its only step
    sends (see record_send_success()'s own docstring), which would make
    it un-pollable by construction and defeat the point of every
    detection-service test in this section. Callers sending more than
    one enrollment through the SAME mailbox must stagger `now` by at
    least PACING_STEP, same requirement as _send_step's own docstring.
    Callers sending more than one enrollment must also share ONE
    `sender` instance across those calls -- RecordingSender.send_
    prepared() derives its fabricated provider_thread_id from its own
    call count, so two independent RecordingSender instances would both
    hand back "thr-1" for their first send, colliding."""
    step1 = make_sequence_step("s1", 1)
    step2 = make_sequence_step("s2", 2)
    outcome, sender, sent = await _send_step(
        svc, enrollment, 1, step1, sender=sender, all_sequence_steps=[step1, step2], now=now
    )
    assert outcome.sent is True
    return sent


@pytest_asyncio.fixture
async def detection_env(svc):
    mailbox_service = FakeMailboxService()
    reader = FakeGmailThreadReaderClient()
    service = MailReplyDetectionService(sending_service=svc, mailbox_service=mailbox_service, gmail_thread_reader_client=reader)
    return svc, service, mailbox_service, reader


async def test_reply_from_matching_thread_marks_enrollment_replied(detection_env):
    """(1) same-thread reply -> REPLIED."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-reply-1", "lead@example.com", in_reply_to=sent1.rfc_message_id)],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 1
    updated = await svc.enrollment_store.get(enrollment.enrollment_id)
    assert updated.status == MailEnrollmentStatus.REPLIED
    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply.gmail_message_id == "m-reply-1"


async def test_reply_preview_is_captured_from_the_same_metadata_response_used_to_detect_the_reply(detection_env):
    """2026-09-18 -- snippet capture reuses the SAME get_thread() call
    detection already makes; no second Gmail call for the preview."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [
            _message("m-out-1", "mbx-1@astronomic.com"),
            _message(
                "m-reply-1",
                "lead@example.com",
                in_reply_to=sent1.rfc_message_id,
                snippet="got it, thanks. On Thu, Sep 17, 2026 at 12:46 PM &lt;mbx-1@astronomic.com&gt; wrote: Hi there,",
            ),
        ],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 1
    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply.reply_preview == "got it, thanks."
    # Exactly one get_thread() call -- no second Gmail request for the preview.
    assert len(reader.get_thread_calls) == 1


async def test_reply_preview_is_none_when_the_message_has_no_snippet(detection_env):
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-reply-1", "lead@example.com", in_reply_to=sent1.rfc_message_id)],
    )

    await detector.poll_for_replies(NOW + PACING_STEP)

    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply.reply_preview is None


async def test_inbound_from_unrelated_address_is_not_a_reply(detection_env):
    """(4) unrelated inbound ignored."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-cc-1", "someone-else@example.com")],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 0
    updated = await svc.enrollment_store.get(enrollment.enrollment_id)
    assert updated.status == MailEnrollmentStatus.ACTIVE
    assert await svc.reply_store.get(enrollment.enrollment_id) is None


async def test_own_outbound_message_is_never_counted_as_a_reply(detection_env):
    """(5) own outbound ignored -- a thread with ONLY our own message
    (no third party at all) never counts, even though From matches
    nothing on the block list -- it matches the mailbox's own address,
    which is checked first and excluded outright."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(sent1.gmail_thread_id, [_message("m-out-1", "mbx-1@astronomic.com")])

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 0
    assert await svc.reply_store.get(enrollment.enrollment_id) is None


async def test_bounce_or_system_sender_is_not_a_reply(detection_env):
    """(6) bounce/system ignored -- same positive-match mechanism as
    (4): a mailer-daemon message is just another non-matching From, no
    blocklist involved."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-bounce-1", "mailer-daemon@example.com")],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 0
    assert await svc.reply_store.get(enrollment.enrollment_id) is None


async def test_reply_matching_is_normalized_case_and_display_name(detection_env):
    """(12) normalization -- a `Display Name <MIXED@CASE.COM>`-shaped
    header, differing only in case/whitespace/display-name wrapping
    from the enrolled address, still matches."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc, email="Lead@Example.com")
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-reply-1", "  John Doe <LEAD@EXAMPLE.COM>  ")],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 1
    reply = await svc.reply_store.get(enrollment.enrollment_id)
    assert reply.reply_email_normalized == normalize_email("Lead@Example.com")


async def test_polling_twice_is_idempotent(detection_env):
    """(3) duplicate processing idempotent, at the detection-service
    level: a second poll_for_replies() call sees the enrollment is no
    longer a candidate (REPLIED, and a MailReply already exists) and
    detects nothing new."""
    svc, detector, _mailbox_service, reader = detection_env
    enrollment = await _enroll(svc)
    sent1 = await _sent_step1(svc, enrollment)
    reader.threads[sent1.gmail_thread_id] = _thread(
        sent1.gmail_thread_id,
        [_message("m-out-1", "mbx-1@astronomic.com"), _message("m-reply-1", "lead@example.com")],
    )

    first = await detector.poll_for_replies(NOW + PACING_STEP)
    second = await detector.poll_for_replies(NOW + 2 * PACING_STEP)

    assert first == 1
    assert second == 0
    events = await svc.activity_log.store.list()
    replied_events = [e for e in events if e.event_type == "mail_enrollment.replied"]
    assert len(replied_events) == 1


async def test_one_candidates_gmail_read_failure_does_not_abort_the_others(detection_env):
    """Resilience: one candidate's Gmail read error must never prevent
    another candidate in the same poll from being checked."""
    svc, detector, _mailbox_service, reader = detection_env
    broken = await _enroll(svc, enrollment_id="e-broken", email="broken@example.com")
    healthy = await _enroll(svc, enrollment_id="e-healthy", email="healthy@example.com")
    shared_sender = RecordingSender()
    sent_broken = await _sent_step1(svc, broken, sender=shared_sender)
    sent_healthy = await _sent_step1(svc, healthy, now=NOW + PACING_STEP, sender=shared_sender)
    reader.errors[sent_broken.gmail_thread_id] = GmailReadProviderError("Gmail 500")
    reader.threads[sent_healthy.gmail_thread_id] = _thread(
        sent_healthy.gmail_thread_id,
        [_message("m-out", "mbx-1@astronomic.com"), _message("m-reply", "healthy@example.com")],
    )

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 1
    assert (await svc.enrollment_store.get(broken.enrollment_id)).status == MailEnrollmentStatus.ACTIVE
    assert (await svc.enrollment_store.get(healthy.enrollment_id)).status == MailEnrollmentStatus.REPLIED


async def test_candidate_whose_mailbox_needs_reauth_is_skipped_not_fatal(detection_env):
    svc, detector, mailbox_service, reader = detection_env
    mailbox_service.refresh_error = GoogleRefreshTokenInvalidError("invalid_grant")
    enrollment = await _enroll(svc)
    await _sent_step1(svc, enrollment)

    detected = await detector.poll_for_replies(NOW + PACING_STEP)

    assert detected == 0
    assert (await svc.enrollment_store.get(enrollment.enrollment_id)).status == MailEnrollmentStatus.ACTIVE
