"""
Generic synchronization routes -- deliberately its own router/namespace,
not nested under /campaign, so it reads as "the sync engine" rather than
"a campaign sub-action." Today this only exposes campaign sync; future
sync operations (leads, messages, other providers) belong here too
rather than each inventing their own ad hoc trigger path.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.dependencies import get_campaign_sync_service, get_itf_ingestion_service, verify_itf_webhook_token
from app.models.campaign_sync import CampaignSyncReport
from app.models.itf import ItfWebhookRequest, ItfWebhookResult
from app.services.campaign_sync_service import CampaignSyncService
from app.services.itf_ingestion_service import ItfIngestionService

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/campaigns", response_model=CampaignSyncReport)
async def sync_campaigns(service: CampaignSyncService = Depends(get_campaign_sync_service)):
    """
    Discovers new Apollo sequences (creating a Campaign for each), updates
    already-synced campaigns from Apollo's current data, and archives any
    that disappeared from Apollo since the last run. Never called
    automatically on a schedule -- the frontend triggers this on page
    load; a real background scheduler is a separate, later decision.
    """
    try:
        return await service.sync()
    except Exception as e:
        logger.error(f"Campaign sync failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/itf-contact", response_model=ItfWebhookResult)
async def sync_itf_contact(
    payload: ItfWebhookRequest,
    dry_run: bool = False,
    _auth: None = Depends(verify_itf_webhook_token),
    service: ItfIngestionService = Depends(get_itf_ingestion_service),
):
    """
    Webhook target for the Google Apps Script bridge bound to the ITF
    response Sheet (its installable onFormSubmit trigger POSTs here once per
    real Form submission) -- turns exactly one submission into a call to
    CrmImportService.import_one_row(), the same pipeline CSV import uses.
    See itf_ingestion_service.py's module docstring for the header-
    disambiguation/idempotency details.

    Auth (verify_itf_webhook_token) runs first and rejects a missing/invalid
    token before this body -- which touches the CRM and the ingestion
    ledger -- ever executes.

    `dry_run` (query param, defaults to False): classifies and reports what
    WOULD happen -- status, matched contact, mapped/classified fields,
    warnings -- without writing to the CRM, the ingestion log, or any custom
    field definition. Use this to inspect real submissions before enabling
    the production Apps Script trigger; it is safe to call repeatedly and
    never advances the idempotency ledger.

    Response `status` is one of: created | updated | possible_duplicate |
    already_processed | error -- the same vocabulary CrmImportRowStatus
    already uses elsewhere, just spelled out for a caller that isn't Python.
    """
    try:
        return await service.process_submission(
            headers=payload.headers,
            values=payload.values,
            row_number=payload.row_number,
            response_id=payload.response_id,
            dry_run=dry_run,
        )
    except Exception as e:
        logger.error(f"ITF webhook processing failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error processing ITF submission.")
