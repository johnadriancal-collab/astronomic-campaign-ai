"""
MailBatchReconciliationWorker -- Astronomic Mail Phase 2 Stage 3. The
in-process asyncio background task that finishes any MailEnrollmentBatch
left PREPARING by a crashed/interrupted add_prospects() call, and cleans
up orphaned MailEnrollmentBatchMember rows (see MailCampaignService.
reconcile_all_preparing_batches()/cleanup_orphan_batch_members()'s own
docstrings for exactly what each does and why both are already fully
idempotent and safe to run redundantly from multiple contexts).

STRUCTURALLY INCAPABLE OF SENDING ANYTHING: this class holds no
MailSenderPort, no GmailSender, no reference to any provider client at
all -- it is wired from exactly two things, MailCampaignService (already
proven, by its own docstrings, to make zero Gmail/provider calls
anywhere in add_prospects()/_reconcile_batch()/cleanup_orphan_batch_
members()) and nothing else. Deliberately independent of
settings.mail_sending_engine_enabled -- unlike MailExecutionWorker (Phase
C), this is pure campaign/enrollment bookkeeping/recovery, not a send
path, so there is no reason to gate it behind the same flag; it runs
(and should run) even in every environment where the engine itself stays
off, exactly like every other Add Prospects capability in this phase.

Cadence is deliberately much more conservative than MailExecutionWorker's
own 45-second send-poll interval (see WORKER_POLL_INTERVAL_SECONDS in
mail_sending_service.py) -- this is recovery bookkeeping for a rare
crash/interruption case, not a responsive send loop.
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from app.services.mail_campaign_service import MailCampaignService

# Conservative on purpose -- see this module's own docstring. 15 minutes
# is far more than enough headroom for a PREPARING batch to either finish
# synchronously (the common case) or be picked up by the very next sweep;
# nothing here needs to be responsive the way the send worker does.
RECONCILIATION_POLL_INTERVAL_SECONDS = 900


class MailBatchReconciliationWorker:
    def __init__(
        self,
        *,
        mail_campaign_service: MailCampaignService,
        poll_interval_seconds: int = RECONCILIATION_POLL_INTERVAL_SECONDS,
    ):
        self.mail_campaign_service = mail_campaign_service
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        """Called once from app/main.py's lifespan startup. Always starts
        (unlike MailExecutionWorker.start(), this has no
        mail_sending_engine_enabled gate -- see this module's own
        docstring for why). A no-op if already started (in-process
        duplicate-start guard)."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())
        logger.info("Mail batch reconciliation worker started.")

    async def stop(self) -> None:
        """Called once from app/main.py's lifespan shutdown, before any
        store's close() -- cancels the loop and awaits it, so no
        coroutine is mid-write when connections close."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Mail batch reconciliation worker stopped.")

    async def _run_forever(self) -> None:
        try:
            await self.run_once()
        except Exception:
            logger.exception("Mail batch reconciliation worker: startup sweep failed.")
        while not self._stopping:
            await asyncio.sleep(self.poll_interval_seconds)
            if self._stopping:
                break
            try:
                await self.run_once()
            except Exception:
                # A single bad sweep must never kill the loop -- log and
                # keep going; the next sweep tries again.
                logger.exception("Mail batch reconciliation worker: sweep failed.")

    async def run_once(self) -> tuple[int, int]:
        """One sweep: reconcile every PREPARING batch, then clean up any
        now-orphaned member rows. Returns (batches_reconciled,
        members_deleted). Public (not just internal to the loop) so
        app startup can also call this directly, once, before the
        periodic loop's own first tick -- see app/main.py."""
        reconciled = await self.mail_campaign_service.reconcile_all_preparing_batches()
        deleted = await self.mail_campaign_service.cleanup_orphan_batch_members(datetime.now(timezone.utc))
        if reconciled or deleted:
            logger.info(f"Mail batch reconciliation sweep: {reconciled} batch(es) advanced, {deleted} orphaned member row(s) deleted.")
        return reconciled, deleted
