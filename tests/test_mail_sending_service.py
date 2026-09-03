"""
Tests for MailSendingService -- Phase A's durable execution engine. Uses
in-memory stores throughout, and FakeMailSender (below) as the
test-only MailSenderPort implementation used by every test in this file
-- see that class's docstring for why it deliberately lives here, in
tests/, and not under app/ (GmailSender, app/google/gmail_sender.py, is
the one real implementation, added in Phase B2 -- this fake exists so
MailSendingService's own tests never depend on Gmail-specific code).
"""

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
    MailSuppression,
    MailSuppressionReason,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxSendPolicy, MailboxStatus
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
    DEFAULT_MAILBOX_DAILY_SEND_LIMIT,
    DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS,
    MailSendRequest,
    MailSenderPort,
    MailSendingService,
    NoUsableMailboxError,
    SendBlockReason,
    SendResult,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)  # Monday, mid-afternoon UTC


class FakeMailSender(MailSenderPort):
    """A MailSenderPort test double -- lives in tests/, not under app/, so
    it can never be mistaken for a production-capable sender. Records
    every MailSendRequest it receives (in prepare(), matching where a
    real sender's own preparation work happens -- see GmailSender's
    prepare()/send_prepared() split) so tests can assert on exactly what
    would have been sent, without ever actually sending anything. Echoes
    `request.rfc_message_id` back on the returned SendResult -- never
    invents its own -- matching the real contract every MailSenderPort
    implementation must honor (see MailSendRequest's docstring).

    `fail_at` controls WHICH phase raises when `fail=True` -- "prepare"
    (a pre-SENDING failure, matching GoogleRefreshTokenInvalidError/
    scope-missing/composition-failure cases) or "send_prepared" (a
    post-SENDING, provider-uncertain failure -- the default, matching
    this class's original pre-split behavior exactly: append to
    self.calls, then raise)."""

    def __init__(self, fail: bool = False, fail_at: str = "send_prepared", error: Exception | None = None):
        self.fail = fail
        self.fail_at = fail_at
        self.error = error
        self.calls: list[MailSendRequest] = []

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.calls.append(request)
        if self.fail and self.fail_at == "prepare":
            raise self.error or RuntimeError("FakeMailSender configured to fail in prepare()")
        return request  # trivial passthrough -- the "prepared" object IS the request itself

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        if self.fail and self.fail_at == "send_prepared":
            raise self.error or RuntimeError("FakeMailSender configured to fail")
        return SendResult(
            provider_message_id=f"msg-{len(self.calls)}",
            provider_thread_id=f"thr-{len(self.calls)}",
            rfc_message_id=prepared.rfc_message_id,
        )


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
        # Hardening pass: prepare_and_send_step()'s final safety cluster
        # freshly re-verifies gmail.send is granted -- every mailbox used
        # through the canonical execution path (including via
        # process_one_due_step()'s delegation) needs it, matching
        # tests/test_gmail_sender.py's/test_mail_execution_worker.py's
        # own convention.
        granted_scopes=["https://www.googleapis.com/auth/gmail.send"],
        connected_at=NOW, updated_at=NOW,
    )


def make_campaign(status=MailCampaignStatus.ACTIVE, daily_lead_start_limit=None) -> MailCampaign:
    return MailCampaign(
        mail_campaign_id="c1", name="Test Campaign", status=status, timezone=TZ,
        daily_lead_start_limit=daily_lead_start_limit, created_at=NOW, updated_at=NOW,
    )


def make_step(step_id, step_number, delay_days=0) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number,
        subject=f"Subject {step_number}", body=f"Body {step_number}", delay_days=delay_days,
        reply_in_thread=True, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id, email="lead@example.com", status=MailEnrollmentStatus.ACTIVE) -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=status, enrolled_at=NOW, created_at=NOW,
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


@pytest.fixture(autouse=True)
def _canonical_path_preconditions(monkeypatch):
    """Hardening pass: process_one_due_step() now delegates to
    prepare_and_send_step() (the ONE canonical execution path -- see
    that method's own docstring), so every test in this file that
    expects a send to actually reach the sender now also passes through
    the controlled-test allowlist gate and real unsubscribe composition,
    neither of which this file's tests are about. Configure both here,
    once, for every test:
      - controlled_test_send_allowed() is monkeypatched to always return
        True -- a deliberate, test-only bypass of a cross-cutting Phase C
        policy gate (production's own fail-closed default in
        app/config.py is completely untouched), matching how this
        codebase already isolates other cross-cutting concerns in tests.
        Not configured via matching allowlist VALUES because this file
        exercises many different mailbox_ids/recipient emails across
        ~700 lines -- a single monkeypatch is what actually stays
        maintainable.
      - unsubscribe composition preconditions (encryption key + public
        origin) are set to real, fixed values -- these do NOT vary by
        recipient, so, unlike the allowlist gate, configuring the
        underlying settings (not monkeypatching the function) is both
        simpler and exercises composition for real, matching
        tests/test_mail_execution_worker.py's own established
        convention."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr("app.services.mail_sending_service.controlled_test_send_allowed", lambda *a, **k: True)
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture
def svc(campaign_store, enrollment_store, step_store, mailbox_store, channel_store, policy_store, suppression_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store,
        enrollment_store=enrollment_store,
        step_store=step_store,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        policy_store=policy_store,
        suppression_store=suppression_store,
        activity_log=activity_log,
    )


@pytest_asyncio.fixture
async def basic_setup(campaign_store, mailbox_store, channel_store):
    """One ACTIVE campaign, one CONNECTED+selected mailbox."""
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])


# --- Step 1 materialization -------------------------------------------------


async def test_create_step1_execution_is_idempotent(svc, basic_setup):
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    row_again = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert row.enrollment_step_id == row_again.enrollment_step_id
    assert row.status == MailEnrollmentStepStatus.QUEUED


# --- Completion semantics (Correction 2's exact 3-step example) -------------


async def test_3_step_sequence_step1_sent_does_not_complete_enrollment(svc, basic_setup, step_store, enrollment_store):
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=2), make_step("s3", 3, delay_days=3)]
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)

    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(
        row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    assert outcome.sent

    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 2, "step 2 must be materialized"
    step2_row = next(r for r in rows if r.step_number == 2)
    assert step2_row.status == MailEnrollmentStepStatus.QUEUED

    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.ACTIVE, "must NOT complete after step 1"


async def test_3_step_sequence_step2_sent_materializes_step3(svc, basic_setup, step_store, enrollment_store):
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=2), make_step("s3", 3, delay_days=3)]
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()
    await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)

    step2_row = next(r for r in await step_store.list_for_enrollment("e1") if r.step_number == 2)
    later = NOW + timedelta(days=2)
    outcome2 = await svc.process_one_due_step(
        step2_row, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=later
    )
    assert outcome2.sent
    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 3
    step3_row = next(r for r in rows if r.step_number == 3)
    assert step3_row.status == MailEnrollmentStepStatus.QUEUED
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.ACTIVE


async def test_3_step_sequence_step3_sent_completes_enrollment_with_no_phantom_step4(
    svc, basic_setup, step_store, enrollment_store
):
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=2), make_step("s3", 3, delay_days=3)]
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    sender = FakeMailSender()

    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    step2_row = next(r for r in await step_store.list_for_enrollment("e1") if r.step_number == 2)
    await svc.process_one_due_step(step2_row, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW + timedelta(days=2))
    step3_row = next(r for r in await step_store.list_for_enrollment("e1") if r.step_number == 3)
    outcome3 = await svc.process_one_due_step(step3_row, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW + timedelta(days=5))

    assert outcome3.sent
    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 3, "no step 4 must ever be materialized"
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.COMPLETED


async def test_campaign_remains_active_after_every_enrollment_becomes_terminal(
    svc, basic_setup, campaign_store, enrollment_store, activity_log
):
    """Phase 2 (2026-09-03): a campaign is a PERSISTENT container, not a
    one-time batch -- exhausting an ACTIVE campaign's current workload
    must never auto-transition it anywhere. This is the direct
    replacement for the old maybe_complete_campaign() behavior (removed
    entirely, not left as a silent no-op) -- see MailCampaignStatus's own
    docstring for the current, permanent meaning of ACTIVE vs. the
    now-legacy-only COMPLETED."""
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()
    await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)

    # The enrollment itself still reaches its own terminal state normally --
    # only the campaign-level auto-transition is gone.
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.COMPLETED

    campaign_after = await campaign_store.get("c1")
    assert campaign_after.status == MailCampaignStatus.ACTIVE, "a campaign must stay ACTIVE regardless of remaining workload"

    events = await activity_log.store.list()
    assert not any(e.event_type == "mail_campaign.completed" for e in events), "no code path should emit this event anymore"


# --- Suppression cascade -----------------------------------------------------


async def test_suppressed_recipient_stops_sequence_with_no_future_materialization(
    svc, basic_setup, step_store, enrollment_store, suppression_store
):
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=2)]
    enrollment = make_enrollment("e1", email="suppressed@example.com")
    await enrollment_store.create(enrollment)
    await suppression_store.upsert(
        MailSuppression(email_normalized="suppressed@example.com", reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
    )
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()

    outcome = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.calls) == 0, "suppression must be checked BEFORE the sender boundary"

    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 1, "no step 2 must ever be materialized for a suppressed enrollment"
    assert rows[0].status == MailEnrollmentStepStatus.SKIPPED_SUPPRESSED
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.SUPPRESSED


async def test_unsubscribed_recipient_stops_sequence_same_as_manual_suppression(
    svc, basic_setup, step_store, enrollment_store, suppression_store
):
    """Phase B3 regression guard: the live pre-send suppression check
    (this test's real subject, unmodified by B3) must honor a row whose
    reason is UNSUBSCRIBED exactly the same way it already honors MANUAL
    -- this service has no reason-specific branching, and B3 must not
    have accidentally introduced any. See app/api/mail_unsubscribe.py for
    the (separate, unwired) route that would actually create such a row."""
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=2)]
    enrollment = make_enrollment("e1", email="unsubscribed@example.com")
    await enrollment_store.create(enrollment)
    await suppression_store.upsert(
        MailSuppression(
            email_normalized="unsubscribed@example.com", reason=MailSuppressionReason.UNSUBSCRIBED,
            active=True, created_at=NOW, updated_at=NOW,
        )
    )
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()

    outcome = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.calls) == 0

    rows = await step_store.list_for_enrollment("e1")
    assert rows[0].status == MailEnrollmentStepStatus.SKIPPED_SUPPRESSED
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.SUPPRESSED


async def test_suppress_enrollment_leaves_a_sending_row_untouched(svc, step_store, enrollment_store):
    """A row already past the provider-call-uncertainty boundary is never
    touched by the suppression cascade -- it resolves via reap_orphans()
    on its own schedule."""
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    sending_row = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENDING, created_at=NOW, updated_at=NOW,
    )
    await step_store.create(sending_row)
    await svc.suppress_enrollment(enrollment, NOW)
    persisted = await step_store.get("es1")
    assert persisted.status == MailEnrollmentStepStatus.SENDING


# --- daily_lead_start_limit (Step 1 only, campaign-local day) ---------------


async def test_daily_lead_start_limit_blocks_step1_but_not_followups(svc, basic_setup, step_store, policy_store):
    campaign_store_local = svc.campaign_store
    await campaign_store_local.save((await campaign_store_local.get("c1")).model_copy(update={"daily_lead_start_limit": 1}))
    # Zero out pacing so it can never be the reason e2's send is blocked --
    # this test is specifically about the lead-start limit, not pacing.
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=1)]
    sender = FakeMailSender()

    # First lead's Step 1 send consumes the limit.
    e1 = make_enrollment("e1", email="lead1@example.com")
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    await svc.enrollment_store.create(e1)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    # A second lead's Step 1, same campaign-local day, is blocked.
    e2 = make_enrollment("e2", email="lead2@example.com")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome2.blocked_reason == SendBlockReason.LEAD_START_LIMIT_REACHED
    released = await step_store.get(row2.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED, "must release to QUEUED, never stay CLAIMED"

    # The FIRST lead's own Step 2 (a follow-up) is NOT subject to this limit.
    step2_row = next(r for r in await step_store.list_for_enrollment("e1") if r.step_number == 2)
    outcome_followup = await svc.process_one_due_step(
        step2_row, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW + timedelta(days=1)
    )
    assert outcome_followup.sent, "daily_lead_start_limit must never apply to a follow-up step"


async def test_daily_lead_start_limit_does_not_repeatedly_reclaim_the_same_blocked_row(svc, basic_setup, step_store):
    campaign_store_local = svc.campaign_store
    await campaign_store_local.save((await campaign_store_local.get("c1")).model_copy(update={"daily_lead_start_limit": 0}))
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await svc.enrollment_store.create(enrollment)
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.LEAD_START_LIMIT_REACHED
    released = await step_store.get(row.enrollment_step_id)
    assert released.next_send_at > NOW, "retry must be pushed to the next campaign-local day, not left at now"


# --- Mailbox daily_send_limit (UTC calendar day, ALL steps/campaigns) -------


async def test_mailbox_daily_send_limit_counts_step1_and_followups_together(svc, basic_setup, step_store, policy_store):
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    step1 = make_step("s1", 1)
    step2 = make_step("s2", 2, delay_days=1)
    enrollment = make_enrollment("e1")
    await svc.enrollment_store.create(enrollment)
    sender = FakeMailSender()

    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    # Same UTC day, a DIFFERENT enrollment's Step 1 on the SAME mailbox is blocked
    # by the mailbox's total daily cap, which counts every step across every campaign.
    enrollment2 = make_enrollment("e2")
    await svc.enrollment_store.create(enrollment2)
    row2 = await svc.create_step1_execution(enrollment=enrollment2, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome2.blocked_reason == SendBlockReason.MAILBOX_DAILY_LIMIT_REACHED


async def test_mailbox_daily_limit_uses_utc_boundary_not_campaign_local(svc, basic_setup, step_store, policy_store, campaign_store):
    """Campaign timezone is America/Chicago (UTC-5 in September). A send at
    23:00 UTC (18:00 Chicago) and a second at 02:00 UTC the next calendar
    date (21:00 Chicago, SAME Chicago calendar day) must be treated as TWO
    DIFFERENT UTC days for the mailbox limit -- proving the boundary is
    UTC-fixed, not campaign-local, exactly as MailboxSendPolicy's docstring
    requires."""
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    step1 = make_step("s1", 1)
    sender = FakeMailSender()

    late_utc = datetime(2026, 9, 7, 23, 0, tzinfo=timezone.utc)
    next_utc_day = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)  # still the same Chicago calendar day (21:00 local)

    # Both enrollments/rows created upfront so the campaign never
    # auto-completes after e1's single-step sequence sends (maybe_complete_
    # campaign() would otherwise see e1 as the only, now-terminal,
    # enrollment and flip the campaign to COMPLETED before e2 is ever
    # attempted).
    e1 = make_enrollment("e1")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=late_utc)
    e2 = make_enrollment("e2")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=next_utc_day)

    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=late_utc)
    assert outcome1.sent

    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=next_utc_day)
    assert outcome2.sent, "a new UTC day must reset the mailbox's daily counter even mid-Chicago-day"


# --- Mailbox pacing -----------------------------------------------------------


async def test_mailbox_pacing_prevents_a_burst_send(svc, basic_setup, step_store, policy_store):
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=60, created_at=NOW, updated_at=NOW))
    step1 = make_step("s1", 1)
    sender = FakeMailSender()

    # Both rows created upfront -- see the analogous comment in
    # test_mailbox_daily_limit_uses_utc_boundary_not_campaign_local for why
    # (avoids e1's single-step sequence auto-completing the campaign before
    # e2 is ever attempted).
    e1 = make_enrollment("e1")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    e2 = make_enrollment("e2")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW + timedelta(seconds=5))

    assert (await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)).sent

    outcome2 = await svc.process_one_due_step(
        row2, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW + timedelta(seconds=5)
    )
    assert outcome2.blocked_reason == SendBlockReason.MAILBOX_PACING_NOT_SATISFIED
    released = await step_store.get(row2.enrollment_step_id)
    assert released.next_send_at >= NOW + timedelta(seconds=60)


# --- Missing / null / explicit MailboxSendPolicy resolution -----------------


async def test_missing_policy_row_resolves_to_system_defaults_without_creating_one(svc, mailbox_store, policy_store):
    await mailbox_store.create(make_mailbox("mbx-missing"))
    resolved = await svc.resolve_mailbox_send_policy("mbx-missing")
    assert resolved.daily_send_limit == DEFAULT_MAILBOX_DAILY_SEND_LIMIT
    assert resolved.min_seconds_between_sends == DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS
    assert await policy_store.get("mbx-missing") is None, "resolution must never write a backfill row"


async def test_existing_row_with_null_overrides_resolves_to_defaults(svc, policy_store):
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-null", daily_send_limit=None, min_seconds_between_sends=None, created_at=NOW, updated_at=NOW))
    resolved = await svc.resolve_mailbox_send_policy("mbx-null")
    assert resolved.daily_send_limit == DEFAULT_MAILBOX_DAILY_SEND_LIMIT
    assert resolved.min_seconds_between_sends == DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS


async def test_existing_row_with_explicit_overrides_is_honored(svc, policy_store):
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-explicit", daily_send_limit=5, min_seconds_between_sends=120, created_at=NOW, updated_at=NOW))
    resolved = await svc.resolve_mailbox_send_policy("mbx-explicit")
    assert resolved.daily_send_limit == 5
    assert resolved.min_seconds_between_sends == 120


# --- Mailbox assignment ------------------------------------------------------


async def test_sticky_mailbox_assignment_survives_subsequent_steps(svc, basic_setup, enrollment_store):
    steps = [make_step("s1", 1), make_step("s2", 2, delay_days=1)]
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row1 = await svc.create_step1_execution(enrollment=enrollment, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    sender = FakeMailSender()
    await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)

    after_step1 = await enrollment_store.get("e1")
    assert after_step1.assigned_mailbox_id == "mbx-1"

    # A second call to assign_mailbox_if_needed() must be a total no-op.
    unchanged = await svc.assign_mailbox_if_needed(after_step1)
    assert unchanged.assigned_mailbox_id == "mbx-1"


async def test_assignment_cannot_silently_change_once_set(svc, mailbox_store, channel_store, enrollment_store):
    await mailbox_store.create(make_mailbox("mbx-1"))
    await mailbox_store.create(make_mailbox("mbx-2"))
    await channel_store.replace_for_campaign("c1", ["mbx-1", "mbx-2"])
    enrollment = make_enrollment("e1")
    enrollment = enrollment.model_copy(update={"assigned_mailbox_id": "mbx-1"})
    await enrollment_store.create(enrollment)
    result = await svc.assign_mailbox_if_needed(enrollment)
    assert result.assigned_mailbox_id == "mbx-1", "an already-assigned mailbox is returned completely unchanged"


async def test_no_usable_mailbox_raises(svc, campaign_store, channel_store, enrollment_store):
    await campaign_store.create(make_campaign())
    # No mailboxes selected at all.
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    with pytest.raises(NoUsableMailboxError):
        await svc.assign_mailbox_if_needed(enrollment)


async def test_disconnected_assigned_mailbox_pauses_the_enrollment(svc, basic_setup, mailbox_store, enrollment_store, step_store):
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)

    # Assign, then disconnect the mailbox out from under the enrollment.
    assigned = await svc.assign_mailbox_if_needed(await enrollment_store.get("e1"))
    assert assigned.assigned_mailbox_id == "mbx-1"
    await mailbox_store.save(make_mailbox("mbx-1", status=MailboxStatus.DISCONNECTED))

    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE
    assert len(sender.calls) == 0

    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.PAUSED
    released = await step_store.get(row.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED


# --- Campaign/enrollment not-active guards ----------------------------------


async def test_campaign_not_active_blocks_before_any_claim(svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store):
    await campaign_store.create(make_campaign(status=MailCampaignStatus.PAUSED))
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)
    row = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW, created_at=NOW, updated_at=NOW,
    )
    await step_store.create(row)
    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.CAMPAIGN_NOT_ACTIVE
    unchanged = await step_store.get("es1")
    assert unchanged.status == MailEnrollmentStepStatus.QUEUED, "must never even be claimed"


async def test_enrollment_not_active_blocks_before_any_claim(svc, basic_setup, enrollment_store, step_store):
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1", status=MailEnrollmentStatus.PAUSED)
    await enrollment_store.create(enrollment)
    row = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW, created_at=NOW, updated_at=NOW,
    )
    await step_store.create(row)
    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.ENROLLMENT_NOT_ACTIVE


# --- Runtime window re-check (stale next_send_at is never trusted alone) ---


async def test_stale_next_send_at_is_not_trusted_runtime_rechecks_window(svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await enrollment_store.create(enrollment)

    monday_only = [MailSendWindow(window_id="w0", mail_campaign_id="c1", day_of_week=0, start_time=time(9, 0), end_time=time(17, 0), created_at=NOW, updated_at=NOW)]
    # next_send_at is stale/wrong (claims to be due right now), but "now" is actually a Saturday.
    saturday = NOW + timedelta(days=5)
    row = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.QUEUED, next_send_at=saturday, created_at=NOW, updated_at=NOW,
    )
    await step_store.create(row)
    sender = FakeMailSender()
    outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=monday_only, timezone_name=TZ, now=saturday)
    assert outcome.blocked_reason == SendBlockReason.OUTSIDE_SEND_WINDOW
    assert not outcome.sent
    # sender.prepare() legitimately runs BEFORE the final safety cluster
    # (which includes this fresh window recheck) -- see
    # prepare_and_send_step()'s own docstring on why PREPARE work is not
    # gated behind it. What matters is that no actual send happened and
    # the row is safely back in QUEUED, never SENDING/SENT.
    released = await step_store.get("es1")
    assert released.status == MailEnrollmentStepStatus.QUEUED
    assert released.next_send_at.astimezone().weekday() != 5 or released.next_send_at > saturday


# --- Orphan reaping -----------------------------------------------------------


async def test_reap_orphans_resets_claimed_and_marks_sending_unknown_never_resends(svc, step_store):
    stale_claimed = MailEnrollmentStep(
        enrollment_step_id="es-claimed", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.CLAIMED, claimed_by="dead-worker", claimed_at=NOW - timedelta(seconds=1000),
        created_at=NOW, updated_at=NOW,
    )
    stale_sending = MailEnrollmentStep(
        enrollment_step_id="es-sending", mail_campaign_id="c1", enrollment_id="e2", crm_contact_id="contact-e2",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENDING, claimed_by="dead-worker", claimed_at=NOW - timedelta(seconds=2000),
        created_at=NOW, updated_at=NOW,
    )
    await step_store.create(stale_claimed)
    await step_store.create(stale_sending)

    result = await svc.reap_orphans(NOW, claimed_timeout_seconds=300, sending_timeout_seconds=900)
    assert result.reset_to_queued == 1
    assert result.marked_unknown == 1

    reaped_claimed = await step_store.get("es-claimed")
    reaped_sending = await step_store.get("es-sending")
    assert reaped_claimed.status == MailEnrollmentStepStatus.QUEUED
    assert reaped_claimed.claimed_by is None
    assert reaped_sending.status == MailEnrollmentStepStatus.UNKNOWN, "a SENDING orphan must NEVER auto-resend -- only UNKNOWN"


async def test_reap_orphans_ignores_rows_within_the_timeout(svc, step_store):
    fresh_claimed = MailEnrollmentStep(
        enrollment_step_id="es-fresh", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-e1",
        step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.CLAIMED, claimed_by="w1", claimed_at=NOW - timedelta(seconds=10),
        created_at=NOW, updated_at=NOW,
    )
    await step_store.create(fresh_claimed)
    result = await svc.reap_orphans(NOW, claimed_timeout_seconds=300, sending_timeout_seconds=900)
    assert result.reset_to_queued == 0
    unchanged = await step_store.get("es-fresh")
    assert unchanged.status == MailEnrollmentStepStatus.CLAIMED


# --- Sender failure leaves the row in SENDING, uncertain, never auto-resolved


async def test_sender_exception_leaves_row_in_sending_never_guesses_at_outcome(svc, basic_setup, step_store):
    step1 = make_step("s1", 1)
    enrollment = make_enrollment("e1")
    await svc.enrollment_store.create(enrollment)
    row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    failing_sender = FakeMailSender(fail=True)

    outcome = await svc.process_one_due_step(row, sender=failing_sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert not outcome.sent
    assert outcome.sender_error is not None
    persisted = await step_store.get(row.enrollment_step_id)
    assert persisted.status == MailEnrollmentStepStatus.SENDING, "must stay SENDING -- provider outcome unknown, never guessed at"
