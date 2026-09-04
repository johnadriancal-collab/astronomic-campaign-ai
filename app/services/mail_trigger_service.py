"""
MailTriggerService -- Trigger feature, Stage 5D (2026-09-04).

Owns BOTH Trigger CRUD (MailLeadStartTrigger definitions) and occurrence
discovery/freeze/reconciliation (the mechanism that actually starts a
TRIGGERED-mode PENDING lead) -- one service, matching this codebase's
one-service-per-concern convention (CrmImportService, MailSuppressionService),
specifically because occurrence execution's own campaign-state/eligibility
checks are the same ones CRUD's own lifecycle validation needs; splitting
them into two services would just duplicate that logic and let it drift.

Holds its own `campaign_store` reference directly for reads and the one
targeted `lead_start_mode` write -- the same precedent MailSendingService
itself already uses (a sibling service needing simple campaign access
without importing MailCampaignService's whole lifecycle-mutation surface).
Step 1 materialization goes through MailSendingService.create_step1_execution()
-- the SAME idempotent method activate_campaign()/_reconcile_batch() use --
never a second implementation of that logic.

Scope discipline (Stage 5D only): controls WHEN a PENDING lead becomes
ACTIVE + gets a Step 1 row. Never touches send windows, mailbox quota,
pacing, suppression enforcement semantics, or Gmail/provider behavior --
those stay entirely inside MailSendingService.prepare_and_send_step(),
unchanged, downstream of whatever this service starts.

Due-occurrence discovery here is deliberately MINIMAL (see
process_due_occurrences()'s own docstring) -- Stage 5E owns multi-day
catch-up/missed-occurrence semantics; this stage only ever considers
TODAY's (campaign-local) scheduled occurrence per trigger.
"""

import uuid
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentStatus,
    MailLeadStartTrigger,
    MailTriggerOccurrence,
    validate_lead_start_trigger,
)
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_lead_start_trigger_store import (
    MailLeadStartTriggerNotFoundError,
    MailLeadStartTriggerStore,
)
from app.repositories.mail_suppression_store import MailSuppressionStore
from app.repositories.mail_trigger_occurrence_store import MailTriggerOccurrenceStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_campaign_service import MailCampaignNotFound, MailCampaignService
from app.services.mail_sending_service import MailSendingService

# Trigger configuration is allowed anywhere campaign configuration is
# meaningfully editable -- matching the Trigger design's own "editable
# live, closer to Channels than to the DRAFT-only Schedule lock" framing.
# ARCHIVED (terminal) and legacy COMPLETED are excluded: COMPLETED is a
# pre-persistent-campaign status this codebase no longer transitions INTO
# on its own (see mail_campaign_service.py's "Remove maybe_complete_campaign()"
# history) -- a COMPLETED campaign is either reopened to ACTIVE by
# add_prospects() first (at which point it's eligible again) or stays a
# frozen historical record; there's no compelling reason for Trigger
# configuration to be the one thing that treats it as still-live.
_TRIGGER_CONFIGURABLE_STATUSES = frozenset(
    {MailCampaignStatus.DRAFT, MailCampaignStatus.READY, MailCampaignStatus.ACTIVE, MailCampaignStatus.PAUSED}
)


class MailCampaignNotEligibleForTriggersError(Exception):
    def __init__(self, mail_campaign_id: str, status):
        self.mail_campaign_id = mail_campaign_id
        self.status = status
        super().__init__(
            f"MailCampaign {mail_campaign_id} (status={status}) is not eligible for Trigger configuration."
        )


def parse_local_time(value: str) -> time:
    """Mirrors mail_campaign_service.py's own _parse_time_of_day() exactly
    (HH:MM/HH:MM:SS via time.fromisoformat) -- not imported cross-module
    since that helper is module-private by convention there; this is a
    deliberately identical, independently-owned copy for Trigger CRUD's
    own request-layer parsing."""
    try:
        return time.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"'{value}' is not a valid time of day (expected HH:MM).") from e


class MailTriggerService:
    def __init__(
        self,
        *,
        trigger_store: MailLeadStartTriggerStore,
        occurrence_store: MailTriggerOccurrenceStore,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        suppression_store: MailSuppressionStore,
        sending_service: MailSendingService,
        mail_campaign_service: MailCampaignService,
        activity_log: ActivityLogService,
    ):
        self.trigger_store = trigger_store
        self.occurrence_store = occurrence_store
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.suppression_store = suppression_store
        self.sending_service = sending_service
        self.mail_campaign_service = mail_campaign_service
        self.activity_log = activity_log

    async def _require_campaign(self, mail_campaign_id: str):
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)
        return campaign

    def _require_configurable(self, campaign) -> None:
        if campaign.status not in _TRIGGER_CONFIGURABLE_STATUSES:
            raise MailCampaignNotEligibleForTriggersError(campaign.mail_campaign_id, campaign.status)

    # =====================================================================
    # Trigger CRUD
    # =====================================================================

    async def list_triggers(self, mail_campaign_id: str) -> list[MailLeadStartTrigger]:
        await self._require_campaign(mail_campaign_id)
        return await self.trigger_store.list_for_campaign(mail_campaign_id)

    async def create_trigger(
        self,
        mail_campaign_id: str,
        weekdays: list[int],
        local_time: str,
        leads_to_start: int,
        enabled: bool = True,
        actor: str | None = None,
    ) -> MailLeadStartTrigger:
        """Creates a trigger and -- ONLY on the first successful creation
        for a campaign still in "immediate" mode -- flips
        lead_start_mode to "triggered". This never starts a single lead:
        _prepare_activation()/_reconcile_batch() (Stage 5C) are the only
        things that ever read lead_start_mode to decide eager-vs-pending,
        and neither is called from here. A READY campaign's already-
        snapshotted PENDING enrollments simply become the future Not
        Started pool -- no Step 1 is created by this method."""
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_configurable(campaign)
        validate_lead_start_trigger(weekdays, leads_to_start)
        parsed_time = parse_local_time(local_time)

        now = datetime.now(timezone.utc)
        trigger = MailLeadStartTrigger(
            trigger_id=str(uuid.uuid4()),
            mail_campaign_id=mail_campaign_id,
            weekdays=sorted(set(weekdays)),
            local_time=parsed_time,
            leads_to_start=leads_to_start,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        await self.trigger_store.create(trigger)

        # One-way transition (Stage 5C's own approved design) -- never
        # flipped back by delete/disable, and never re-checked here on an
        # already-"triggered" campaign (a second/third trigger creation
        # is a pure no-op on this field).
        if campaign.lead_start_mode != "triggered":
            await self.campaign_store.save(
                campaign.model_copy(update={"lead_start_mode": "triggered", "updated_at": now})
            )

        await self.activity_log.record(
            event_type="mail_lead_start_trigger.created",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'A lead-start trigger was created for campaign "{campaign.name}" (leads_to_start={leads_to_start}).',
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
            entity_name=campaign.name,
            metadata={"trigger_id": trigger.trigger_id, "leads_to_start": leads_to_start, "weekdays": trigger.weekdays},
            actor=actor,
        )
        return trigger

    async def update_trigger(
        self,
        mail_campaign_id: str,
        trigger_id: str,
        weekdays: list[int] | None = None,
        local_time: str | None = None,
        leads_to_start: int | None = None,
        enabled: bool | None = None,
        actor: str | None = None,
    ) -> MailLeadStartTrigger:
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_configurable(campaign)
        trigger = await self.trigger_store.get(trigger_id)
        if trigger is None or trigger.mail_campaign_id != mail_campaign_id:
            raise MailLeadStartTriggerNotFoundError(trigger_id)

        new_weekdays = sorted(set(weekdays)) if weekdays is not None else trigger.weekdays
        new_leads_to_start = leads_to_start if leads_to_start is not None else trigger.leads_to_start
        validate_lead_start_trigger(new_weekdays, new_leads_to_start)
        new_local_time = parse_local_time(local_time) if local_time is not None else trigger.local_time
        new_enabled = trigger.enabled if enabled is None else enabled

        updated = trigger.model_copy(
            update={
                "weekdays": new_weekdays,
                "local_time": new_local_time,
                "leads_to_start": new_leads_to_start,
                "enabled": new_enabled,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await self.trigger_store.save(updated)
        await self.activity_log.record(
            event_type="mail_lead_start_trigger.updated",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'A lead-start trigger was updated for campaign "{campaign.name}".',
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
            entity_name=campaign.name,
            metadata={"trigger_id": trigger_id, "leads_to_start": new_leads_to_start, "enabled": new_enabled},
            actor=actor,
        )
        return updated

    async def delete_trigger(self, mail_campaign_id: str, trigger_id: str, actor: str | None = None) -> None:
        """lead_start_mode is deliberately NEVER reverted here, even if
        this was the campaign's last trigger -- see MailCampaign.
        lead_start_mode's own docstring: a one-way transition. A
        "triggered" campaign with zero remaining triggers correctly
        leaves its PENDING pool waiting indefinitely (Stage 5C's own
        approved behavior), not silently reverting to eager activation."""
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_configurable(campaign)
        trigger = await self.trigger_store.get(trigger_id)
        if trigger is None or trigger.mail_campaign_id != mail_campaign_id:
            raise MailLeadStartTriggerNotFoundError(trigger_id)

        await self.trigger_store.delete(trigger_id)
        await self.activity_log.record(
            event_type="mail_lead_start_trigger.deleted",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'A lead-start trigger was deleted for campaign "{campaign.name}".',
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
            entity_name=campaign.name,
            metadata={"trigger_id": trigger_id},
            actor=actor,
        )

    # =====================================================================
    # Occurrence discovery / freeze / reconciliation (worker-invoked)
    # =====================================================================

    @staticmethod
    def _scheduled_for(local_date: date, local_time: time, timezone_name: str) -> datetime:
        """Deterministic occurrence identity component -- see
        MailTriggerOccurrence's own docstring: `(trigger_id, scheduled_for)`
        must be computable identically regardless of WHEN a worker tick
        happens to observe it. Never derived from worker-run time."""
        tz = ZoneInfo(timezone_name)
        local_dt = datetime.combine(local_date, local_time, tzinfo=tz)
        return local_dt.astimezone(timezone.utc)

    async def process_due_occurrences(self, now: datetime) -> None:
        """Called once per MailExecutionWorker tick, only while this
        process holds worker leadership, BEFORE ordinary due-step
        processing (see MailExecutionWorker.tick()) -- structurally
        unreachable when MAIL_SENDING_ENGINE_ENABLED is false, since
        start() never schedules the tick loop at all in that case. The
        SAME engine flag that gates send execution therefore also gates
        every Trigger lead-start, with no separate check needed here.

        Stage 5D's MINIMUM due-occurrence discovery (Stage 5E owns
        multi-day catch-up / "most recent of several" semantics): for
        each ACTIVE, "triggered" campaign, each ENABLED trigger, consider
        ONLY today's (campaign-local) scheduled occurrence. If it's due
        (scheduled_for <= now) and within the campaign's current active
        streak (scheduled_for >= execution_active_since), execute it
        exactly once via the freeze-then-reconcile flow below. A trigger
        whose scheduled time hasn't arrived yet today is simply not
        considered -- never a future occurrence, and never a prior day's
        (no catch-up invented here)."""
        campaigns = await self.campaign_store.list()
        for campaign in campaigns:
            if campaign.status != MailCampaignStatus.ACTIVE:
                continue
            if campaign.lead_start_mode != "triggered":
                continue
            if campaign.execution_active_since is None or not campaign.timezone:
                continue  # defensive -- ACTIVE always has both; never crash a whole tick over one bad row

            triggers = await self.trigger_store.list_for_campaign(campaign.mail_campaign_id)
            if not triggers:
                continue

            tz = ZoneInfo(campaign.timezone)
            today_local = now.astimezone(tz).date()
            today_weekday = today_local.weekday()

            for trigger in triggers:
                if not trigger.enabled:
                    continue
                if today_weekday not in trigger.weekdays:
                    continue
                scheduled_for = self._scheduled_for(today_local, trigger.local_time, campaign.timezone)
                if scheduled_for > now:
                    continue  # not due yet -- never execute a future occurrence
                if scheduled_for < campaign.execution_active_since:
                    continue  # outside the campaign's current active streak

                await self._execute_occurrence(campaign, trigger, scheduled_for, now)

    async def _execute_occurrence(self, campaign, trigger: MailLeadStartTrigger, scheduled_for: datetime, now: datetime) -> None:
        """Steps 1-4 of the approved freeze-before-mutation contract:
        discover/create the durable occurrence, freeze its candidate
        cohort (steps 2+3, atomic), then reconcile (step 4) -- see this
        module's own docstring and MailTriggerOccurrenceStore's. Every
        step here is idempotent/resumable from durable state alone, so
        re-running this for the SAME (trigger_id, scheduled_for) -- from
        a second tick, a crash-retry, or rediscovery of an
        already-COMPLETED occurrence -- is always safe."""
        occurrence = await self.occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
        if occurrence is not None and occurrence.status == "COMPLETED":
            return  # idempotent rediscovery -- nothing left to do

        if occurrence is None:
            candidate_occurrence = MailTriggerOccurrence(
                trigger_id=trigger.trigger_id,
                mail_campaign_id=campaign.mail_campaign_id,
                scheduled_for=scheduled_for,
                target_count=trigger.leads_to_start,
                created_at=now,
            )
            await self.occurrence_store.create_occurrence(candidate_occurrence)
            # Whether THIS call won the claim or lost it to a concurrent
            # one, the durable row is now the single source of truth --
            # re-fetch rather than trust either branch's own belief.
            occurrence = await self.occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
            if occurrence is None:
                return  # defensive: should be unreachable, nothing to recover into
            if occurrence.status == "COMPLETED":
                return

        if occurrence.frozen_at is None:
            step1 = await self._get_step1(campaign.mail_campaign_id)
            candidate_ids = await self._select_candidates(campaign.mail_campaign_id, occurrence.target_count, step1)
            # freeze_members()'s own return value doesn't change what
            # happens next -- whether THIS call performed the freeze or a
            # concurrent one already did, list_members() below reads
            # whatever is durably there, which is always correct to
            # reconcile from.
            await self.occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, candidate_ids, now)

        await self._reconcile_occurrence(campaign, trigger.trigger_id, scheduled_for, now)

    async def _get_step1(self, mail_campaign_id: str):
        steps = await self.mail_campaign_service.list_steps(mail_campaign_id)
        return next((s for s in steps if s.step_number == 1), None)

    async def _select_candidates(self, mail_campaign_id: str, limit: int, step1) -> list[str]:
        """Oldest eligible PENDING enrollment first (enrolled_at, then
        enrollment_id as a deterministic tie-breaker), across every batch
        -- up to `limit`. "Not already frozen into another occurrence" is
        NOT re-checked here via a separate query: the occurrence store's
        own global UNIQUE(enrollment_id) constraint is the actual
        correctness boundary (freeze_members() silently excludes an
        already-claimed id rather than erroring) -- adding a second,
        redundant cross-store query here would be exactly the kind of
        unrelated infrastructure the approved design said to avoid. The
        accepted consequence, matching the approved V1 decision, is that
        an occurrence can finish with fewer than `limit` members if some
        selected candidates turn out to already be claimed elsewhere --
        never refilled.

        Excludes a PENDING enrollment that already, unexpectedly, has a
        Step 1 row (the same corruption case Stage 5C's
        _find_incomplete_activation() defends against) -- reconciliation
        independently re-verifies this too (defense in depth, and the
        authoritative check), so this is purely an optimization that
        avoids wasting a frozen slot on a row already known to be
        inconsistent."""
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        pending = [e for e in enrollments if e.status == MailEnrollmentStatus.PENDING]
        pending.sort(key=lambda e: (e.enrolled_at, e.enrollment_id))

        candidates: list[str] = []
        for enrollment in pending:
            if len(candidates) >= limit:
                break
            if step1 is not None:
                existing = await self.enrollment_step_store.get_by_enrollment_and_step(enrollment.enrollment_id, step1.step_id)
                if existing is not None:
                    continue
            candidates.append(enrollment.enrollment_id)
        return candidates

    async def _reconcile_occurrence(self, campaign, trigger_id: str, scheduled_for: datetime, now: datetime) -> None:
        members = await self.occurrence_store.list_members(trigger_id, scheduled_for)
        unreconciled = [m for m in members if m.reconciled_at is None]

        if unreconciled:
            step1 = await self._get_step1(campaign.mail_campaign_id)
            schedule = await self.mail_campaign_service.get_schedule(campaign.mail_campaign_id)
            for member in unreconciled:
                await self._reconcile_member(campaign, step1, schedule, member, now)

        members = await self.occurrence_store.list_members(trigger_id, scheduled_for)
        if any(m.reconciled_at is None for m in members):
            return  # a member's own reconciliation failed to persist somehow -- a later tick resumes it

        started_count = sum(1 for m in members if m.outcome == "STARTED")
        skipped_count = sum(1 for m in members if m.outcome == "SKIPPED_INELIGIBLE")
        completed = await self.occurrence_store.complete_occurrence(trigger_id, scheduled_for, started_count, now)
        if not completed:
            return  # already completed by an earlier attempt -- no duplicate activity log entry

        occurrence = await self.occurrence_store.get_occurrence(trigger_id, scheduled_for)
        await self.activity_log.record(
            event_type="mail_trigger_occurrence.completed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=(
                f'A lead-start trigger occurrence completed for campaign "{campaign.name}" '
                f"(requested {occurrence.target_count if occurrence else '?'}, started {started_count}, skipped {skipped_count})."
            ),
            entity_type="mail_campaign",
            entity_id=campaign.mail_campaign_id,
            entity_name=campaign.name,
            metadata={
                "trigger_id": trigger_id,
                "target_count": occurrence.target_count if occurrence else None,
                "frozen_member_count": len(members),
                "started_count": started_count,
                "skipped_count": skipped_count,
            },
        )

    async def _reconcile_member(self, campaign, step1, schedule, member, now: datetime) -> None:
        """The reconciliation state machine (Stage 5D, hardened 2026-09-04
        after a discovered crash gap -- see the two PENDING cases below):

          Case 1 -- PENDING + no Step1:
            re-check live suppression; if suppressed -> suppress_enrollment()
            + SKIPPED_INELIGIBLE; else create Step1, enrollment -> ACTIVE,
            member -> STARTED.

          Case 2 -- PENDING + the EXPECTED Step1 already exists (crash
            recovery: Write A -- create_step1_execution() -- succeeded in
            an earlier, interrupted attempt at reconciling THIS SAME
            frozen member, but the process crashed before Write B, the
            ACTIVE flip): re-check live suppression AGAIN (fresh -- it may
            have changed since the crashed attempt); if suppressed, do
            NOT activate -- suppress_enrollment() (the exact same cascade
            Case 1 and every other suppression path in this codebase
            already uses) transitions the already-QUEUED Step1 row to
            SKIPPED_SUPPRESSED, so it can never remain an endlessly-
            claimable orphan (confirmed directly against that method's
            own source before relying on it here -- no new cleanup
            mechanism was needed); member -> SKIPPED_INELIGIBLE. If not
            suppressed, do NOT create a second Step1 (existing_step1 IS
            the one Write A already made) -- just finish Write B:
            enrollment -> ACTIVE, member -> STARTED.

            This case is safe BECAUSE of frozen-member provenance, not
            despite it: exactly three places in this whole codebase ever
            call create_step1_execution() -- _prepare_activation() and
            _reconcile_batch() are both gated `lead_start_mode ==
            "immediate"` (Stage 5C), and lead_start_mode is a one-way
            transition -- so neither can be the source of a Step1 row for
            an enrollment that is a frozen member of a Trigger occurrence
            (which can only exist once the campaign is already
            "triggered"). Combined with mail_enrollment_steps' own
            UNIQUE(enrollment_id, step_id) constraint, the row
            existing_step1 refers to can only ever have been created by
            THIS reconciliation flow's own earlier attempt at THIS
            member. Stage 5C's _find_incomplete_activation() treats the
            same durable SHAPE as corruption in ITS OWN, differently-
            scoped context (any enrollment campaign-wide, no per-
            enrollment provenance) -- that rule is deliberately NOT
            reused here; the two are answering different questions.

          Case 3 -- ACTIVE + expected Step1 (crash recovery: Write A and
            Write B both already succeeded, only the member's own
            STARTED bookkeeping didn't persist): member -> STARTED, no
            duplicate Step1.

          Case 4 -- everything else (SUPPRESSED, FAILED, COMPLETED,
            unexpected PAUSED, ACTIVE-without-the-expected-Step1, or any
            other unrecognized combination): member -> SKIPPED_INELIGIBLE,
            no repair invented -- these remain genuinely undistinguishable
            from real corruption, unlike Case 2."""
        enrollment = await self.enrollment_store.get(member.enrollment_id)
        if enrollment is None:
            await self.occurrence_store.mark_member_reconciled(
                member.trigger_id, member.scheduled_for, member.enrollment_id, "SKIPPED_INELIGIBLE", now
            )
            return

        existing_step1 = None
        if step1 is not None:
            existing_step1 = await self.enrollment_step_store.get_by_enrollment_and_step(enrollment.enrollment_id, step1.step_id)

        outcome = "SKIPPED_INELIGIBLE"
        if step1 is not None and enrollment.status == MailEnrollmentStatus.PENDING:
            # Cases 1 and 2 share the same live suppression re-check --
            # they differ only in whether Write A (Step1 creation) still
            # needs to happen.
            normalized = normalize_email(enrollment.email_at_enrollment)
            suppression = await self.suppression_store.get(normalized) if normalized else None
            if suppression is not None and suppression.active:
                # Reuses the EXACT same cascade as everywhere else in this
                # codebase -- neutralizes a Case-2 orphan Step1 too (see
                # this method's own docstring).
                await self.sending_service.suppress_enrollment(enrollment, now)
                outcome = "SKIPPED_INELIGIBLE"
            elif existing_step1 is None:
                # Case 1: genuinely fresh start.
                await self.sending_service.create_step1_execution(
                    enrollment=enrollment, step1=step1, windows=schedule.windows,
                    timezone_name=schedule.timezone or campaign.timezone, now=now,
                )
                await self.enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))
                outcome = "STARTED"
            else:
                # Case 2: Write A already succeeded in an earlier,
                # crashed attempt -- never re-create Step1, just finish
                # the one remaining write.
                await self.enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))
                outcome = "STARTED"
        elif enrollment.status == MailEnrollmentStatus.ACTIVE and existing_step1 is not None:
            outcome = "STARTED"  # Case 3
        # else: Case 4 -- falls through to the SKIPPED_INELIGIBLE default above.

        await self.occurrence_store.mark_member_reconciled(
            member.trigger_id, member.scheduled_for, member.enrollment_id, outcome, now
        )
