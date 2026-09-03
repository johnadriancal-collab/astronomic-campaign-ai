"""
MailSendingService.prepare_and_send_step() -- Phase C's real execution
entry point. Uses in-memory stores throughout (same convention as
tests/test_mail_sending_service.py, which covers process_one_due_step()
-- Phase A, UNCHANGED, and is NOT re-tested here).

FakePreparingSender is a richer test double than tests/
test_mail_sending_service.py's FakeMailSender -- it lets each test inject
a specific prepare()/send_prepared() failure (including a specific
`.certainty`/`.retryable` combination, or a real GoogleRefreshTokenInvalidError)
so the provider-boundary/retry-policy tests can be precise about exactly
which phase fails and why.
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentPauseReason,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
    MailSuppression,
    MailSuppressionReason,
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
    DEFINITELY_NOT_SENT_MAX_ATTEMPTS,
    PREPARE_TRANSIENT_MAX_ATTEMPTS,
    MailSendError,
    MailSendingService,
    MailSenderPort,
    MailSendRequest,
    ProcessOutcome,
    SendBlockReason,
    SendOutcomeCertainty,
    SendResult,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)  # Monday, mid-afternoon UTC


class FakeRetryableError(MailSendError):
    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class FakeNonRetryableError(MailSendError):
    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = False


class FakeUnknownOutcomeError(MailSendError):
    certainty = SendOutcomeCertainty.OUTCOME_UNKNOWN


class FakePreparingSender(MailSenderPort):
    def __init__(self):
        self.prepare_calls: list[MailSendRequest] = []
        self.send_prepared_calls: list[MailSendRequest] = []
        self.prepare_error: Exception | None = None
        self.send_prepared_error: Exception | None = None
        # A hook a test can use to peek at execution-row state DURING
        # send_prepared() -- i.e. after the SENDING CAS has already
        # committed -- to prove the provider boundary is exactly where
        # it's claimed to be.
        self.on_send_prepared: object = None

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.prepare_calls.append(request)
        if self.prepare_error is not None:
            raise self.prepare_error
        return request

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        self.send_prepared_calls.append(prepared)
        if self.on_send_prepared is not None:
            await self.on_send_prepared()
        if self.send_prepared_error is not None:
            raise self.send_prepared_error
        return SendResult(
            provider_message_id=f"msg-{len(self.send_prepared_calls)}",
            provider_thread_id=f"thr-{len(self.send_prepared_calls)}",
            rfc_message_id=prepared.rfc_message_id,
        )


async def always_leader() -> bool:
    return True


async def never_leader() -> bool:
    return False


def make_flaky_leader(true_for_n_calls: int):
    calls = {"n": 0}

    async def confirm() -> bool:
        calls["n"] += 1
        return calls["n"] <= true_for_n_calls

    return confirm


def all_day_windows() -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{d}", mail_campaign_id="c1", day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


def make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED) -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=status, google_user_id=f"g-{mailbox_id}",
        granted_scopes=["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.send"],
        connected_at=NOW, updated_at=NOW,
    )


def make_campaign(status=MailCampaignStatus.ACTIVE) -> MailCampaign:
    return MailCampaign(
        mail_campaign_id="c1", name="Test Campaign", status=status, timezone=TZ, created_at=NOW, updated_at=NOW
    )


def make_step(step_id="s1", step_number=1) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number,
        subject="Subject", body="Body.", delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id="e1", email="lead@example.com", assigned_mailbox_id="mbx-1") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=assigned_mailbox_id,
    )


@pytest.fixture
def campaign_store():
    return MemoryMailCampaignStore()


@pytest.fixture
def enrollment_store():
    return MemoryMailEnrollmentStore()


@pytest.fixture
def step_store():
    return MemoryMailEnrollmentStepStore()


@pytest.fixture
def mailbox_store():
    return MemoryMailboxStore()


@pytest.fixture
def channel_store():
    return MemoryMailCampaignMailboxStore()


@pytest.fixture
def policy_store():
    return MemoryMailboxSendPolicyStore()


@pytest.fixture
def suppression_store():
    return MemoryMailSuppressionStore()


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def svc(campaign_store, enrollment_store, step_store, mailbox_store, channel_store, policy_store, suppression_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=policy_store,
        suppression_store=suppression_store, activity_log=activity_log,
    )


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    """Happy-path default for every test in this file: unsubscribe
    composition succeeds. Tests that specifically need it to FAIL
    override this via monkeypatch themselves (see the composition-
    failure tests below)."""
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _controlled_test_gate_configured(monkeypatch):
    """Happy-path default: mbx-1 / lead@example.com allowlisted. Tests
    that specifically exercise the gate override these themselves."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")


@pytest_asyncio.fixture
async def basic_setup(campaign_store, mailbox_store, channel_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])


async def _due_step(svc, enrollment=None) -> tuple[MailEnrollmentStep, MailEnrollment]:
    enrollment = enrollment or make_enrollment()
    await svc.enrollment_store.create(enrollment)
    step1 = make_step()
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    return row, enrollment


# =====================================================================
# Message-ID durability
# =====================================================================


async def test_message_id_is_persisted_while_still_claimed_before_sending(svc, basic_setup, step_store):
    """Direct proof of the mandatory invariant: read the row's persisted
    rfc_message_id from the STORE (not the in-memory return value) after
    the call completes, and confirm it matches what was actually sent."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is True
    sent_row = await step_store.get(row.enrollment_step_id)
    assert sent_row.rfc_message_id == sender.send_prepared_calls[0].rfc_message_id
    assert sent_row.rfc_message_id is not None


async def test_message_id_survives_a_definitely_not_sent_retry(svc, basic_setup, step_store):
    """Fail once (retryable), then succeed -- the SAME rfc_message_id
    must appear on both attempts."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = FakeRetryableError("rate limited")
    outcome1 = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome1.blocked_reason == SendBlockReason.DEFINITELY_NOT_SENT_RETRY
    first_id = sender.send_prepared_calls[0].rfc_message_id
    released_row = await step_store.get(row.enrollment_step_id)
    assert released_row.status == MailEnrollmentStepStatus.QUEUED
    assert released_row.rfc_message_id == first_id  # preserved through the release

    sender.send_prepared_error = None
    outcome2 = await svc.prepare_and_send_step(
        released_row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + timedelta(minutes=2), confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    assert sender.send_prepared_calls[1].rfc_message_id == first_id  # NOT regenerated


async def test_message_id_survives_a_stale_claimed_reap(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = RuntimeError("simulated crash before persisting")
    # Force a mid-CLAIMED state without completing prepare(): claim manually.
    claimed = row.model_copy(update={"status": MailEnrollmentStepStatus.CLAIMED, "claimed_by": "w1", "claimed_at": NOW})
    await step_store.try_transition(row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED, claimed)
    # Now simulate a FIRST successful attempt that got as far as persisting an ID, then crashed pre-SENDING:
    with_id = claimed.model_copy(update={"rfc_message_id": "seed-id@astronomic.com"})
    await step_store.persist_prepared_fields(row.enrollment_step_id, with_id)

    from app.services.mail_sending_service import CLAIMED_ORPHAN_TIMEOUT_SECONDS

    reaped_at = NOW + timedelta(seconds=CLAIMED_ORPHAN_TIMEOUT_SECONDS + 1)
    await svc.reap_orphans(reaped_at)
    reset_row = await step_store.get(row.enrollment_step_id)
    assert reset_row.status == MailEnrollmentStepStatus.QUEUED
    assert reset_row.rfc_message_id == "seed-id@astronomic.com"  # preserved through the reap

    sender2 = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        reset_row, sender=sender2, claimed_by="w2", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=reaped_at + timedelta(minutes=1), confirm_leadership=always_leader,
    )
    assert outcome.sent is True
    assert sender2.send_prepared_calls[0].rfc_message_id == "seed-id@astronomic.com"  # reused, not regenerated


# =====================================================================
# Provider-boundary proof
# =====================================================================


async def test_row_is_already_sending_when_send_prepared_is_invoked(svc, basic_setup, step_store):
    """Direct proof of the corrected provider boundary: peek at the
    row's PERSISTED status from INSIDE send_prepared() -- it must
    already be SENDING at that exact moment, proving the CAS committed
    strictly before the provider call."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    observed = {}

    async def peek():
        current = await step_store.get(row.enrollment_step_id)
        observed["status"] = current.status

    sender.on_send_prepared = peek
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert observed["status"] == MailEnrollmentStepStatus.SENDING


async def test_prepare_is_called_before_the_sending_transition(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    observed = {}
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        current = await step_store.get(row.enrollment_step_id)
        observed["status_during_prepare"] = current.status
        return await original_prepare(request)

    sender.prepare = wrapped_prepare
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert observed["status_during_prepare"] == MailEnrollmentStepStatus.CLAIMED


# =====================================================================
# OAuth refresh / composition failures cannot manufacture UNKNOWN
# =====================================================================


async def test_transient_oauth_refresh_failure_in_prepare_releases_to_queued_never_sending(svc, basic_setup, step_store, enrollment_store):
    """A GoogleTokenRefreshError (or subclass, other than the invalid_grant
    special case) -- category B of the PREPARE-failure taxonomy: bounded
    retry, NOT an unconditional release, and NOT a pause on the first
    occurrence."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = GoogleTokenRefreshError("token endpoint network error")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.PREPARE_TRANSIENT_RETRY
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    assert result_row.prepare_failure_count == 1
    assert result_row.next_send_at > NOW  # backoff, not immediately due again
    assert len(sender.send_prepared_calls) == 0
    enrollment = await enrollment_store.get(row.enrollment_id)
    assert enrollment.status == MailEnrollmentStatus.ACTIVE  # NOT paused on the first transient failure


async def test_invalid_grant_during_prepare_pauses_enrollment_not_sending(svc, basic_setup, step_store, enrollment_store, activity_log):
    row, enrollment = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = GoogleRefreshTokenInvalidError("invalid_grant")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    paused_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert paused_enrollment.status == MailEnrollmentStatus.PAUSED
    assert paused_enrollment.paused_reason == MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE
    assert paused_enrollment.assigned_mailbox_id == "mbx-1"  # never reassigned
    assert len(sender.send_prepared_calls) == 0


async def test_unsubscribe_composition_configuration_failure_pauses_never_sending(svc, basic_setup, step_store, enrollment_store, monkeypatch):
    """PublicOriginNotConfiguredError -- category C (a known, deterministic
    operator-configuration gap): PAUSED(PREPARE_CONFIG_BLOCKED)
    immediately, no bounded-retry-first (retrying a value we already
    know is absent serves no purpose), and the enrollment is NOT
    permanently failed -- it is only recoverable via
    resume_prepare_config_blocked_enrollments(), and only once the
    prerequisite is actually satisfied again (see the dedicated section
    below)."""
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", None)
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.PREPARE_CONFIG_BLOCKED
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    assert result_row.status != MailEnrollmentStepStatus.UNKNOWN
    assert len(sender.prepare_calls) == 0  # never even reached sender.prepare()
    assert len(sender.send_prepared_calls) == 0
    enrollment = await enrollment_store.get(row.enrollment_id)
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED
    assert enrollment.status != MailEnrollmentStatus.FAILED  # config-absent must never permanently fail the recipient


async def test_transient_prepare_failure_exhausts_bounded_retries_and_blocks(svc, basic_setup, step_store, enrollment_store):
    """Repeated GoogleTokenRefreshError, past PREPARE_TRANSIENT_MAX_ATTEMPTS
    -- must NOT retry forever (no infinite loop) and must NOT permanently
    FAIL the enrollment either; it moves to PAUSED(PREPARE_TRANSIENT_
    EXHAUSTED) -- a state with NO automatic recovery path (see the
    dedicated section below for proof it stays blocked forever without
    an explicit recovery action)."""
    row, enrollment = await _due_step(svc)
    now = NOW
    outcome = None
    for _ in range(PREPARE_TRANSIENT_MAX_ATTEMPTS):
        sender = FakePreparingSender()
        sender.prepare_error = GoogleTokenRefreshError("token endpoint network error")
        current_row = await step_store.get(row.enrollment_step_id)
        outcome = await svc.prepare_and_send_step(
            current_row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
            timezone_name=TZ, now=now, confirm_leadership=always_leader,
        )
        now += timedelta(hours=1)  # jump well past any backoff/window concerns

    assert outcome.blocked_reason == SendBlockReason.PREPARE_TRANSIENT_EXHAUSTED
    final_row = await step_store.get(row.enrollment_step_id)
    assert final_row.status == MailEnrollmentStepStatus.QUEUED
    assert final_row.status != MailEnrollmentStepStatus.FAILED
    assert final_row.prepare_failure_count == PREPARE_TRANSIENT_MAX_ATTEMPTS
    blocked_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert blocked_enrollment.status == MailEnrollmentStatus.PAUSED
    assert blocked_enrollment.paused_reason == MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED
    assert blocked_enrollment.status != MailEnrollmentStatus.FAILED


async def test_permanently_invalid_prepare_failure_fails_step_and_enrollment(svc, basic_setup, step_store, enrollment_store):
    """A ValueError from sender.prepare() (matching HeaderInjectionError's
    own shape -- app/google/gmail_mime.py's ValueError subclass) is
    genuinely permanent: retrying an identical request would fail
    identically, so step and enrollment go straight to FAILED, never a
    bounded retry or a pause."""
    row, enrollment = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = ValueError("malformed header content")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.PREPARE_PERMANENTLY_INVALID
    failed_row = await step_store.get(row.enrollment_step_id)
    assert failed_row.status == MailEnrollmentStepStatus.FAILED
    assert failed_row.status != MailEnrollmentStepStatus.UNKNOWN
    failed_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert failed_enrollment.status == MailEnrollmentStatus.FAILED
    assert len(sender.send_prepared_calls) == 0  # no provider call was ever made


async def test_unclassified_prepare_failure_blocks_on_the_first_occurrence_no_bounded_retry(
    svc, basic_setup, step_store, enrollment_store
):
    """Unlike category B (transient), an unclassified failure does NOT
    get a bounded-retry budget first -- there is no basis to believe a
    fast retry would help, so it goes straight to PAUSED(PREPARE_
    UNCLASSIFIED_BLOCKED) on the very first occurrence. This state has
    NO automatic recovery path -- see tests/test_prepare_blocked_
    recovery.py for proof it is never touched by the periodic sweep."""
    row, enrollment = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = RuntimeError("a completely unrecognized failure")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.PREPARE_UNCLASSIFIED_BLOCKED
    assert len(sender.send_prepared_calls) == 0  # no provider invocation while blocked
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    assert result_row.rfc_message_id is not None  # persisted before prepare() -- survives the block
    blocked_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert blocked_enrollment.status == MailEnrollmentStatus.PAUSED
    assert blocked_enrollment.paused_reason == MailEnrollmentPauseReason.PREPARE_UNCLASSIFIED_BLOCKED


async def test_message_id_survives_an_unclassified_block_and_explicit_recovery(svc, basic_setup, step_store):
    """End to end: block on an unclassified failure, explicitly recover
    (the only path available for this reason), retry, and succeed --
    the SAME Message-ID must appear throughout."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = RuntimeError("a completely unrecognized failure")
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    blocked_row = await step_store.get(row.enrollment_step_id)
    first_id = blocked_row.rfc_message_id
    assert first_id is not None

    resumed_at = NOW + timedelta(minutes=10)
    await svc.resolve_prepare_blocked_step(row.enrollment_step_id, now=resumed_at)
    resumed_row = await step_store.get(row.enrollment_step_id)
    assert resumed_row.prepare_failure_count == 0

    sender2 = FakePreparingSender()
    outcome2 = await svc.prepare_and_send_step(
        resumed_row, sender=sender2, claimed_by="w2", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=resumed_at, confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    assert sender2.send_prepared_calls[0].rfc_message_id == first_id  # NOT regenerated


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: GoogleRefreshTokenInvalidError("invalid_grant"),
        lambda: GoogleTokenRefreshError("network blip"),
        lambda: ValueError("malformed content"),
        lambda: RuntimeError("totally unclassified"),
    ],
)
async def test_no_prepare_failure_ever_produces_unknown(svc, basic_setup, step_store, make_error):
    """Structural proof across the full taxonomy: NOTHING that fails
    inside compose_outbound_email()/sender.prepare() may ever leave a row
    in UNKNOWN -- that status is reachable ONLY via a post-SENDING
    provider-uncertain outcome (see SendOutcomeCertainty.OUTCOME_UNKNOWN's
    own docstring), and none of these paths ever reach SENDING."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = make_error()
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status != MailEnrollmentStepStatus.UNKNOWN
    assert result_row.status != MailEnrollmentStepStatus.SENDING
    assert len(sender.send_prepared_calls) == 0


async def test_message_id_persisted_before_prepare_survives_a_configuration_block(svc, basic_setup, step_store, monkeypatch):
    """The reordering fix: Message-ID is persisted BEFORE composition is
    even attempted, so a category-C configuration failure -- which used
    to happen before persistence ever ran -- no longer causes a later
    retry to generate a fresh, different Message-ID."""
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", None)
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    blocked_row = await step_store.get(row.enrollment_step_id)
    assert blocked_row.rfc_message_id is not None
    first_id = blocked_row.rfc_message_id

    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    resumed_at = NOW + timedelta(minutes=5)
    await svc.resume_prepare_config_blocked_enrollments(resumed_at)
    resumed_row = await step_store.get(row.enrollment_step_id)
    sender2 = FakePreparingSender()
    outcome2 = await svc.prepare_and_send_step(
        resumed_row, sender=sender2, claimed_by="w2", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=resumed_at, confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    assert sender2.send_prepared_calls[0].rfc_message_id == first_id  # NOT regenerated


async def test_message_id_persisted_before_prepare_survives_a_transient_retry(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.prepare_error = GoogleTokenRefreshError("network blip")
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    released_row = await step_store.get(row.enrollment_step_id)
    first_id = released_row.rfc_message_id
    assert first_id is not None

    sender2 = FakePreparingSender()
    outcome2 = await svc.prepare_and_send_step(
        released_row, sender=sender2, claimed_by="w2", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + timedelta(minutes=5), confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    assert sender2.send_prepared_calls[0].rfc_message_id == first_id  # NOT regenerated


# =====================================================================
# Final, fresh mailbox recheck immediately before SENDING
# =====================================================================


async def test_final_recheck_catches_mailbox_disconnected_during_preparation(svc, basic_setup, step_store, mailbox_store, enrollment_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        result = await original_prepare(request)
        mailbox = await mailbox_store.get("mbx-1")
        await mailbox_store.save(mailbox.model_copy(update={"status": MailboxStatus.NEEDS_REAUTH}))
        return result

    sender.prepare = wrapped_prepare
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE
    assert len(sender.send_prepared_calls) == 0
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    assert result_row.rfc_message_id is not None  # persisted before prepare() -- survives
    enrollment = await enrollment_store.get(row.enrollment_id)
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE


async def test_final_recheck_catches_gmail_send_scope_removed_during_preparation(svc, basic_setup, step_store, mailbox_store, enrollment_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        result = await original_prepare(request)
        mailbox = await mailbox_store.get("mbx-1")
        await mailbox_store.save(mailbox.model_copy(update={"granted_scopes": ["openid", "email", "profile"]}))
        return result

    sender.prepare = wrapped_prepare
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE
    assert len(sender.send_prepared_calls) == 0
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.rfc_message_id is not None
    enrollment = await enrollment_store.get(row.enrollment_id)
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE


async def test_final_recheck_catches_mailbox_removed_from_campaign_selection_during_preparation(svc, basic_setup, step_store, channel_store, enrollment_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        result = await original_prepare(request)
        await channel_store.replace_for_campaign("c1", [])
        return result

    sender.prepare = wrapped_prepare
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE
    assert len(sender.send_prepared_calls) == 0
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.rfc_message_id is not None
    enrollment = await enrollment_store.get(row.enrollment_id)
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE


async def test_final_recheck_does_not_trust_the_earlier_prepare_time_mailbox_object(svc, basic_setup, step_store):
    """A mailbox that was perfectly valid when captured earlier in the
    call must never be trusted as still valid merely because prepare()
    succeeded -- see _fresh_mailbox_still_valid_for_sending()'s own
    docstring. This is the negative/control case: nothing changes, so
    the send DOES proceed -- proving the earlier positive tests above
    are catching a real check, not something else blocking the send."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is True


# =====================================================================
# Suppression ordering
# =====================================================================


async def test_early_suppression_check_blocks_before_any_prep_work(svc, basic_setup, suppression_store):
    row, enrollment = await _due_step(svc)
    await suppression_store.upsert(
        MailSuppression(email_normalized="lead@example.com", reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
    )
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.prepare_calls) == 0


async def test_final_suppression_check_is_load_bearing_even_after_prep_succeeded(svc, basic_setup, suppression_store, step_store):
    """Suppression added AFTER the early check but BEFORE the final
    check must still block -- proves the final check is real, not a
    formality."""
    row, enrollment = await _due_step(svc)
    sender = FakePreparingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        await suppression_store.upsert(
            MailSuppression(email_normalized="lead@example.com", reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
        )
        return await original_prepare(request)

    sender.prepare = wrapped_prepare
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.send_prepared_calls) == 0  # prepared, but never sent


# =====================================================================
# Campaign pause during preparation
# =====================================================================


async def test_campaign_paused_during_preparation_blocks_before_sending(svc, basic_setup, campaign_store, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    original_prepare = sender.prepare

    async def wrapped_prepare(request):
        paused = (await campaign_store.get("c1")).model_copy(update={"status": MailCampaignStatus.PAUSED})
        await campaign_store.save(paused)
        return await original_prepare(request)

    sender.prepare = wrapped_prepare
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.CAMPAIGN_NOT_ACTIVE
    assert len(sender.send_prepared_calls) == 0
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED


# =====================================================================
# Controlled-test gate (fail closed)
# =====================================================================


async def test_missing_mailbox_allowlist_sends_nothing(svc, basic_setup, monkeypatch, step_store):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", None)
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED
    assert len(sender.prepare_calls) == 0


async def test_missing_recipient_allowlist_sends_nothing(svc, basic_setup, monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", None)
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED


async def test_wrong_mailbox_not_allowlisted_sends_nothing(svc, basic_setup, monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "some-other-mailbox")
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED


async def test_wrong_recipient_not_allowlisted_sends_nothing(svc, basic_setup, monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "someone-else@example.com")
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED


async def test_engine_enabled_alone_with_no_allowlists_sends_nothing(svc, basic_setup, monkeypatch):
    """mail_sending_engine_enabled is checked at activate_campaign() time,
    not here -- but even simulating "as if" it were on (an ACTIVE
    campaign, which basic_setup already provides), the controlled-test
    gate alone is what actually blocks the send when allowlists are
    unset. This is the literal scenario decision #6 exists to prevent."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", None)
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", None)
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is False
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED
    assert len(sender.prepare_calls) == 0
    assert len(sender.send_prepared_calls) == 0


# =====================================================================
# Leadership
# =====================================================================


async def test_no_leadership_blocks_before_any_claim(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=never_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.NOT_LEADER
    unclaimed_row = await step_store.get(row.enrollment_step_id)
    assert unclaimed_row.status == MailEnrollmentStepStatus.QUEUED  # never even claimed
    assert len(sender.prepare_calls) == 0


async def test_leadership_lost_mid_preparation_blocks_before_sending(svc, basic_setup, step_store):
    """True on the FIRST confirm_leadership() call (so claiming proceeds),
    False on the second (the fresh recheck immediately before SENDING)."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    confirm = make_flaky_leader(true_for_n_calls=1)
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=confirm,
    )
    assert outcome.blocked_reason == SendBlockReason.NOT_LEADER
    assert len(sender.send_prepared_calls) == 0
    result_row = await step_store.get(row.enrollment_step_id)
    assert result_row.status == MailEnrollmentStepStatus.QUEUED
    # Preparation DID happen (leadership was valid at that point) -- only the final, pre-SENDING recheck caught it.
    assert len(sender.prepare_calls) == 1


# =====================================================================
# Retry / certainty policy
# =====================================================================


async def test_definitely_not_sent_retryable_releases_to_queued_with_backoff(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = FakeRetryableError("rate limited")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.DEFINITELY_NOT_SENT_RETRY
    released_row = await step_store.get(row.enrollment_step_id)
    assert released_row.status == MailEnrollmentStepStatus.QUEUED
    assert released_row.next_send_at > NOW  # backoff applied
    assert released_row.attempt_count == 1


async def test_definitely_not_sent_nonretryable_fails_immediately(svc, basic_setup, step_store, enrollment_store):
    row, enrollment = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = FakeNonRetryableError("permission denied")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.blocked_reason == SendBlockReason.PROVIDER_PERMANENTLY_REJECTED
    failed_row = await step_store.get(row.enrollment_step_id)
    assert failed_row.status == MailEnrollmentStepStatus.FAILED
    failed_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert failed_enrollment.status == MailEnrollmentStatus.FAILED


async def test_definitely_not_sent_retryable_exhausted_becomes_failed(svc, basic_setup, step_store, enrollment_store):
    row, enrollment = await _due_step(svc)
    now = NOW
    for attempt in range(DEFINITELY_NOT_SENT_MAX_ATTEMPTS):
        sender = FakePreparingSender()
        sender.send_prepared_error = FakeRetryableError("rate limited")
        current_row = await step_store.get(row.enrollment_step_id)
        outcome = await svc.prepare_and_send_step(
            current_row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
            timezone_name=TZ, now=now, confirm_leadership=always_leader,
        )
        now += timedelta(hours=1)  # jump well past any backoff/window concerns

    assert outcome.blocked_reason == SendBlockReason.PROVIDER_PERMANENTLY_REJECTED
    final_row = await step_store.get(row.enrollment_step_id)
    assert final_row.status == MailEnrollmentStepStatus.FAILED
    assert final_row.attempt_count == DEFINITELY_NOT_SENT_MAX_ATTEMPTS
    failed_enrollment = await enrollment_store.get(enrollment.enrollment_id)
    assert failed_enrollment.status == MailEnrollmentStatus.FAILED


async def test_failed_step_never_materializes_a_next_step(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = FakeNonRetryableError("permission denied")
    step2 = make_step("s2", 2)
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step(), step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    all_rows = await step_store.list_for_enrollment("e1")
    assert len(all_rows) == 1  # no Step 2 row was ever created


async def test_outcome_unknown_leaves_row_in_sending_never_auto_resolved(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = FakeUnknownOutcomeError("5xx")
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is False
    assert outcome.blocked_reason is None  # not a "blocked" outcome -- genuinely uncertain
    stuck_row = await step_store.get(row.enrollment_step_id)
    assert stuck_row.status == MailEnrollmentStepStatus.SENDING


async def test_unclassified_bare_exception_also_treated_as_outcome_unknown(svc, basic_setup, step_store):
    """An exception with no `.certainty` attribute at all must default
    conservatively -- never guessed as DEFINITELY_NOT_SENT."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    sender.send_prepared_error = RuntimeError("totally unclassified failure")
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    stuck_row = await step_store.get(row.enrollment_step_id)
    assert stuck_row.status == MailEnrollmentStepStatus.SENDING


# =====================================================================
# Happy path / unsubscribe wiring end to end
# =====================================================================


async def test_successful_send_carries_composed_body_and_headers(svc, basic_setup):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is True
    sent_request = sender.prepare_calls[0]
    assert "Unsubscribe:" in sent_request.body
    assert sent_request.body.startswith("Body.")  # snapshot body preserved as the prefix
    assert sent_request.list_unsubscribe_header is not None
    assert sent_request.list_unsubscribe_post_header == "List-Unsubscribe=One-Click"


async def test_successful_send_never_mutates_the_persisted_snapshot_body(svc, basic_setup, step_store):
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    sent_row = await step_store.get(row.enrollment_step_id)
    assert sent_row.body == "Body."  # unchanged -- the footer was never persisted onto the row


async def test_successful_send_carries_the_html_alternative_body_too(svc, basic_setup):
    """Companion to test_successful_send_carries_composed_body_and_headers
    (Phase C/D): the same request must also carry the HTML alternative,
    sharing the snapshot content and the same unsubscribe token as the
    plain-text body -- see mail_unsubscribe_composition.compose_outbound_email()."""
    row, _ = await _due_step(svc)
    sender = FakePreparingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[make_step()], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome.sent is True
    sent_request = sender.prepare_calls[0]
    assert sent_request.html_body is not None
    assert "Body." in sent_request.html_body
    assert "Unsubscribe</a>" in sent_request.html_body
    plain_token = sent_request.body.split("token=", 1)[1].strip()
    html_token = sent_request.html_body.split("token=", 1)[1].split('"', 1)[0]
    assert plain_token == html_token
