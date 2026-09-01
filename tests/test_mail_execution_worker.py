"""
MailExecutionWorker -- tick()/recovery/liveness. Every test drives tick()
directly (never the real asyncio sleep loop), matching this codebase's
established "the scheduling wrapper is thin and mostly untested; the real
logic is a directly-callable method" convention.
"""

import asyncio

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
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.repositories.worker_lease_store import MemoryWorkerLeaseStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_execution_worker import MailExecutionWorker
from app.services.mail_sending_service import MailSenderPort, MailSendingService, SendResult
from app.services.worker_lease_service import WorkerLeaseService

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


class RecordingSender(MailSenderPort):
    def __init__(self):
        self.prepare_calls = []
        self.send_prepared_calls = []

    async def prepare(self, request):
        self.prepare_calls.append(request)
        return request

    async def send_prepared(self, prepared):
        self.send_prepared_calls.append(prepared)
        return SendResult(
            provider_message_id=f"msg-{len(self.send_prepared_calls)}",
            provider_thread_id=f"thr-{len(self.send_prepared_calls)}",
            rfc_message_id=prepared.rfc_message_id,
        )


@pytest.fixture(autouse=True)
def _unsubscribe_and_allowlist_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")


def all_day_windows(campaign_id="c1") -> list[MailSendWindow]:
    return [
        MailSendWindow(window_id=f"w-{d}", mail_campaign_id=campaign_id, day_of_week=d, start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW)
        for d in range(7)
    ]


@pytest_asyncio.fixture
async def env():
    campaign_store = MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailEnrollmentStepStore()
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    window_store = MemoryMailSendWindowStore()
    sequence_step_store = MemoryMailSequenceStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())

    mail_sending_service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(), activity_log=activity_log,
    )
    mail_campaign_service = MailCampaignService(
        campaign_store=campaign_store, step_store=sequence_step_store, enrollment_store=enrollment_store,
        crm_service=CrmService(), activity_log=activity_log, mailbox_store=mailbox_store, channel_store=channel_store,
        window_store=window_store, enrollment_step_store=step_store, sending_service=mail_sending_service,
    )

    await campaign_store.create(
        MailCampaign(mail_campaign_id="c1", name="Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await mailbox_store.create(
        Mailbox(mailbox_id="mbx-1", provider=MailboxProvider.GOOGLE, email="mbx-1@astronomic.com", display_name=None,
                status=MailboxStatus.CONNECTED, google_user_id="g-1",
                granted_scopes=["https://www.googleapis.com/auth/gmail.send"], connected_at=NOW, updated_at=NOW)
    )
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    step1 = MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="Subj", body="Body.", delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW)
    await sequence_step_store.create(step1)
    await window_store.replace_for_campaign("c1", all_day_windows())

    return {
        "campaign_store": campaign_store, "enrollment_store": enrollment_store, "step_store": step_store,
        "mailbox_store": mailbox_store, "channel_store": channel_store, "mail_sending_service": mail_sending_service,
        "mail_campaign_service": mail_campaign_service, "step1": step1, "activity_log": activity_log,
    }


async def make_worker(env, holder_id="worker-A", lease_store=None, activity_log=None):
    sender = RecordingSender()
    lease_service = WorkerLeaseService(lease_store or MemoryWorkerLeaseStore(), holder_id=holder_id)
    worker = MailExecutionWorker(
        mail_sending_service=env["mail_sending_service"], mail_campaign_service=env["mail_campaign_service"],
        lease_service=lease_service, sender=sender, lease_duration_seconds=90, poll_interval_seconds=45,
        activity_log=activity_log,
    )
    return worker, sender


async def enroll_and_queue(env, enrollment_id="e1", email="lead@example.com"):
    enrollment = MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )
    await env["enrollment_store"].create(enrollment)
    row = await env["mail_sending_service"].create_step1_execution(
        enrollment=enrollment, step1=env["step1"], windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    return enrollment, row


# --- tick(): leadership -------------------------------------------------------


async def test_tick_acquires_leadership_and_processes_nothing_when_no_due_rows(env):
    worker, sender = await make_worker(env)
    result = await worker.tick()
    assert result.is_leader is True
    assert result.due_rows_seen == 0


async def test_second_worker_cannot_process_while_first_holds_the_lease(env):
    lease_store = MemoryWorkerLeaseStore()
    worker_a, sender_a = await make_worker(env, holder_id="A", lease_store=lease_store)
    worker_b, sender_b = await make_worker(env, holder_id="B", lease_store=lease_store)
    await enroll_and_queue(env)

    result_a = await worker_a.tick()
    result_b = await worker_b.tick()

    assert result_a.is_leader is True
    assert result_b.is_leader is False
    assert result_b.due_rows_seen == 0
    assert len(sender_b.prepare_calls) == 0


async def test_worker_processes_a_due_row_and_sends(env):
    worker, sender = await make_worker(env)
    await enroll_and_queue(env)
    result = await worker.tick(NOW)
    assert result.is_leader is True
    assert result.due_rows_seen == 1
    assert result.sent == 1
    assert len(sender.send_prepared_calls) == 1


async def test_worker_processes_a_bounded_batch(env):
    worker, sender = await make_worker(env)
    worker.batch_size = 2
    await enroll_and_queue(env, "e1", "lead@example.com")
    # Only e1 is allowlisted -- e2/e3 will be blocked by the controlled-test
    # gate, but still COUNT as "seen" due rows for batching purposes.
    await enroll_and_queue(env, "e2", "someone-else@example.com")
    await enroll_and_queue(env, "e3", "another@example.com")
    result = await worker.tick(NOW)
    assert result.due_rows_seen == 2  # capped at batch_size


# --- Recovery -----------------------------------------------------------------


async def test_startup_recovery_resets_stale_claimed_rows(env):
    from app.models.mail import MailEnrollmentStepStatus

    _, row = await enroll_and_queue(env)
    step_store = env["step_store"]
    stale_claimed = row.model_copy(update={"status": MailEnrollmentStepStatus.CLAIMED, "claimed_by": "ghost", "claimed_at": NOW - timedelta(hours=1)})
    await step_store.try_transition(row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED, stale_claimed)

    worker, _ = await make_worker(env)
    await worker._run_recovery(NOW)

    recovered = await step_store.get(row.enrollment_step_id)
    assert recovered.status == MailEnrollmentStepStatus.QUEUED
    assert recovered.claimed_by is None


async def test_recovery_resumes_mailbox_paused_enrollments(env):
    from app.models.mail import MailEnrollmentPauseReason

    enrollment, _ = await enroll_and_queue(env)
    paused = enrollment.model_copy(update={"status": MailEnrollmentStatus.PAUSED, "paused_reason": MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE})
    await env["enrollment_store"].save(paused)

    worker, _ = await make_worker(env)
    await worker._run_recovery(NOW)

    resumed = await env["enrollment_store"].get(enrollment.enrollment_id)
    assert resumed.status == MailEnrollmentStatus.ACTIVE


async def test_tick_runs_recovery_only_once_per_recovery_interval(env):
    worker, _ = await make_worker(env)
    worker.recovery_interval_seconds = 300
    await worker.tick()
    first_recovery_at = worker._last_recovery_at
    await worker.tick()  # immediately again -- should NOT re-run recovery
    assert worker._last_recovery_at == first_recovery_at


# --- Lifecycle / liveness ------------------------------------------------------


async def test_start_is_a_noop_when_engine_disabled(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", False)
    worker, _ = await make_worker(env)
    worker.start()
    assert worker._task is None
    snapshot = worker.liveness_snapshot(NOW)
    assert snapshot["state"] == "disabled"
    assert snapshot["engine_enabled"] is False


async def test_start_launches_the_task_when_engine_enabled(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    worker, _ = await make_worker(env)
    worker.start()
    try:
        assert worker._task is not None
    finally:
        await worker.stop()


async def test_start_twice_does_not_create_a_second_task(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    worker, _ = await make_worker(env)
    worker.start()
    first_task = worker._task
    worker.start()
    try:
        assert worker._task is first_task
    finally:
        await worker.stop()


async def test_stop_releases_the_lease(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    lease_store = MemoryWorkerLeaseStore()
    worker, _ = await make_worker(env, lease_store=lease_store)
    worker.start()
    await worker.tick()  # acquire leadership for real
    await worker.stop()
    assert await lease_store.get("mail_execution_worker") is None


async def test_liveness_snapshot_leader_vs_non_leader(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    lease_store = MemoryWorkerLeaseStore()
    worker_a, _ = await make_worker(env, holder_id="A", lease_store=lease_store)
    worker_b, _ = await make_worker(env, holder_id="B", lease_store=lease_store)
    await worker_a.tick()
    await worker_b.tick()

    snapshot_a = worker_a.liveness_snapshot(NOW)
    snapshot_b = worker_b.liveness_snapshot(NOW)
    # Both are structurally "dead" per liveness_snapshot() since neither
    # ._task was ever started via start() in this direct-tick test --
    # liveness_snapshot() reports on the TASK, not on tick() calls made
    # directly. Confirm that explicitly rather than assume it.
    assert snapshot_a["state"] == "dead"
    assert snapshot_b["state"] == "dead"


async def test_liveness_snapshot_dead_before_start(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    worker, _ = await make_worker(env)
    snapshot = worker.liveness_snapshot(NOW)
    assert snapshot["state"] == "dead"


# --- Structural activity events: worker lifecycle / leadership (Phase C) --------


async def test_tick_logs_leadership_acquired_on_first_acquisition(env):
    worker, _ = await make_worker(env, activity_log=env["activity_log"])

    await worker.tick(NOW)

    events = await env["activity_log"].store.list()
    matching = [e for e in events if e.event_type == "mail_worker.leadership_acquired"]
    assert len(matching) == 1


async def test_tick_does_not_relog_leadership_acquired_on_renewal(env):
    """Acquiring is a TRANSITION, not a per-tick heartbeat -- a worker
    that already holds the lease and simply renews it must not produce a
    fresh event on every single tick."""
    worker, _ = await make_worker(env, activity_log=env["activity_log"])

    await worker.tick(NOW)
    await worker.tick(NOW + timedelta(seconds=10))

    events = await env["activity_log"].store.list()
    matching = [e for e in events if e.event_type == "mail_worker.leadership_acquired"]
    assert len(matching) == 1


async def test_second_worker_losing_the_race_never_logs_leadership_acquired(env):
    lease_store = MemoryWorkerLeaseStore()
    worker_a, _ = await make_worker(env, holder_id="A", lease_store=lease_store, activity_log=env["activity_log"])
    worker_b, _ = await make_worker(env, holder_id="B", lease_store=lease_store, activity_log=env["activity_log"])

    await worker_a.tick(NOW)
    await worker_b.tick(NOW)

    events = await env["activity_log"].store.list()
    acquired = [e for e in events if e.event_type == "mail_worker.leadership_acquired"]
    assert len(acquired) == 1
    assert acquired[0].entity_id == "A"


async def test_tick_logs_leadership_lost_after_a_takeover(env):
    lease_store = MemoryWorkerLeaseStore()
    worker_a, _ = await make_worker(env, holder_id="A", lease_store=lease_store, activity_log=env["activity_log"])
    worker_b, _ = await make_worker(env, holder_id="B", lease_store=lease_store, activity_log=env["activity_log"])

    await worker_a.tick(NOW)  # A becomes leader
    later = NOW + timedelta(seconds=200)  # well past the 90s lease duration
    await worker_b.tick(later)  # B takes over
    await worker_a.tick(later)  # A discovers it lost leadership

    events = await env["activity_log"].store.list()
    lost = [e for e in events if e.event_type == "mail_worker.leadership_lost"]
    assert len(lost) == 1
    assert lost[0].entity_id == "A"


async def test_worker_without_activity_log_still_ticks_normally(env):
    """activity_log stays fully optional -- every existing call site
    (make_worker's default, and every test above this section) must keep
    working exactly as before."""
    worker, _ = await make_worker(env)
    assert worker.activity_log is None

    result = await worker.tick(NOW)
    assert result.is_leader is True


async def test_start_and_stop_log_started_and_stopped_activity_events(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    worker, _ = await make_worker(env, activity_log=env["activity_log"])

    worker.start()
    await asyncio.sleep(0)  # let the background task reach its first await point
    await worker.stop()

    events = await env["activity_log"].store.list()
    assert any(e.event_type == "mail_worker.started" for e in events)
    assert any(e.event_type == "mail_worker.stopped" for e in events)


async def test_start_without_activity_log_does_not_raise(env, monkeypatch):
    monkeypatch.setattr("app.services.mail_execution_worker.settings.mail_sending_engine_enabled", True)
    worker, _ = await make_worker(env)

    worker.start()
    await asyncio.sleep(0)
    await worker.stop()  # must not raise
