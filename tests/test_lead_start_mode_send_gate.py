"""
Stage 5B (2026-09-04): daily_lead_start_limit's Step-1 send-time
enforcement now applies ONLY while campaign.lead_start_mode == "immediate"
-- see MailSendingService.prepare_and_send_step()'s own comment at the
enforcement site, and MailCampaign.daily_lead_start_limit's docstring.

This file is the dedicated regression suite for that one behavior change,
proving in the same breath that EVERY OTHER send gate (mailbox daily
quota, send windows, pacing, suppression, the controlled-test allowlist)
is completely untouched and still fully applies to a "triggered"-mode
campaign -- Stage 5B changes exactly one condition in one place, nothing
else in the canonical execution path.

Same in-memory-stores/FakeMailSender convention as
tests/test_mail_sending_service.py and tests/test_prepare_and_send_step.py
(module-private helpers, not shared, matching this codebase's existing
per-file convention)."""

from datetime import datetime, time, timedelta, timezone

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
    MailSendingService,
    MailSenderPort,
    MailSendRequest,
    SendBlockReason,
    SendResult,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)  # Monday, mid-afternoon UTC


class FakeMailSender(MailSenderPort):
    def __init__(self):
        self.calls: list[MailSendRequest] = []

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.calls.append(request)
        return request

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        return SendResult(
            provider_message_id=f"msg-{len(self.calls)}", provider_thread_id=f"thr-{len(self.calls)}",
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


def business_hours_window() -> list[MailSendWindow]:
    """Monday 9am-5pm only -- used by the send-window test to prove a
    TRIGGERED campaign is still blocked outside its configured window."""
    return [MailSendWindow(window_id="w-mon", mail_campaign_id="c1", day_of_week=0, start_time=time(9, 0), end_time=time(17, 0), created_at=NOW, updated_at=NOW)]


def make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED) -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=status, google_user_id=f"g-{mailbox_id}",
        granted_scopes=["https://www.googleapis.com/auth/gmail.send"], connected_at=NOW, updated_at=NOW,
    )


def make_campaign(lead_start_mode="immediate", daily_lead_start_limit=None, status=MailCampaignStatus.ACTIVE) -> MailCampaign:
    return MailCampaign(
        mail_campaign_id="c1", name="Test Campaign", status=status, timezone=TZ,
        lead_start_mode=lead_start_mode, daily_lead_start_limit=daily_lead_start_limit,
        created_at=NOW, updated_at=NOW,
    )


def make_step(step_id, step_number, delay_days=0) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number,
        subject=f"Subject {step_number}", body=f"Body {step_number}", delay_days=delay_days,
        reply_in_thread=True, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id, email="lead@example.com") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
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
def _happy_path_gates(monkeypatch):
    """Every test in this file except the dedicated controlled-test-gate
    one (which overrides this itself) exercises lead_start_mode/other
    send gates, not the allowlist or unsubscribe composition -- configure
    both to their happy path once, matching test_prepare_and_send_step.py's
    own convention (real matching allowlist values, not a bypass
    monkeypatch of the function itself)."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead1@example.com,lead2@example.com,lead@example.com")
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr("app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode())


@pytest.fixture
def svc(campaign_store, enrollment_store, step_store, mailbox_store, channel_store, policy_store, suppression_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=policy_store,
        suppression_store=suppression_store, activity_log=activity_log,
    )


@pytest_asyncio.fixture
async def basic_setup(campaign_store, mailbox_store, channel_store):
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])


# --- 1. IMMEDIATE + limit still blocks the (limit+1)th Step-1 send ---------


async def test_immediate_mode_with_limit_still_blocks_the_second_step1_send(svc, basic_setup, campaign_store, step_store, policy_store):
    await campaign_store.create(make_campaign(lead_start_mode="immediate", daily_lead_start_limit=1))
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    e2 = make_enrollment("e2", email="lead2@example.com")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome2.blocked_reason == SendBlockReason.LEAD_START_LIMIT_REACHED
    released = await step_store.get(row2.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED


# --- 2. IMMEDIATE + null remains unlimited ----------------------------------


async def test_immediate_mode_with_null_limit_remains_unlimited(svc, basic_setup, campaign_store, policy_store, monkeypatch):
    await campaign_store.create(make_campaign(lead_start_mode="immediate", daily_lead_start_limit=None))
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    # This test's own 5 distinct recipients -- widen the recipient allowlist
    # beyond this file's shared happy-path fixture value.
    monkeypatch.setattr(
        "app.services.mail_sending_service.settings.mail_sending_recipient_allowlist",
        ",".join(f"lead{i}@example.com" for i in range(5)),
    )
    step1 = make_step("s1", 1)
    sender = FakeMailSender()

    for i in range(5):
        enrollment = make_enrollment(f"e{i}", email=f"lead{i}@example.com")
        await svc.enrollment_store.create(enrollment)
        row = await svc.create_step1_execution(enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW)
        outcome = await svc.process_one_due_step(row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(), timezone_name=TZ, now=NOW)
        assert outcome.sent, f"lead {i} must send -- daily_lead_start_limit is None (unlimited)"


# --- 3. TRIGGERED + stale limit never blocks --------------------------------


async def test_triggered_mode_ignores_a_stale_legacy_limit(svc, basic_setup, campaign_store, policy_store):
    """The core Stage 5B behavior: a campaign that has switched to
    lead_start_mode='triggered' must NEVER hit LEAD_START_LIMIT_REACHED,
    even with a non-null daily_lead_start_limit left over from before the
    switch (deliberately never cleared -- see MailCampaign's docstring)."""
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=1))
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    # A SECOND Step 1 send, same campaign-local day -- would have been
    # blocked under IMMEDIATE mode (limit=1), must succeed under TRIGGERED.
    e2 = make_enrollment("e2", email="lead2@example.com")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome2.sent
    assert outcome2.blocked_reason is None


# --- 4. TRIGGERED still respects mailbox daily send limit -------------------


async def test_triggered_mode_still_respects_mailbox_daily_send_limit(svc, basic_setup, campaign_store, policy_store, step_store):
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=None))
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    e2 = make_enrollment("e2", email="lead2@example.com")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome2.blocked_reason == SendBlockReason.MAILBOX_DAILY_LIMIT_REACHED
    released = await step_store.get(row2.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED


# --- 5. TRIGGERED still respects sending windows ----------------------------


async def test_triggered_mode_still_respects_send_windows(svc, basic_setup, campaign_store, step_store):
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=None))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()
    outside_window = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)  # Monday ~10pm Chicago -- before 9am window

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=business_hours_window(), timezone_name=TZ, now=outside_window)
    outcome = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=business_hours_window(), timezone_name=TZ, now=outside_window)
    assert outcome.blocked_reason == SendBlockReason.OUTSIDE_SEND_WINDOW
    released = await step_store.get(row1.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED


# --- 6. TRIGGERED still respects mailbox pacing -----------------------------


async def test_triggered_mode_still_respects_mailbox_pacing(svc, basic_setup, campaign_store, policy_store, step_store):
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=None))
    await policy_store.upsert(MailboxSendPolicy(mailbox_id="mbx-1", daily_send_limit=1000, min_seconds_between_sends=3600, created_at=NOW, updated_at=NOW))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome1 = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome1.sent

    e2 = make_enrollment("e2", email="lead2@example.com")
    await svc.enrollment_store.create(e2)
    row2 = await svc.create_step1_execution(enrollment=e2, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    # Only 60 seconds later -- well within the 1-hour pacing requirement.
    soon_after = NOW + timedelta(seconds=60)
    outcome2 = await svc.process_one_due_step(row2, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=soon_after)
    assert outcome2.blocked_reason == SendBlockReason.MAILBOX_PACING_NOT_SATISFIED
    released = await step_store.get(row2.enrollment_step_id)
    assert released.status == MailEnrollmentStepStatus.QUEUED


# --- 7. TRIGGERED still respects suppression --------------------------------


async def test_triggered_mode_still_respects_suppression(svc, basic_setup, campaign_store, suppression_store, step_store):
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=None))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    await suppression_store.upsert(
        MailSuppression(email_normalized="lead1@example.com", reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
    )
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.RECIPIENT_SUPPRESSED
    assert len(sender.calls) == 0

    enrollment_after = await svc.enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.SUPPRESSED


# --- 8. TRIGGERED still respects the controlled-test allowlist -------------


async def test_triggered_mode_still_respects_controlled_test_allowlist(svc, basic_setup, campaign_store, step_store, monkeypatch):
    """Overrides this file's own happy-path allowlist fixture back to the
    real fail-closed default (both settings unset) to prove the gate isn't
    accidentally bypassed for a 'triggered' campaign."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", None)
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", None)
    await campaign_store.create(make_campaign(lead_start_mode="triggered", daily_lead_start_limit=None))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()

    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    outcome = await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    assert outcome.blocked_reason == SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED
    assert len(sender.calls) == 0


# --- 9. No Trigger execution has been introduced ----------------------------


def test_stage_5b_adds_no_trigger_execution_logic():
    """Structural guard: the ONLY new lead_start_mode-related logic in the
    canonical send path is the one gating condition Stage 5B approved --
    nothing resembling occurrence/member reconciliation exists here."""
    from pathlib import Path

    source = Path("app/services/mail_sending_service.py").read_text()
    assert source.count('lead_start_mode == "immediate"') == 1
    # Explanatory comments may legitimately NAME the future Trigger models
    # (see the comment right above the gate) -- what must never appear is
    # an actual CALL/instantiation, i.e. real reconciliation logic.
    for forbidden in ("freeze_members(", "MailTriggerOccurrence(", "create_occurrence("):
        assert forbidden not in source


def test_mail_execution_worker_still_has_no_trigger_code():
    from pathlib import Path

    source = Path("app/services/mail_execution_worker.py").read_text()
    assert "trigger" not in source.lower()


# --- 12. No existing campaign is automatically switched to TRIGGERED -------


async def test_creating_and_sending_through_a_campaign_never_changes_its_lead_start_mode(svc, basic_setup, campaign_store):
    await campaign_store.create(make_campaign(lead_start_mode="immediate", daily_lead_start_limit=None))
    steps = [make_step("s1", 1)]
    sender = FakeMailSender()
    e1 = make_enrollment("e1", email="lead1@example.com")
    await svc.enrollment_store.create(e1)
    row1 = await svc.create_step1_execution(enrollment=e1, step1=steps[0], windows=all_day_windows(), timezone_name=TZ, now=NOW)
    await svc.process_one_due_step(row1, sender=sender, claimed_by="w1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)

    campaign_after = await campaign_store.get("c1")
    assert campaign_after.lead_start_mode == "immediate"
