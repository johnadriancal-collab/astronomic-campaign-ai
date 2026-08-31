"""
Astronomic Mail routes -- Phase 1 (Foundation) only.

IMPORTANT, and load-bearing for this phase's safety guarantee: there is NO
route here (and none anywhere else in this app) capable of activating a
campaign into a sending state, dispatching a Gmail/SMTP call, or starting
any background job. The only state transitions exposed are
draft->ready, ready->draft (unlock), and (draft|ready)->archived -- see
MailCampaignService's module docstring for the full state machine. This is
not a UI-level restriction: no service method, and therefore no route,
exists that could reach an active-sending state even if called directly.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_mail_campaign_service, get_mail_suppression_service
from app.models.mail import (
    MailCampaign,
    MailCampaignReview,
    MailCampaignSharing,
    MailContactSuppressionStatus,
    MailEnrollment,
    MailScheduleValidationError,
    MailSequenceStep,
    MailSuppression,
    MailSuppressionReason,
)
from app.models.mailbox import Mailbox
from app.services.crm_service import CrmContactListNotFound
from app.services.mail_campaign_service import (
    InvalidMailTemplateVariableError,
    MailboxChannelNotFoundError,
    MailboxChannelNotUsableError,
    MailCampaignChannelsFrozenError,
    MailCampaignInvalidTransitionError,
    MailCampaignNotEditableError,
    MailCampaignNotFound,
    MailCampaignNotReadyError,
    MailCampaignService,
    MailSequenceStepNotFound,
)
from app.services.mail_suppression_service import (
    InvalidMailSuppressionEmailError,
    MailSuppressionNotFoundError,
    MailSuppressionService,
)

router = APIRouter(prefix="/mail", tags=["mail"])


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
    payload: MailCampaignCreateRequest, service: MailCampaignService = Depends(get_mail_campaign_service)
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
    campaign = await service.create_campaign(payload.name)
    patch = payload.model_dump(exclude={"name"}, exclude_none=True)
    if not patch:
        return campaign
    try:
        return await service.update_campaign(campaign.mail_campaign_id, patch)
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
    patch: dict[str, Any] = Body(...),
    service: MailCampaignService = Depends(get_mail_campaign_service),
):
    try:
        return await service.update_campaign(mail_campaign_id, patch)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignNotEditableError as e:
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
    campaign_service: MailCampaignService = Depends(get_mail_campaign_service),
    suppression_service: MailSuppressionService = Depends(get_mail_suppression_service),
):
    """Validates completeness and snapshots the audience into MailEnrollment
    rows -- see MailCampaignService.mark_ready()'s docstring for exactly
    what's checked and why this is the one place snapshotting happens."""
    try:
        suppressed = await suppression_service.list_active_suppressed_emails()
        return await campaign_service.mark_ready(mail_campaign_id, suppressed)
    except MailCampaignNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailCampaignInvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except MailCampaignNotReadyError as e:
        raise HTTPException(status_code=422, detail="; ".join(e.reasons))


@router.post("/campaigns/{mail_campaign_id}/unlock", response_model=MailCampaign)
async def unlock_campaign(mail_campaign_id: str, service: MailCampaignService = Depends(get_mail_campaign_service)):
    """READY -> DRAFT. Deletes the (now-stale) enrollment snapshot -- see
    MailCampaignService.unlock_campaign()."""
    try:
        return await service.unlock_campaign(mail_campaign_id)
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


@router.get("/suppressions/{email}", response_model=MailContactSuppressionStatus)
async def get_suppression_status(email: str, service: MailSuppressionService = Depends(get_mail_suppression_service)):
    """Never 404s -- always returns a status object, `suppressed=False` for
    an address that's never been suppressed. This lets the CRM contact page
    call it unconditionally for any contact's email."""
    return await service.get_status(email)
