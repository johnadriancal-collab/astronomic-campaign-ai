"""
Outbound Gmail threading fix (2026-09-15) -- MailSendingService.
_resolve_reply_threading(), wired into prepare_and_send_step() immediately
before compose_outbound_email(). Closes the confirmed production gap
found by the Step 0 threading proof: `reply_in_thread` was copied from
the sequence step definition all the way into MailSendRequest, but
nothing ever looked up the prior SENT step and populated
thread_id/in_reply_to_message_id/references from it -- so every
"follow-up" silently started a brand-new Gmail conversation instead of
threading.

Fixture conventions mirror tests/test_prepare_and_send_step.py exactly
(mailbox/campaign/allowlist setup, FakePreparingSender-style recording
sender) -- bodies here deliberately carry no {{template}} tokens, so
personalization is never a variable in these tests.
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

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
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_sending_service import (
    MailSendError,
    MailSendingService,
    MailSenderPort,
    MailSendRequest,
    MailThreadingDataMissingError,
    SendBlockReason,
    SendOutcomeCertainty,
    SendResult,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
# DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS is 30s -- every multi-send test
# advances `now` well past that between sends so pacing is never what's
# under test here (see test_mailbox_send_policy.py for pacing itself).
PACING_STEP = timedelta(seconds=60)


class FakeRetryableError(MailSendError):
    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class RecordingSender(MailSenderPort):
    def __init__(self):
        self.prepare_calls: list[MailSendRequest] = []
        self.send_prepared_calls: list[MailSendRequest] = []
        self.send_prepared_error: Exception | None = None

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.prepare_calls.append(request)
        return request

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        self.send_prepared_calls.append(prepared)
        if self.send_prepared_error is not None:
            raise self.send_prepared_error
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
        granted_scopes=["https://www.googleapis.com/auth/gmail.send"], connected_at=NOW, updated_at=NOW,
    )


def make_campaign() -> MailCampaign:
    return MailCampaign(
        mail_campaign_id="c1", name="Threading Test", status=MailCampaignStatus.ACTIVE, timezone=TZ,
        created_at=NOW, updated_at=NOW,
    )


def make_sequence_step(step_id: str, step_number: int, reply_in_thread: bool) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number, subject=f"Subject {step_number}",
        body=f"Body {step_number}.", delay_days=0, reply_in_thread=reply_in_thread, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id="e1", email="lead@example.com") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id="mbx-1",
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
        activity_log=ActivityLogService(MemoryActivityEventStore()),
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
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")


async def _enroll(svc) -> MailEnrollment:
    enrollment = make_enrollment()
    await svc.enrollment_store.create(enrollment)
    return enrollment


async def _send_step(svc, enrollment, step_number, sequence_step, sender=None, all_sequence_steps=None, now=None):
    """Sends one step through the REAL progression, returning
    (outcome, sender, sent_row). `all_sequence_steps` is the FULL campaign
    sequence definition (every MailSequenceStep, not just the one being
    sent) -- record_send_success() uses it to decide whether a next step
    exists (enrollment stays ACTIVE, and that next step's row is
    auto-materialized) or the enrollment is now COMPLETED; passing only
    the current step would make every send look like the last one.

    Step 1's row is created directly (create_step1_execution -- there is
    no prior send to have materialized it). Every later step's row must
    already exist, auto-materialized by the PRIOR step's own send (via
    this same helper, called with the full sequence) -- fetched here
    rather than constructed, so the real "one row, created exactly once,
    by record_send_success()" path is what's actually under test.

    `now` defaults to the module NOW -- callers sending more than one
    step for the same enrollment must advance it by at least PACING_STEP
    each time, or the mailbox pacing check (not threading) blocks the
    second send."""
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


# --- (1) Step 1 sends with no threading headers/thread_id -----------------------


async def test_step1_sends_with_no_threading_fields(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)  # Step 1's own default per MailSequenceStep

    outcome, sender, _ = await _send_step(svc, enrollment, 1, step1)

    assert outcome.sent is True
    req = sender.prepare_calls[0]
    assert req.thread_id is None
    assert req.in_reply_to_message_id is None
    assert req.references == ()


# --- (8) No prior SENT step -> treated as first outbound, new thread allowed ----


async def test_no_prior_sent_step_is_treated_as_first_outbound(svc):
    """Same shape as (1) but stated explicitly per the requested scenario:
    reply_in_thread=True with zero prior SENT rows is a NORMAL case, not
    a failure -- MailThreadingDataMissingError must never fire here."""
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)

    outcome, sender, _ = await _send_step(svc, enrollment, 1, step1)

    assert outcome.sent is True
    assert sender.prepare_calls[0].thread_id is None


# --- (2) Step 2 threads against step 1 -------------------------------------------


async def test_step2_threads_against_step1(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    outcome1, sender1, sent1 = await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])
    assert outcome1.sent is True

    outcome2, sender2, sent2 = await _send_step(
        svc, enrollment, 2, step2, all_sequence_steps=[step1, step2], now=NOW + PACING_STEP
    )

    assert outcome2.sent is True
    req2 = sender2.prepare_calls[0]
    assert req2.thread_id == sent1.gmail_thread_id
    assert req2.in_reply_to_message_id == sent1.rfc_message_id
    assert req2.references == (sent1.rfc_message_id,)


# --- (3) Step 3 threads against the most recent SENT, References has all prior --


async def test_step3_threads_against_most_recent_sent_with_full_references_chain(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    step3 = make_sequence_step("s3", 3, reply_in_thread=True)
    all_steps = [step1, step2, step3]
    _, _, sent1 = await _send_step(svc, enrollment, 1, step1, all_sequence_steps=all_steps)
    _, _, sent2 = await _send_step(svc, enrollment, 2, step2, all_sequence_steps=all_steps, now=NOW + PACING_STEP)

    outcome3, sender3, _ = await _send_step(
        svc, enrollment, 3, step3, all_sequence_steps=all_steps, now=NOW + 2 * PACING_STEP
    )

    assert outcome3.sent is True
    req3 = sender3.prepare_calls[0]
    # thread_id stays the ORIGINAL Gmail thread (step 1's), never step 2's own message id.
    assert req3.thread_id == sent1.gmail_thread_id
    # In-Reply-To points to the MOST RECENT sent step (step 2), not step 1.
    assert req3.in_reply_to_message_id == sent2.rfc_message_id
    # References contains every prior SENT step's rfc_message_id, oldest-first.
    assert req3.references == (sent1.rfc_message_id, sent2.rfc_message_id)


# --- (4) A numbered step was skipped -- uses the most recent SENT, not step_number-1 ---


async def test_uses_most_recent_sent_step_not_blindly_step_number_minus_one(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    # step1's send auto-materializes step2 (a next MailSequenceStep exists) --
    # real behavior, not a shortcut.
    _, _, sent1 = await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])

    # step 2 is then SKIPPED (e.g. a suppression that later cleared, or any
    # future flow that can skip a step without terminating the enrollment)
    # -- it carries no gmail_thread_id/rfc_message_id at all. record_send_
    # success() never runs for it, so step 3 is never auto-materialized --
    # this specific gap-then-jump shape isn't reachable via any flow that
    # exists today, so step 3's row is constructed directly here to prove
    # the ROBUSTNESS of "most recent SENT, not step_number - 1" even in
    # this edge/future state, not to claim it happens today.
    materialized_step2 = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, "s2")
    assert materialized_step2 is not None
    await svc.step_store.try_transition(
        materialized_step2.enrollment_step_id, materialized_step2.status,
        materialized_step2.model_copy(update={"status": MailEnrollmentStepStatus.SKIPPED_SUPPRESSED, "updated_at": NOW}),
    )

    step3 = make_sequence_step("s3", 3, reply_in_thread=True)
    row3 = MailEnrollmentStep(
        enrollment_step_id="step-3", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s3", step_number=3, subject=step3.subject, body=step3.body,
        delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.QUEUED, eligible_at=NOW, next_send_at=NOW,
        created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(row3)
    sender3 = RecordingSender()
    outcome3 = await svc.prepare_and_send_step(
        row3, sender=sender3, claimed_by="w1", sequence_steps=[step3], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + PACING_STEP, confirm_leadership=always_leader,
    )

    assert outcome3.sent is True
    req3 = sender3.prepare_calls[0]
    # Must thread against step 1 (the actual most-recent SENT row), not
    # against the skipped step 2 (which has no identifiers anyway).
    assert req3.thread_id == sent1.gmail_thread_id
    assert req3.in_reply_to_message_id == sent1.rfc_message_id
    assert req3.references == (sent1.rfc_message_id,)


# --- (5) Prior SENT step missing threading identifiers -> fail safe, no provider call ---


async def test_missing_threading_identifiers_on_prior_sent_step_fails_safely(svc):
    enrollment = await _enroll(svc)

    # A SENT row with NO rfc_message_id/gmail_thread_id -- a genuine
    # data-integrity gap that should never occur via the real send path
    # (record_send_success() always persists both together), constructed
    # directly here to prove the fail-safe, not fail-open, behavior.
    broken_sent_step1 = MailEnrollmentStep(
        enrollment_step_id="step-broken-1", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s1", step_number=1, subject="Subject 1", body="Body 1.",
        delay_days=0, reply_in_thread=False, status=MailEnrollmentStepStatus.SENT, sent_at=NOW,
        rfc_message_id=None, gmail_thread_id=None, gmail_message_id=None, created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(broken_sent_step1)

    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    row2 = MailEnrollmentStep(
        enrollment_step_id="step-2", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s2", step_number=2, subject=step2.subject, body=step2.body,
        delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.QUEUED, eligible_at=NOW, next_send_at=NOW,
        created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(row2)
    sender2 = RecordingSender()
    outcome2 = await svc.prepare_and_send_step(
        row2, sender=sender2, claimed_by="w1", sequence_steps=[step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    sent_row2 = await svc.step_store.get(row2.enrollment_step_id)

    assert outcome2.sent is False
    assert outcome2.blocked_reason == SendBlockReason.PREPARE_PERMANENTLY_INVALID
    assert len(sender2.prepare_calls) == 0  # NEVER reached sender.prepare()
    assert len(sender2.send_prepared_calls) == 0
    assert sent_row2.status == MailEnrollmentStepStatus.FAILED
    failed_enrollment = await svc.enrollment_store.get(enrollment.enrollment_id)
    assert failed_enrollment.status == MailEnrollmentStatus.FAILED


async def test_resolve_reply_threading_raises_the_specific_exception_type(svc):
    """Unit-level check on the exception type itself, independent of how
    _handle_prepare_failure() happens to route it."""
    enrollment = await _enroll(svc)
    broken_sent_step1 = MailEnrollmentStep(
        enrollment_step_id="step-broken-1", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s1", step_number=1, subject="Subject 1", body="Body 1.",
        delay_days=0, reply_in_thread=False, status=MailEnrollmentStepStatus.SENT, sent_at=NOW,
        rfc_message_id="rfc-1@test", gmail_thread_id=None, gmail_message_id="gmail-1", created_at=NOW, updated_at=NOW,
    )
    await svc.step_store.create(broken_sent_step1)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    pending_step2 = MailEnrollmentStep(
        enrollment_step_id="step-2", mail_campaign_id="c1", enrollment_id=enrollment.enrollment_id,
        crm_contact_id=enrollment.crm_contact_id, step_id="s2", step_number=2, subject=step2.subject, body=step2.body,
        delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.QUEUED, created_at=NOW, updated_at=NOW,
    )

    with pytest.raises(MailThreadingDataMissingError):
        await svc._resolve_reply_threading(pending_step2)


# --- (6) Retry of the same follow-up -> identical threading metadata ------------


async def test_retry_of_a_follow_up_keeps_threading_metadata_identical(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True)
    _, _, sent1 = await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])
    row2 = await svc.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, "s2")
    assert row2 is not None

    sender = RecordingSender()
    sender.send_prepared_error = FakeRetryableError("rate limited")
    outcome1 = await svc.prepare_and_send_step(
        row2, sender=sender, claimed_by="w1", sequence_steps=[step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + PACING_STEP, confirm_leadership=always_leader,
    )
    assert outcome1.blocked_reason == SendBlockReason.DEFINITELY_NOT_SENT_RETRY
    first_req = sender.prepare_calls[0]
    released_row = await svc.step_store.get(row2.enrollment_step_id)
    assert released_row.status == MailEnrollmentStepStatus.QUEUED

    sender.send_prepared_error = None
    outcome2 = await svc.prepare_and_send_step(
        released_row, sender=sender, claimed_by="w1", sequence_steps=[step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + PACING_STEP, confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    second_req = sender.prepare_calls[1]

    assert second_req.thread_id == first_req.thread_id == sent1.gmail_thread_id
    assert second_req.in_reply_to_message_id == first_req.in_reply_to_message_id == sent1.rfc_message_id
    assert second_req.references == first_req.references == (sent1.rfc_message_id,)
    # Both attempts reached the provider-uncertainty boundary (the first
    # raised there, the second succeeded) -- this asserts the retry
    # actually happened via send_prepared(), not that it was skipped.
    assert len(sender.send_prepared_calls) == 2


# --- (7) reply_in_thread=False intentionally starts a new thread ----------------


async def test_reply_in_thread_false_starts_a_new_thread_even_with_a_prior_sent_step(svc):
    enrollment = await _enroll(svc)
    step1 = make_sequence_step("s1", 1, reply_in_thread=True)
    step2 = make_sequence_step("s2", 2, reply_in_thread=False)
    _, _, sent1 = await _send_step(svc, enrollment, 1, step1, all_sequence_steps=[step1, step2])
    assert sent1.gmail_thread_id is not None  # sanity: a real prior thread exists

    outcome2, sender2, _ = await _send_step(
        svc, enrollment, 2, step2, all_sequence_steps=[step1, step2], now=NOW + PACING_STEP
    )

    assert outcome2.sent is True
    req2 = sender2.prepare_calls[0]
    assert req2.thread_id is None
    assert req2.in_reply_to_message_id is None
    assert req2.references == ()
