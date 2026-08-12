"""
Email -> CRM Intake routes (Phase 1).

Two routers, deliberately: `sync_router` (prefix /sync) holds the webhook
target, grouped with POST /sync/itf-contact under the same "sync engine"
namespace (see app/api/sync.py's own module docstring) since a future
Apps Script bridge is exactly analogous to the ITF one. `crm_router`
(prefix /crm/email-intake) holds the human review/approval surface,
alongside the rest of the CRM's own routes.

Every route here is either read-only or explicitly reviewer-initiated
(manual match, approve, reject) -- nothing in this file calls
CrmService.update_contact() except the one line inside
EmailIntakeService.approve() itself, which this module never bypasses.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.dependencies import get_email_intake_service, verify_email_intake_webhook_token
from app.models.email_intake import (
    ApproveEmailIntakeRequest,
    ApproveEmailIntakeResult,
    EmailIntakeItem,
    EmailIntakeItemPage,
    EmailIntakeStatus,
    EmailIntakeWebhookRequest,
    EmailIntakeWebhookResult,
    ManualMatchRequest,
)
from app.services.email_intake_service import (
    EmailIntakeInvalidStateError,
    EmailIntakeItemNotFound,
    EmailIntakeService,
)
from app.services.crm_service import CrmContactNotFound

sync_router = APIRouter(prefix="/sync", tags=["email-intake"])
crm_router = APIRouter(prefix="/crm/email-intake", tags=["email-intake"])


@sync_router.post("/email-intake", response_model=EmailIntakeWebhookResult)
async def ingest_email(
    payload: EmailIntakeWebhookRequest,
    _auth: None = Depends(verify_email_intake_webhook_token),
    service: EmailIntakeService = Depends(get_email_intake_service),
):
    """
    Webhook target for a (Phase 2, not yet activated) Apps Script bridge --
    turns exactly one email into exactly one EmailIntakeItem. Idempotent on
    gmail_message_id: a retried/duplicate call for the same id returns the
    existing item's current status (already_processed=True) rather than
    creating a second item or re-running extraction.

    NEVER writes to the CRM -- at most, this creates a pending proposal or
    a needs-match item. See EmailIntakeService.ingest()'s docstring.
    """
    try:
        return await service.ingest(payload)
    except Exception as e:
        logger.error(f"Email intake webhook processing failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error processing email intake payload.")


@crm_router.get("", response_model=EmailIntakeItemPage)
async def list_email_intake_items(
    status: EmailIntakeStatus | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 25,
    service: EmailIntakeService = Depends(get_email_intake_service),
):
    """Newest first. `status` and `q` (matches sender/subject/matched
    contact name) filter in the service layer, same convention as
    GET /crm/activity."""
    items = await service.list_items(status=status, q=q)
    total = len(items)
    page = max(page, 1)
    page_size = max(page_size, 1)
    start = (page - 1) * page_size
    return EmailIntakeItemPage(items=items[start : start + page_size], total=total, page=page, page_size=page_size)


@crm_router.get("/{intake_id}", response_model=EmailIntakeItem)
async def get_email_intake_item(intake_id: str, service: EmailIntakeService = Depends(get_email_intake_service)):
    try:
        return await service.get_item(intake_id)
    except EmailIntakeItemNotFound:
        raise HTTPException(status_code=404, detail="Email intake item not found.")


@crm_router.post("/{intake_id}/match", response_model=EmailIntakeItem)
async def manual_match_email_intake_item(
    intake_id: str,
    payload: ManualMatchRequest,
    service: EmailIntakeService = Depends(get_email_intake_service),
):
    """Reviewer-selected contact for a NEEDS_MATCH item. Generates the
    proposal against the chosen contact and moves the item to
    PENDING_REVIEW. Never creates a new contact."""
    try:
        return await service.manual_match(intake_id, payload.crm_contact_id)
    except EmailIntakeItemNotFound:
        raise HTTPException(status_code=404, detail="Email intake item not found.")
    except CrmContactNotFound:
        raise HTTPException(status_code=404, detail="CRM contact not found.")
    except EmailIntakeInvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


@crm_router.post("/{intake_id}/approve", response_model=ApproveEmailIntakeResult)
async def approve_email_intake_item(
    intake_id: str,
    payload: ApproveEmailIntakeRequest,
    service: EmailIntakeService = Depends(get_email_intake_service),
):
    """
    Applies ONLY the checked (`field_keys`) proposed changes, through
    CrmService.update_contact() -- the normal manual-edit path, not the
    CSV/ITF merge rule. If any requested field drifted from what this
    proposal originally reviewed, nothing is written and the response's
    `status` is "stale" with the specific conflicts -- see
    EmailIntakeService.approve()'s docstring.
    """
    try:
        return await service.approve(intake_id, payload.field_keys)
    except EmailIntakeItemNotFound:
        raise HTTPException(status_code=404, detail="Email intake item not found.")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except EmailIntakeInvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


@crm_router.post("/{intake_id}/reject", response_model=EmailIntakeItem)
async def reject_email_intake_item(intake_id: str, service: EmailIntakeService = Depends(get_email_intake_service)):
    """Marks the item Rejected. Never touches the CRM. The email/proposal
    is retained permanently -- no delete route exists in Phase 1."""
    try:
        return await service.reject(intake_id)
    except EmailIntakeItemNotFound:
        raise HTTPException(status_code=404, detail="Email intake item not found.")
    except EmailIntakeInvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
