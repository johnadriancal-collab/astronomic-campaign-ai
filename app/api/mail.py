"""
Astronomic Mail routes -- Phase 1 (Foundation), Phase A's durable
execution lifecycle, and (Phase C) session-authenticated admin capability
for manually resolving an UNKNOWN send outcome.

IMPORTANT, and still load-bearing: there is NO route here (and none
anywhere else in this app) capable of dispatching a real Gmail/SMTP call.
/activate DOES move a campaign into MailCampaignStatus.ACTIVE and
materializes Step 1 execution rows (see MailCampaignService.
activate_campaign()) -- but "ACTIVE" here means "eligible for the Phase C
worker to act on," never "a message was sent," and that worker itself
will not even START unless mail_sending_engine_enabled is True (unset in
this repo), then still cannot invoke a provider without BOTH
controlled-test allowlists explicitly configured (also unset) AND
currently holding the database execution lease -- see
app/services/mail_execution_worker.py's own module docstring for the
full chain. The three routes at the bottom of this file
(POST .../resolve-sent, POST .../resolve-not-sent,
POST .../resolve-prepare-blocked) do NOT send anything either -- they
only let an authenticated human record what they already independently
confirmed happened (or didn't) for a row ALREADY stuck in UNKNOWN, or
explicitly resume a lead whose enrollment is PAUSED with no automatic
recovery path (PREPARE_TRANSIENT_EXHAUSTED/PREPARE_UNCLASSIFIED_BLOCKED);
see MailSendingService.resolve_unknown_step_confirmed_sent()/
_confirmed_not_sent()/resolve_prepare_blocked_step()'s own docstrings for
why these are fundamentally different capabilities from dispatching a
send. Also deliberately absent from this file: any route to drive
prepare_and_send_step()/reap_orphans()
directly (no /send, /queue, /dispatch, /worker/run).
"""

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies import (
    get_mail_campaign_csv_prospect_service,
    get_mail_campaign_service,
    get_mail_sending_service,
    get_mail_suppression_service,
    get_mail_trigger_service,
)
from app.models.mail import (
    MailCampaign,
    MailCampaignReview,
    MailCampaignSchedule,
    MailCampaignSharing,
    MailCampaignWorkload,
    MailContactSuppressionStatus,
    MailEnrollment,
    MailEnrollmentBatch,
    MailEnrollmentBatchSource,
    MailLeadStartTrigger,
    MailScheduleValidationError,
    MailSequenceStep,
    MailSuppression,
    MailSuppressionReason,
)
from app.models.mailbox import Mailbox
from app.services.crm_import_service import CrmImportBatchNotFound
from app.services.crm_service import CrmContactListNotFound
from app.services.mail_campaign_csv_prospect_service import MailCampaignCsvProspectService
from app.services.mail_campaign_service import (
    InvalidMailSequenceStepDelayError,
    InvalidMailTemplateVariableError,
    MailboxChannelNotFoundError,
    MailboxChannelNotUsableError,
    MailCampaignChannelsFrozenError,
    MailCampaignInvalidTransitionError,
    MailCampaignLegacyScheduleLockedError,
    MailCampaignNotEditableError,
    MailCampaignNotEligibleForProspectsError,
    MailCampaignNotFound,
    MailCampaignNotReadyError,
    MailCampaignService,
    MailSendingEngineDisabledError,
    MailSequenceStepNotFound,
)
from app.services.mail_sending_service import (
    MailSendingService,
    PrepareBlockedWrongStateError,
    UnknownStepNotFoundError,
    UnknownStepWrongStatusError,
)
from app.services.mail_suppression_service import (
    InvalidMailSuppressionEmailError,
    MailSuppressionNotFoundError,
    MailSuppressionService,
    UnsubscribeReversalNotAllowedError,
)
from app.services.mail_trigger_service import MailCampaignNotEligibleForTriggersError, MailTriggerService
from app.repositories.mail_lead_start_trigger_store import MailLeadStartTriggerNotFoundError

router = APIRouter(prefix="/mail", tags=["mail"])


def _operator_actor(request: Request) -> str | None:
    """"claude_operator" when this request was authenticated via the
    admin/service OPERATOR token (app/session_auth_middleware.py sets
    request.state.identity == "service_operator" for exactly that case);
    None (byte-identical to before this feature existed) for an ordinary
    Hub session or any other identity. Passed through to the relevant
    MailCampaignService method's `actor` argument so the resulting
    Activity Log entry is attributable -- see that middleware module's
    own "Attribution" docstring section."""
    return "claude_operator" if getattr(request.state, "identity", None) == "service_operator" else None


# --- Campaigns -----------------------------------------------------------


class MailCampaignCreateRequest(BaseModel):
    """`name` is the only truly required field (matching create_campaign()'s
    existing minimal contract, unchanged, so every prior caller keeps
    working). Everything else is optional, campaign-level configuration
    from the Create Campaign modal (Campaign Manager Integration Phase) --
    any field left unset here is simply not included in the follow-up patch
    below, so the campaign is created exactly like before with no extra
    fields written. `start_time`/`end_time` are "HH:MM" strings, matching
    the existing PATCH endpoint's convention."""

    name: str
    sharing: MailCampaignSharing | None = None
    sending_days: list[int] | None = None
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    all_hours: bool | None = None
    start_immediately: bool | None = None
    daily_lead_start_limit: int | None = None


@router.get("/campaigns", response_model=list[MailCampaign])
async def list_campaigns(service: MailCampaignService = Depends(get_mail_campaign_service)):
    return await service.list_campaigns()


@router.post("/campaigns", response_model=MailCampaign)
async def create_campaign(
    payload: MailCampaignCreateRequest, request: Request, service: MailCampaignService = Depends(get_mail_campaign_service)
):
    """
    Two existing service calls composed at this route only (create_campaign()
    and update_campaign() are both unchanged in what they individually
    accept) -- this reuses update_campaign()'s existing soft validation
    (time-string parsing, day-range/timezone checks, all_hours full-day
    forcing, daily_lead_start_limit positivity) instead of duplicating any
    of it here. If no optional field was provided, the second call is
    skipped entirely and behavior is byte-identical to before this phase.
    """
    actor = _operator_actor(request)
    campaign = await service.create_campaign(payload.name, actor=actor)
    patch = payload.model_dump(exclude={"name"}, exclude_none=True)
    if not patch:
        return campaign
    try:
        return await service.update_campaign(campaign.mail_campaign_id, patch, actor=actor)
    except MailScheduleValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/campaigns/{mail_campaign_id}", response_model=MailCampaign)
async def get_campaign(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    try:
        return await service.get_campaign(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/campaigns/{mail_campaign_id}", response_model=MailCampaign)
async def update_campaign(
    mail_campaign_id: str,
    request: Request,
    patch: dict[str, Any] = Body(...),
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    try:
        return await service.update_campaign(mail_campaign_id, patch, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailCampaignLegacyScheduleLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=400, detail=f"Selected CRM List not found: {e}")
    except MailScheduleValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/ready", response_model=MailCampaign)
async def mark_campaign_ready(
    mail_campaign_id: str,
    request: Request,
    campaign_service: MailCampaignService = Depends(get_mail_campaign_service),
    suppression_service: MailSuppressionService = Depends(get_mail_suppression_service),
):
    """Validates completeness and snapshots the audience into MailEnrollment
    rows -- see MailCampaignService.mark_ready()'s docstring for exactly
    what's checked and why this is the one place snapshotting happens."""
    try:
        suppressed = await suppression_service.list_active_suppressed_emails()
        return await campaign_service.mark_ready(mail_campaign_id, suppressed, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailCampaignNotReadyError as e:
        raise HTTPException(status_code=422, detail="; ".join(e.reasons))


@router.post("/campaigns/{mail_campaign_id}/unlock", response_model=MailCampaign)
async def unlock_campaign(
    mail_campaign_id: str, request: Request, service: MailCampaignService = Depends(get_mail_campaign_service)
):
    """READY -> DRAFT. Deletes the (now-stale) enrollment snapshot -- see
    MailCampaignService.unlock_campaign()."""
    try:
        return await service.unlock_campaign(mail_campaign_id, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/archive", response_model=MailCampaign)
async def archive_campaign(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    try:
        return await service.archive_campaign(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/activate", response_model=MailCampaign)
async def activate_campaign(
    mail_campaign_id: str, request: Request, service: MailCampaignService = Depends(get_mail_campaign_service)
):
    """READY -> ACTIVE. Backend only in this phase -- deliberately not
    exposed anywhere in the production frontend yet (see
    MailCampaignService.activate_campaign()'s docstring); nothing calls
    this route today except tests (and, since Phase 2, the admin/service
    OPERATOR token -- see app/session_auth_middleware.py's own docstring
    for why activation and actual provider sending are two independent
    safety gates: this route can only ever flip a campaign's STATUS,
    never touch mail_sending_engine_enabled or the allowlists below). 422
    lists every readiness problem found (same shape as POST .../ready) if
    the campaign's execution-critical state has drifted since it became
    READY. 503 if mail_sending_engine_enabled is False (the default) --
    see that setting's docstring in app/config.py; same convention as
    AuthNotConfiguredError/EmailIntakeWebhook's "unconfigured/disabled ->
    503" pattern elsewhere in this API."""
    try:
        return await service.activate_campaign(mail_campaign_id, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailSendingEngineDisabledError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except MailCampaignNotReadyError as e:
        raise HTTPException(status_code=422, detail="; ".join(e.reasons))


@router.post("/campaigns/{mail_campaign_id}/pause", response_model=MailCampaign)
async def pause_campaign(
    mail_campaign_id: str, request: Request, service: MailCampaignService = Depends(get_mail_campaign_service)
):
    """ACTIVE -> PAUSED. Backend only -- see activate_campaign()'s note
    above. Also reachable by the admin/service OPERATOR token (since
    Phase 2, 2026-09-03) -- the safer inverse of Activate: it can only
    ever stop new claims on an already-ACTIVE campaign, never resume one
    or touch anything else -- see app/session_auth_middleware.py's own
    docstring."""
    try:
        return await service.pause_campaign(mail_campaign_id, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/resume", response_model=MailCampaign)
async def resume_campaign(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """PAUSED -> ACTIVE. Backend only -- see activate_campaign()'s note
    above. 422 if this campaign's selected mailboxes are no longer
    CONNECTED (see MailCampaignService.resume_campaign()'s docstring). 503
    if mail_sending_engine_enabled is False -- same gate as activate()."""
    try:
        return await service.resume_campaign(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailSendingEngineDisabledError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except MailCampaignNotReadyError as e:
        raise HTTPException(status_code=422, detail="; ".join(e.reasons))


@router.get("/campaigns/{mail_campaign_id}/review", response_model=MailCampaignReview)
async def get_campaign_review(
    mail_campaign_id: str,
    campaign_service: MailCampaignService = Depends(get_mail_campaign_service),
    suppression_service: MailSuppressionService = Depends(get_mail_suppression_service),
):
    """Pure, read-only -- see MailCampaignService.get_review()'s docstring
    for the zero-mutation guarantee. Callable at any campaign status."""
    try:
        suppressed = await suppression_service.list_active_suppressed_emails()
        return await campaign_service.get_review(mail_campaign_id, suppressed)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/campaigns/{mail_campaign_id}/enrollments", response_model=list[MailEnrollment])
async def list_enrollments(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    try:
        return await service.list_enrollments(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Workload / prospect batches (Phase 2, 2026-09-03) ---------------------


@router.get("/campaigns/{mail_campaign_id}/workload", response_model=MailCampaignWorkload)
async def get_campaign_workload(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """Enrollment-status counts, independent of the campaign's own
    lifecycle status -- see MailCampaignWorkload's own docstring for why
    this is never inferred from `status` (an ACTIVE campaign with zero
    pending/in-progress enrollments is still ACTIVE, not "done")."""
    try:
        return await service.get_workload(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/campaigns/{mail_campaign_id}/batches", response_model=list[MailEnrollmentBatch])
async def list_campaign_batches(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """Every prospect batch ever added to this campaign, newest first --
    see MailEnrollmentBatch's own docstring. Empty for every campaign that
    predates add_prospects(), or that simply has never had a batch added
    -- not an error, not a gap."""
    try:
        return await service.list_batches(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


class MailAddProspectsRequest(BaseModel):
    """`source` is a `Literal["crm_list"]` (not the broader
    MailEnrollmentBatchSource enum) deliberately -- Stage 3 only
    implements the CRM-List path; a request naming "csv_upload" is
    rejected by Pydantic itself with a 422, before this route's own body
    ever runs, rather than reaching MailCampaignService.add_prospects()'s
    own NotImplementedError. `idempotency_key` is required, not optional
    -- see add_prospects()'s own docstring for why this is the entire
    mechanism preventing an HTTP retry from creating a duplicate batch."""

    source: Literal["crm_list"]
    source_list_id: str
    idempotency_key: str


@router.post("/campaigns/{mail_campaign_id}/prospects", response_model=MailEnrollmentBatch)
async def add_prospects(
    mail_campaign_id: str,
    payload: MailAddProspectsRequest,
    request: Request,
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """Add a batch of prospects to an already-persistent campaign -- see
    MailCampaignService.add_prospects()'s own docstring for the full
    idempotency/freeze/reconciliation contract. A retry with the same
    idempotency_key always resolves to the same batch, never creates a
    second one."""
    try:
        return await service.add_prospects(
            mail_campaign_id,
            source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=payload.idempotency_key,
            source_list_id=payload.source_list_id,
            actor=_operator_actor(request),
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEligibleForProspectsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=400, detail=f"Selected CRM List not found: {e}")


class MailAddProspectsFromCsvRequest(BaseModel):
    """`decisions` uses the EXACT same shape as CrmImportCommitRequest
    (app/api/crm.py) -- row_index (as a string, JSON object keys) ->
    "create"|"update"|"skip" -- so the frontend can build it identically
    to how it already builds a decisions payload for the standalone
    CRM Import review step, no new format to learn. `import_batch_id`
    must already be COMMITTED, in progress (COMMITTING), or ready to
    commit (MAPPED) -- it's expected to be the id returned by the
    existing, unmodified POST /crm/import/upload -> POST /crm/import/{id}/preview
    steps the frontend already calls directly (see MailCampaignCsvProspectService's
    own module docstring for the full flow); this route never uploads or
    parses a CSV itself, and never will -- see this file's own module
    docstring for why no second CSV upload/parser API exists here."""

    import_batch_id: str
    idempotency_key: str
    decisions: dict[str, str] = {}


@router.post("/campaigns/{mail_campaign_id}/prospects/csv", response_model=MailEnrollmentBatch)
async def add_prospects_from_csv(
    mail_campaign_id: str,
    payload: MailAddProspectsFromCsvRequest,
    request: Request,
    service: MailCampaignCsvProspectService = Depends(get_mail_campaign_csv_prospect_service),
):
    """The one new route Stage 4B adds (2026-09-03) -- CSV-driven Add
    Prospects, orchestrating CrmImportService.commit() +
    MailCampaignService.add_prospects() behind one durable idempotency
    link. See MailCampaignCsvProspectService's own module docstring for
    the full ordering, the "existing link always wins" guarantee, and the
    documented eligibility race.

    DELIBERATELY NOT reachable by either admin/service token -- this is a
    human-Hub-session-only route for Stage 4B V1 (see
    app/session_auth_middleware.py's module docstring): no entry exists
    for it in _SERVICE_OPERATOR_RULES (denied by omission, the same
    allowlist mechanism protecting every other unlisted route), and it is
    a genuinely distinct path from the existing, unmodified
    POST .../prospects (CRM-List) route -- not a shared path with a
    branching body, specifically so the operator token's existing grant
    on that route can never be reinterpreted as covering this one."""
    try:
        decisions = {int(row_index): decision for row_index, decision in payload.decisions.items()}
        return await service.add_prospects_from_csv(
            mail_campaign_id,
            idempotency_key=payload.idempotency_key,
            import_batch_id=payload.import_batch_id,
            decisions=decisions,
            actor=_operator_actor(request),
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEligibleForProspectsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except CrmImportBatchNotFound as e:
        raise HTTPException(status_code=404, detail=f"CSV import batch not found: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Channels (selected sending mailboxes) --------------------------------


class MailCampaignChannelsUpdateRequest(BaseModel):
    mailbox_ids: list[str]


@router.get("/campaigns/{mail_campaign_id}/channels", response_model=list[str])
async def list_campaign_channels(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """The campaign's currently-selected mailbox ids -- the frontend already
    has full mailbox details from GET /mailboxes, so this deliberately
    returns just ids rather than duplicating that data."""
    try:
        mailboxes = await service.list_channel_mailboxes(mail_campaign_id)
        return [m.mailbox_id for m in mailboxes]
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/campaigns/{mail_campaign_id}/channels", response_model=list[Mailbox])
async def set_campaign_channels(
    mail_campaign_id: str,
    payload: MailCampaignChannelsUpdateRequest,
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """Atomically replaces the campaign's full selected-mailbox set -- see
    MailCampaignService.set_channel_mailboxes()'s docstring for exactly what
    is/isn't allowed (a disconnected/needs_reauth mailbox may remain
    selected if it already was, but may not be newly added; an ARCHIVED
    campaign rejects this call entirely -- its Channels are read-only)."""
    try:
        return await service.set_channel_mailboxes(mail_campaign_id, payload.mailbox_ids)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignChannelsFrozenError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailboxChannelNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MailboxChannelNotUsableError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Schedule (real send windows, legacy-compatible) -----------------------


class MailScheduleWindowRequest(BaseModel):
    # Optional: omit/null for a genuinely new window; pass an EXISTING
    # window's real id to preserve its identity across an edit (e.g. a
    # drag-to-reschedule) rather than deleting and recreating it -- see
    # MailSendWindow's docstring. An id that isn't one of this campaign's
    # current windows, or that's repeated in the same request, is rejected.
    window_id: str | None = None
    day_of_week: int
    start_time: str  # "HH:MM"
    end_time: str


class MailCampaignScheduleUpdateRequest(BaseModel):
    timezone: str
    windows: list[MailScheduleWindowRequest]


@router.get("/campaigns/{mail_campaign_id}/schedule", response_model=MailCampaignSchedule)
async def get_campaign_schedule(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """Read-only at any campaign status -- see
    MailCampaignService.get_schedule()'s docstring. Returns real
    MailSendWindow rows once any exist for this campaign, otherwise the
    equivalent windows synthesized on the fly from the campaign's legacy
    sending_days/start_time/end_time/all_hours fields (never persisted just
    by reading them) -- see MailCampaignService._resolve_schedule()."""
    try:
        return await service.get_schedule(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/campaigns/{mail_campaign_id}/schedule", response_model=MailCampaignSchedule)
async def set_campaign_schedule(
    mail_campaign_id: str,
    payload: MailCampaignScheduleUpdateRequest,
    request: Request,
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """Atomically replaces the campaign's full schedule (timezone + every
    send window) -- DRAFT-only (409 on READY/ARCHIVED, matching every other
    schedule/audience/sequence mutation; unlock the campaign first). See
    MailCampaignService.set_schedule()'s docstring for the full validation
    (day range, start<end, no same-weekday overlap, window_id ownership/
    uniqueness) and for exactly how this is the one point a campaign's
    schedule permanently switches from "legacy" to real "windows"."""
    try:
        return await service.set_schedule(
            mail_campaign_id,
            payload.timezone,
            [(w.window_id, w.day_of_week, w.start_time, w.end_time) for w in payload.windows],
            actor=_operator_actor(request),
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailScheduleValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Lead-start triggers (Stage 5D, 2026-09-04) -----------------------------
#
# CRUD only here -- occurrence discovery/freeze/reconciliation happens
# entirely inside MailExecutionWorker.tick() (see MailTriggerService), never
# from an HTTP request. Editable anywhere campaign configuration is
# meaningfully editable (DRAFT/READY/ACTIVE/PAUSED, matching Channels'
# posture more than Schedule's DRAFT-only lock) -- see MailTriggerService's
# own _TRIGGER_CONFIGURABLE_STATUSES for the exact set and why ARCHIVED/
# COMPLETED are excluded. Creating/editing/deleting a trigger NEVER starts a
# lead -- see MailTriggerService.create_trigger()'s own docstring.


class MailLeadStartTriggerCreateRequest(BaseModel):
    weekdays: list[int]
    local_time: str  # "HH:MM", same convention as every other time-of-day input in this API
    leads_to_start: int
    enabled: bool = True


class MailLeadStartTriggerUpdateRequest(BaseModel):
    weekdays: list[int] | None = None
    local_time: str | None = None
    leads_to_start: int | None = None
    enabled: bool | None = None


@router.get("/campaigns/{mail_campaign_id}/triggers", response_model=list[MailLeadStartTrigger])
async def list_triggers(mail_campaign_id: str, service: MailTriggerService = Depends(get_mail_trigger_service)):
    try:
        return await service.list_triggers(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/triggers", response_model=MailLeadStartTrigger)
async def create_trigger(
    mail_campaign_id: str,
    payload: MailLeadStartTriggerCreateRequest,
    request: Request,
    service: MailTriggerService = Depends(get_mail_trigger_service),
):
    try:
        return await service.create_trigger(
            mail_campaign_id, payload.weekdays, payload.local_time, payload.leads_to_start, payload.enabled,
            actor=_operator_actor(request),
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEligibleForTriggersError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/campaigns/{mail_campaign_id}/triggers/{trigger_id}", response_model=MailLeadStartTrigger)
async def update_trigger(
    mail_campaign_id: str,
    trigger_id: str,
    payload: MailLeadStartTriggerUpdateRequest,
    request: Request,
    service: MailTriggerService = Depends(get_mail_trigger_service),
):
    try:
        return await service.update_trigger(
            mail_campaign_id, trigger_id, payload.weekdays, payload.local_time, payload.leads_to_start,
            payload.enabled, actor=_operator_actor(request),
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailLeadStartTriggerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEligibleForTriggersError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/campaigns/{mail_campaign_id}/triggers/{trigger_id}", status_code=204)
async def delete_trigger(
    mail_campaign_id: str, trigger_id: str, request: Request, service: MailTriggerService = Depends(get_mail_trigger_service)
):
    try:
        await service.delete_trigger(mail_campaign_id, trigger_id, actor=_operator_actor(request))
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailLeadStartTriggerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEligibleForTriggersError as e:
        raise HTTPException(status_code=409, detail=str(e))


# --- Sequence steps --------------------------------------------------------


class MailSequenceStepCreateRequest(BaseModel):
    subject: str
    body: str
    delay_days: int = 0
    reply_in_thread: bool = True


class MailSequenceStepReorderRequest(BaseModel):
    step_ids: list[str]


@router.get("/campaigns/{mail_campaign_id}/steps", response_model=list[MailSequenceStep])
async def list_steps(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    try:
        return await service.list_steps(mail_campaign_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/steps", response_model=MailSequenceStep)
async def add_step(
    mail_campaign_id: str,
    payload: MailSequenceStepCreateRequest,
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    try:
        return await service.add_step(
            mail_campaign_id, payload.subject, payload.body, payload.delay_days, payload.reply_in_thread
        )
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidMailTemplateVariableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InvalidMailSequenceStepDelayError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/campaigns/{mail_campaign_id}/steps/{step_id}", response_model=MailSequenceStep)
async def update_step(
    mail_campaign_id: str,
    step_id: str,
    patch: dict[str, Any] = Body(...),
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    try:
        return await service.update_step(mail_campaign_id, step_id, patch)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailSequenceStepNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidMailTemplateVariableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InvalidMailSequenceStepDelayError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/campaigns/{mail_campaign_id}/steps/{step_id}", response_model=list[MailSequenceStep])
async def delete_step(
    mail_campaign_id: str, step_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)
):
    try:
        return await service.delete_step(mail_campaign_id, step_id)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailSequenceStepNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/campaigns/{mail_campaign_id}/steps/reorder", response_model=list[MailSequenceStep])
async def reorder_steps(
    mail_campaign_id: str,
    payload: MailSequenceStepReorderRequest,
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    try:
        return await service.reorder_steps(mail_campaign_id, payload.step_ids)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Suppression ------------------------------------------------------------


class MailSuppressRequest(BaseModel):
    email: str
    reason: MailSuppressionReason = MailSuppressionReason.MANUAL
    notes: str | None = None


class MailUnsuppressRequest(BaseModel):
    email: str


@router.get("/suppressions", response_model=list[MailSuppression])
async def list_suppressions(service: MailSuppressionService = Depends(get_mail_suppression_service)):
    return await service.list_all()


@router.post("/suppressions", response_model=MailSuppression)
async def suppress_email(
    payload: MailSuppressRequest, service: MailSuppressionService = Depends(get_mail_suppression_service)
):
    try:
        return await service.suppress(payload.email, payload.reason, payload.notes)
    except InvalidMailSuppressionEmailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/suppressions/unsuppress", response_model=MailSuppression)
async def unsuppress_email(
    payload: MailUnsuppressRequest, service: MailSuppressionService = Depends(get_mail_suppression_service)
):
    try:
        return await service.unsuppress(payload.email)
    except InvalidMailSuppressionEmailError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MailSuppressionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except UnsubscribeReversalNotAllowedError as e:
        # 409: the request is well-formed and the resource exists, but
        # the current state (an explicit recipient unsubscribe) forbids
        # this specific transition -- see MailSuppressionService.
        # unsuppress()'s own docstring. No administrative override route
        # exists yet, deliberately (Phase B3 decision).
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/suppressions/{email}", response_model=MailContactSuppressionStatus)
async def get_suppression_status(email: str, service: MailSuppressionService = Depends(get_mail_suppression_service)):
    """Never 404s -- always returns a status object, `suppressed=False` for
    an address that's never been suppressed. This lets the CRM contact page
    call it unconditionally for any contact's email."""
    return await service.get_status(email)


# --- Execution: UNKNOWN reconciliation (Phase C, minimum backend capability) --
#
# Session-authenticated like every other route in this file (this router
# carries no PUBLIC_PATHS exemption -- see app/session_auth_middleware.py) --
# deliberately NOT public. No polished frontend exists yet for these; they
# exist so manual testing/reconciliation is possible via curl/Swagger UI
# while a real UI is a later decision.


class ResolveUnknownSentRequest(BaseModel):
    provider_message_id: str
    provider_thread_id: str


@router.post("/execution/{enrollment_step_id}/resolve-sent")
async def resolve_unknown_step_sent(
    enrollment_step_id: str,
    payload: ResolveUnknownSentRequest,
    sending_service: MailSendingService = Depends(get_mail_sending_service),
    campaign_service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """A human has independently verified (via Gmail directly) that an
    UNKNOWN row WAS actually delivered. Only ever permitted FROM UNKNOWN --
    see resolve_unknown_step_confirmed_sent()'s own docstring. Requires the
    real provider identifiers the human found, so the row's history stays
    accurate (never synthesized)."""
    step = await sending_service.step_store.get(enrollment_step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="MailEnrollmentStep not found.")
    try:
        schedule = await campaign_service.get_schedule(step.mail_campaign_id)
        sequence_steps = await campaign_service.list_steps(step.mail_campaign_id)
        applied = await sending_service.resolve_unknown_step_confirmed_sent(
            enrollment_step_id,
            provider_message_id=payload.provider_message_id,
            provider_thread_id=payload.provider_thread_id,
            sequence_steps=sequence_steps,
            windows=schedule.windows,
            timezone_name=schedule.timezone or "UTC",
            now=datetime.now(timezone.utc),
        )
    except UnknownStepNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except UnknownStepWrongStatusError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"applied": applied}


@router.post("/execution/{enrollment_step_id}/resolve-prepare-blocked")
async def resolve_prepare_blocked(
    enrollment_step_id: str,
    sending_service: MailSendingService = Depends(get_mail_sending_service),
):
    """An operator has independently confirmed the underlying issue is
    fixed and explicitly resumes a lead whose enrollment is PAUSED
    because message preparation exhausted its bounded transient-retry
    budget, or failed for an unrecognized reason -- see
    resolve_prepare_blocked_step()'s own docstring for exactly why these
    two specific pause reasons have no automatic recovery path at all.
    Only ever permitted from those two reasons -- a config-blocked or
    mailbox-blocked enrollment is untouched by this route."""
    try:
        applied = await sending_service.resolve_prepare_blocked_step(enrollment_step_id, now=datetime.now(timezone.utc))
    except UnknownStepNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PrepareBlockedWrongStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"applied": applied}


@router.post("/execution/{enrollment_step_id}/resolve-not-sent")
async def resolve_unknown_step_not_sent(
    enrollment_step_id: str,
    sending_service: MailSendingService = Depends(get_mail_sending_service),
):
    """A human has independently verified that an UNKNOWN row was NOT
    delivered. Requeues it, preserving its exact persisted rfc_message_id
    -- see resolve_unknown_step_confirmed_not_sent()'s own docstring."""
    try:
        applied = await sending_service.resolve_unknown_step_confirmed_not_sent(
            enrollment_step_id, now=datetime.now(timezone.utc)
        )
    except UnknownStepNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except UnknownStepWrongStatusError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"applied": applied}
