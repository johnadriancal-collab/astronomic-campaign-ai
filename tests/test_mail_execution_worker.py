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
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MemoryMailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.repositories.worker_lease_store import MemoryWorkerLeaseStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_execution_worker import MailExecutionWorker
from app.services.mail_reply_detection_service import MailReplyDetectionService
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
        batch_store=MemoryMailEnrollmentBatchStore(),
        batch_member_store=MemoryMailEnrollmentBatchMemberStore(),
        suppression_store=MemoryMailSuppressionStore(),
        crm_import_reader=CrmImportService(crm_service=CrmService(), batch_store=MemoryCrmImportBatchStore()),
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


async def make_worker(
    env, holder_id="worker-A", lease_store=None, activity_log=None,
    mail_reply_detection_service=None, reply_poll_interval_seconds=None,
    mail_bounce_detection_service=None, bounce_poll_interval_seconds=None,
):
    sender = RecordingSender()
    lease_service = WorkerLeaseService(lease_store or MemoryWorkerLeaseStore(), holder_id=holder_id)
    kwargs = {}
    if reply_poll_interval_seconds is not None:
        kwargs["reply_poll_interval_seconds"] = reply_poll_interval_seconds
    if bounce_poll_interval_seconds is not None:
        kwargs["bounce_poll_interval_seconds"] = bounce_poll_interval_seconds
    worker = MailExecutionWorker(
        mail_sending_service=env["mail_sending_service"], mail_campaign_service=env["mail_campaign_service"],
        lease_service=lease_service, sender=sender, lease_duration_seconds=90, poll_interval_seconds=45,
        activity_log=activity_log, mail_reply_detection_service=mail_reply_detection_service,
        mail_bounce_detection_service=mail_bounce_detection_service, **kwargs,
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


# --- Reply-poll wiring (2026-09-15) ---------------------------------------------
#
# tick()'s own cadence gate for the reply poll, plus the resilience/restart
# guarantees around it. The MATCHING/eligibility logic itself belongs to
# MailReplyDetectionService and MailSendingService (see
# tests/test_mail_reply_detection.py) -- these tests only cover tick()'s own
# responsibility: calling poll_for_replies() on the right cadence, never
# letting its failure abort due-step processing in the same tick, and
# proving nothing about that correctness depends on any in-memory worker
# state (a "restart" -- a brand-new MailExecutionWorker instance -- behaves
# identically, since every fact it needs lives in the stores).


class _CountingReplyDetectionService:
    """A thin stand-in for MailReplyDetectionService -- records how many
    times poll_for_replies() was actually invoked (to prove tick()'s own
    cadence gate, independent of that method's real matching logic,
    which is covered elsewhere) and can be made to raise on demand."""

    def __init__(self, raise_error: Exception | None = None):
        self.calls: list[datetime] = []
        self.raise_error = raise_error

    async def poll_for_replies(self, now: datetime) -> int:
        self.calls.append(now)
        if self.raise_error is not None:
            raise self.raise_error
        return 0


async def test_tick_polls_for_replies_on_its_own_cadence(env):
    detector = _CountingReplyDetectionService()
    worker, _ = await make_worker(env, mail_reply_detection_service=detector, reply_poll_interval_seconds=300)

    await worker.tick(NOW)
    assert len(detector.calls) == 1  # runs immediately on the first tick, like the recovery sweep

    await worker.tick(NOW + timedelta(seconds=30))  # well inside the 300s cadence
    assert len(detector.calls) == 1  # NOT re-run yet

    await worker.tick(NOW + timedelta(seconds=301))
    assert len(detector.calls) == 2  # cadence elapsed -- runs again


async def test_tick_never_polls_for_replies_when_no_detection_service_configured(env):
    worker, _ = await make_worker(env)  # mail_reply_detection_service defaults to None
    result = await worker.tick(NOW)
    assert result.is_leader is True  # tick() itself must not raise/short-circuit


async def test_reply_poll_failure_does_not_abort_due_step_processing_in_the_same_tick(env):
    """A poll_for_replies() failure must never prevent the rest of THIS
    tick from reaching and attempting its ordinary due rows -- same
    isolation discipline as Trigger-processing (see tick()'s own
    docstring). Asserts due_rows_seen (tick reached and attempted the
    row), not a completed send -- whether prepare_and_send_step()
    itself succeeds depends on real wall-clock leadership state
    unrelated to this test (see
    test_worker_processes_a_due_row_and_sends's own known flakiness),
    which is not what this test is about."""
    detector = _CountingReplyDetectionService(raise_error=RuntimeError("Gmail is down"))
    worker, sender = await make_worker(env, mail_reply_detection_service=detector)
    await enroll_and_queue(env)

    result = await worker.tick(NOW)

    assert len(detector.calls) == 1  # the poll really was attempted
    assert result.is_leader is True
    assert result.due_rows_seen == 1  # the reply-poll failure never short-circuited due-row processing


class _CountingBounceDetectionService:
    """Same stand-in shape as _CountingReplyDetectionService above, for
    MailBounceDetectionService.poll_all_mailboxes() -- proves tick()'s
    own bounce-poll cadence gate and isolation, independent of the real
    DSN-detection logic (covered in test_mail_bounce_detection_service.py)."""

    def __init__(self, raise_error: Exception | None = None):
        self.calls: list[datetime] = []
        self.raise_error = raise_error

    async def poll_all_mailboxes(self, now: datetime) -> int:
        self.calls.append(now)
        if self.raise_error is not None:
            raise self.raise_error
        return 0


async def test_tick_polls_for_bounces_on_its_own_cadence(env):
    detector = _CountingBounceDetectionService()
    worker, _ = await make_worker(env, mail_bounce_detection_service=detector, bounce_poll_interval_seconds=300)

    await worker.tick(NOW)
    assert len(detector.calls) == 1  # runs immediately on the first tick, like the recovery sweep

    await worker.tick(NOW + timedelta(seconds=30))  # well inside the 300s cadence
    assert len(detector.calls) == 1  # NOT re-run yet

    await worker.tick(NOW + timedelta(seconds=301))
    assert len(detector.calls) == 2  # cadence elapsed -- runs again


async def test_tick_never_polls_for_bounces_when_no_detection_service_configured(env):
    worker, _ = await make_worker(env)  # mail_bounce_detection_service defaults to None
    result = await worker.tick(NOW)
    assert result.is_leader is True  # tick() itself must not raise/short-circuit


async def test_bounce_poll_failure_does_not_abort_due_step_processing_in_the_same_tick(env):
    """A poll_all_mailboxes() failure must never block outbound sending
    in the SAME tick -- the user's own explicit requirement (spec
    section 15) -- same isolation discipline as Trigger-processing and
    reply-poll above."""
    detector = _CountingBounceDetectionService(raise_error=RuntimeError("Gmail is down"))
    worker, sender = await make_worker(env, mail_bounce_detection_service=detector)
    await enroll_and_queue(env)

    result = await worker.tick(NOW)

    assert len(detector.calls) == 1  # the poll really was attempted
    assert result.is_leader is True
    assert result.due_rows_seen == 1  # the bounce-poll failure never short-circuited due-row processing


async def test_reply_poll_and_bounce_poll_run_independently_in_the_same_tick(env):
    """Both optional services, each on its own cadence, must both run
    inside a single tick without interfering with one another."""
    reply_detector = _CountingReplyDetectionService()
    bounce_detector = _CountingBounceDetectionService()
    worker, _ = await make_worker(env, mail_reply_detection_service=reply_detector, mail_bounce_detection_service=bounce_detector)

    await worker.tick(NOW)

    assert len(reply_detector.calls) == 1
    assert len(bounce_detector.calls) == 1


class _FakeMailboxService:
    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        return "tok-1"


class _FakeGmailThreadReaderClient:
    def __init__(self):
        self.threads: dict[str, dict] = {}

    async def get_thread(self, *, access_token: str, thread_id: str) -> dict:
        return self.threads.get(thread_id, {"id": thread_id, "messages": []})


async def test_reply_poll_survives_worker_restart(env, monkeypatch):
    """(10) worker restart -- a reply detected by one worker instance
    stays detected for a brand-new instance (simulating a process
    restart): nothing about correctness lives in _last_reply_poll_at or
    any other in-memory worker attribute, only in the stores."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")

    mail_sending_service = env["mail_sending_service"]
    # A second MailSequenceStep so the enrollment stays ACTIVE (a next
    # step materializes) rather than COMPLETED once Step 1 sends --
    # required for it to remain a real reply-poll candidate at all.
    step2 = MailSequenceStep(
        step_id="s2", mail_campaign_id="c1", step_number=2, subject="Subj 2", body="Body 2.",
        delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW,
    )
    await env["mail_campaign_service"].step_store.create(step2)

    async def _always_leader() -> bool:
        return True

    enrollment, row1 = await enroll_and_queue(env)
    sender_a = RecordingSender()
    outcome = await mail_sending_service.prepare_and_send_step(
        row1, sender=sender_a, claimed_by="worker-A", sequence_steps=[env["step1"], step2],
        windows=all_day_windows(), timezone_name=TZ, now=NOW, confirm_leadership=_always_leader,
    )
    assert outcome.sent is True
    sent1 = await env["step_store"].get(row1.enrollment_step_id)

    reader = _FakeGmailThreadReaderClient()
    reader.threads[sent1.gmail_thread_id] = {
        "id": sent1.gmail_thread_id,
        "messages": [
            {"id": "m-out", "payload": {"headers": [{"name": "From", "value": "mbx-1@astronomic.com"}]}},
            {"id": "m-reply", "payload": {"headers": [{"name": "From", "value": "lead@example.com"}]}},
        ],
    }
    detector = MailReplyDetectionService(
        sending_service=mail_sending_service, mailbox_service=_FakeMailboxService(), gmail_thread_reader_client=reader
    )

    # First worker instance polls and detects the reply.
    worker_a2, _ = await make_worker(env, mail_reply_detection_service=detector)
    await worker_a2.tick(NOW + timedelta(seconds=1))
    assert (await env["enrollment_store"].get(enrollment.enrollment_id)).status == MailEnrollmentStatus.REPLIED

    # A brand-new MailExecutionWorker instance -- simulating a full
    # process restart, with its OWN fresh _last_reply_poll_at=None --
    # must see the SAME already-terminal state via the stores, and must
    # never re-detect/re-emit for it (list_reply_poll_candidates()
    # excludes REPLIED enrollments outright).
    worker_b, _ = await make_worker(env, mail_reply_detection_service=detector, holder_id="worker-B")
    assert worker_b._last_reply_poll_at is None
    detected_again = await detector.poll_for_replies(NOW + timedelta(seconds=2))
    assert detected_again == 0
