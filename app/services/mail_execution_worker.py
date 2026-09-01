"""
MailExecutionWorker -- Astronomic Mail Phase C. The in-process asyncio
background task that actually drives MailSendingService.
prepare_and_send_step() on a schedule. Runs in the SAME process as the
web server (see the Phase C design report's worker-topology section for
why: Railway's Dockerfile CMD is a single `uvicorn` process, there is no
existing multi-process/supervisor infrastructure, and an in-process task
reuses every store/service app/main.py's lifespan() already constructs,
with zero duplicate wiring).

STILL UNABLE TO SEND IN THIS ENVIRONMENT:
  1. `start()` refuses to even begin polling unless
     settings.mail_sending_engine_enabled is True (unset in this repo,
     defaults False) -- the worker's own first, structural gate.
  2. Even if started, no campaign can ever reach ACTIVE without that same
     flag (MailCampaignService.activate_campaign(), Phase A, unchanged) --
     so there is nothing for list_due() to ever return in this environment.
  3. Even if a due row somehow existed, MailSendingService.
     prepare_and_send_step()'s controlled-test gate
     (controlled_test_send_allowed()) fails closed with BOTH
     mail_sending_mailbox_allowlist and mail_sending_recipient_allowlist
     unset -- no production values configured anywhere in this repo.
  4. Even past all of that, no row is ever claimed without this process
     first winning the WorkerLeaseService's atomic database lease --
     TAKEOVER PROTECTION means a second accidental instance can never
     process rows concurrently with a live leader.

Single-worker only, deliberately (see the Phase C design report's SQLite/
concurrency findings): MailboxSendPolicy quota/pacing enforcement in
MailSendingService is a plain COUNT/SELECT read, not an atomic
reservation -- safe under exactly one concurrent claimant, not proven
safe under N. The leadership lease is what keeps this true even if
Railway is ever misconfigured with more than one replica -- see
WorkerLeaseStore's own module docstring.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from app.config import settings
from app.models.activity import ActivityCategory, ActivitySource
from app.models.mail import MailCampaignStatus, MailSendWindow, MailSequenceStep
from app.services.activity_log_service import ActivityLogService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import (
    WORKER_DUE_ROW_BATCH_SIZE,
    WORKER_LEASE_DURATION_SECONDS,
    WORKER_POLL_INTERVAL_SECONDS,
    WORKER_RECOVERY_INTERVAL_SECONDS,
    MailSenderPort,
    MailSendingService,
)
from app.services.worker_lease_service import WorkerLeaseService

# A tick is considered stalled (worker "dead" for /health purposes) once
# this many poll intervals have passed with no observed tick -- see
# liveness_snapshot(). A provisional multiplier, not a measured value.
_STALLED_TICK_MULTIPLIER = 3


@dataclass(frozen=True)
class TickResult:
    is_leader: bool
    due_rows_seen: int
    sent: int
    blocked: int


class MailExecutionWorker:
    def __init__(
        self,
        *,
        mail_sending_service: MailSendingService,
        mail_campaign_service: MailCampaignService,
        lease_service: WorkerLeaseService,
        sender: MailSenderPort,
        activity_log: ActivityLogService | None = None,
        poll_interval_seconds: int = WORKER_POLL_INTERVAL_SECONDS,
        lease_duration_seconds: int = WORKER_LEASE_DURATION_SECONDS,
        recovery_interval_seconds: int = WORKER_RECOVERY_INTERVAL_SECONDS,
        batch_size: int = WORKER_DUE_ROW_BATCH_SIZE,
    ):
        self.mail_sending_service = mail_sending_service
        self.mail_campaign_service = mail_campaign_service
        self.lease_service = lease_service
        self.sender = sender
        # Optional (matching MailboxService's own activity_log convention --
        # see that class's docstring) so every existing test/call site that
        # constructs a worker without it keeps working unchanged; every
        # emission below is best-effort and skipped when this is None.
        self.activity_log = activity_log
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_duration_seconds = lease_duration_seconds
        self.recovery_interval_seconds = recovery_interval_seconds
        self.batch_size = batch_size

        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_tick_at: datetime | None = None
        self._last_recovery_at: datetime | None = None

    async def _log(self, event_type: str, summary: str) -> None:
        """Best-effort structural event for worker lifecycle/leadership
        transitions -- see this module's own docstring on activity_log
        being optional. Never includes anything beyond the fixed summary
        text and holder_id (no message bodies/tokens/PII exist at this
        layer to begin with)."""
        if self.activity_log is None:
            return
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=summary,
            entity_type="mail_worker",
            entity_id=self.lease_service.holder_id,
        )

    # --- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Called once from app/main.py's lifespan startup. A no-op
        (structurally incapable of ever polling) unless
        mail_sending_engine_enabled is True -- see this module's own
        docstring point 1. Also a no-op if already started (in-process
        duplicate-start guard -- the real cross-process guard is the
        database lease, this just prevents a redundant second task
        within THIS one process)."""
        if not settings.mail_sending_engine_enabled:
            logger.info("Phase C worker not started: mail_sending_engine_enabled is False.")
            return
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())
        logger.info(f"Phase C worker started (holder_id={self.lease_service.holder_id}).")

    async def stop(self) -> None:
        """Called once from app/main.py's lifespan shutdown, BEFORE any
        store's close() -- cancels the loop and awaits it, then
        best-effort releases the lease, so no coroutine is mid-write when
        connections close and a graceful handoff (rather than waiting for
        the lease to merely expire) is possible for whichever process
        picks up next."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.lease_service.release()
        logger.info("Phase C worker stopped.")
        await self._log("mail_worker.stopped", "The mail execution worker stopped.")

    async def _run_forever(self) -> None:
        await self._log("mail_worker.started", "The mail execution worker started.")
        try:
            await self._run_recovery(datetime.now(timezone.utc))
        except Exception:
            logger.exception("Phase C worker: startup recovery sweep failed.")
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                # A single bad tick must never kill the loop -- log and
                # keep going; the next tick tries again.
                logger.exception("Phase C worker: tick failed.")
            await asyncio.sleep(self.poll_interval_seconds)

    # --- One tick ---------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> TickResult:
        """One poll cycle: renew/acquire leadership, run the periodic
        recovery sweep if due, claim and process a bounded batch of due
        rows. Directly callable/testable without the sleep loop above --
        every test in this phase drives this method directly, never the
        real `while True`. `now` is injectable (defaults to the real
        wall clock) -- matching this codebase's established convention
        (reap_orphans(now), process_one_due_step(..., now=now), etc.)
        of never computing "now" internally where a caller might need a
        deterministic, testable value instead."""
        now = now or datetime.now(timezone.utc)
        self._last_tick_at = now

        was_leader = self.lease_service._currently_leader
        is_leader = await self.lease_service.try_acquire_or_renew(now, self.lease_duration_seconds)
        if is_leader and not was_leader:
            await self._log("mail_worker.leadership_acquired", "The mail execution worker acquired leadership.")
        elif was_leader and not is_leader:
            await self._log("mail_worker.leadership_lost", "The mail execution worker lost leadership.")
        if not is_leader:
            return TickResult(is_leader=False, due_rows_seen=0, sent=0, blocked=0)

        if (
            self._last_recovery_at is None
            or (now - self._last_recovery_at).total_seconds() >= self.recovery_interval_seconds
        ):
            await self._run_recovery(now)
            self._last_recovery_at = now

        schedule_cache: dict[str, tuple[list[MailSendWindow], str]] = {}
        steps_cache: dict[str, list[MailSequenceStep]] = {}

        async def confirm_leadership() -> bool:
            return await self.lease_service.is_leader(datetime.now(timezone.utc))

        due_rows = await self.mail_sending_service.step_store.list_due(now, limit=self.batch_size)
        sent = 0
        blocked = 0
        for row in due_rows:
            if row.mail_campaign_id not in schedule_cache:
                schedule = await self.mail_campaign_service.get_schedule(row.mail_campaign_id)
                schedule_cache[row.mail_campaign_id] = (schedule.windows, schedule.timezone or "UTC")
            windows, timezone_name = schedule_cache[row.mail_campaign_id]

            if row.mail_campaign_id not in steps_cache:
                steps_cache[row.mail_campaign_id] = await self.mail_campaign_service.list_steps(row.mail_campaign_id)
            sequence_steps = steps_cache[row.mail_campaign_id]

            outcome = await self.mail_sending_service.prepare_and_send_step(
                row,
                sender=self.sender,
                claimed_by=self.lease_service.holder_id,
                sequence_steps=sequence_steps,
                windows=windows,
                timezone_name=timezone_name,
                now=now,
                confirm_leadership=confirm_leadership,
            )
            if outcome.sent:
                sent += 1
            else:
                blocked += 1

        return TickResult(is_leader=True, due_rows_seen=len(due_rows), sent=sent, blocked=blocked)

    # --- Recovery -----------------------------------------------------------------

    async def _run_recovery(self, now: datetime) -> None:
        """Startup + periodic recovery sweep -- see the Phase C design
        report's recovery section. Order: reap first (so a stale CLAIMED
        row is back to QUEUED, and a stale SENDING row is UNKNOWN, before
        anything else looks at it), then reconcile any stalled
        progression per ACTIVE campaign, then resume any mailbox-paused
        enrollment whose sticky mailbox is usable again, then resume any
        PREPARE_CONFIG_BLOCKED enrollment -- but ONLY if the missing
        configuration prerequisite now reads as present (see
        MailSendingService.resume_prepare_config_blocked_enrollments()'s
        own docstring); a genuinely still-missing value means this is a
        complete no-op, not a blind periodic retry. Deliberately does
        NOT touch PREPARE_TRANSIENT_EXHAUSTED/PREPARE_UNCLASSIFIED_BLOCKED
        enrollments at all -- those have no automatic recovery path;
        see MailSendingService.resolve_prepare_blocked_step()."""
        reap_result = await self.mail_sending_service.reap_orphans(now)
        if reap_result.reset_to_queued or reap_result.marked_unknown:
            logger.info(
                f"Phase C recovery: reset_to_queued={reap_result.reset_to_queued}, "
                f"marked_unknown={reap_result.marked_unknown}."
            )

        campaigns = await self.mail_sending_service.campaign_store.list()
        for campaign in campaigns:
            if campaign.status != MailCampaignStatus.ACTIVE:
                continue
            schedule = await self.mail_campaign_service.get_schedule(campaign.mail_campaign_id)
            sequence_steps = await self.mail_campaign_service.list_steps(campaign.mail_campaign_id)
            await self.mail_sending_service.reconcile_stalled_progressions(
                mail_campaign_id=campaign.mail_campaign_id,
                sequence_steps=sequence_steps,
                windows=schedule.windows,
                timezone_name=schedule.timezone or "UTC",
                now=now,
            )

        resumed = await self.mail_sending_service.resume_mailbox_paused_enrollments(now)
        if resumed:
            logger.info(f"Phase C recovery: resumed {resumed} mailbox-paused enrollment(s).")

        prepare_resumed = await self.mail_sending_service.resume_prepare_config_blocked_enrollments(now)
        if prepare_resumed:
            logger.info(f"Phase C recovery: resumed {prepare_resumed} config-blocked enrollment(s).")

    # --- Observability ------------------------------------------------------------

    def liveness_snapshot(self, now: datetime | None = None) -> dict:
        """Consumed by GET /health -- see app/main.py. Deliberately never
        used to change /health's HTTP status code (a disabled or
        non-leader worker is a NORMAL state, not an unhealthy process --
        flipping the status code on it would make Railway restart a
        perfectly healthy web service; see this module's own docstring
        and the Phase C design report's observability section)."""
        now = now or datetime.now(timezone.utc)
        if not settings.mail_sending_engine_enabled:
            return {"state": "disabled", "engine_enabled": False}
        if self._task is None or self._task.done():
            return {"state": "dead", "engine_enabled": True}

        seconds_since_tick = (now - self._last_tick_at).total_seconds() if self._last_tick_at else None
        if seconds_since_tick is not None and seconds_since_tick > self.poll_interval_seconds * _STALLED_TICK_MULTIPLIER:
            state = "stalled"
        elif self.lease_service._currently_leader:
            state = "leader"
        else:
            state = "non_leader"

        return {
            "state": state,
            "engine_enabled": True,
            "seconds_since_last_tick": seconds_since_tick,
            "holder_id": self.lease_service.holder_id,
        }
