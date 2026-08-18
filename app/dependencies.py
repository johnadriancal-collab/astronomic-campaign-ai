"""
Shared FastAPI dependencies.

CampaignService/LeadService are constructed once at app startup (see
main.py's lifespan handler) and stored on app.state -- these dependencies
just read them back out, so route modules don't each need their own
import-time singleton.
"""

import hmac

from fastapi import Header, HTTPException, Request

from app.config import settings
from app.services.activity_log_service import ActivityLogService
from app.services.campaign_service import CampaignService
from app.services.campaign_sync_service import CampaignSyncService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.email_intake_service import EmailIntakeService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.itf_ingestion_service import ItfIngestionService
from app.services.lead_service import LeadService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_suppression_service import MailSuppressionService


async def get_campaign_service(request: Request) -> CampaignService:
    return request.app.state.campaign_service


async def get_campaign_sync_service(request: Request) -> CampaignSyncService:
    return request.app.state.campaign_sync_service


async def get_lead_service(request: Request) -> LeadService:
    return request.app.state.lead_service


async def get_email_sequence_sync_service(request: Request) -> EmailSequenceSyncService:
    return request.app.state.email_sequence_sync_service


async def get_email_message_sync_service(request: Request) -> EmailMessageSyncService:
    return request.app.state.email_message_sync_service


async def get_crm_service(request: Request) -> CrmService:
    return request.app.state.crm_service


async def get_crm_import_service(request: Request) -> CrmImportService:
    return request.app.state.crm_import_service


async def get_itf_ingestion_service(request: Request) -> ItfIngestionService:
    return request.app.state.itf_ingestion_service


async def get_activity_log_service(request: Request) -> ActivityLogService:
    return request.app.state.activity_log_service


async def get_email_intake_service(request: Request) -> EmailIntakeService:
    return request.app.state.email_intake_service


async def get_mail_campaign_service(request: Request) -> MailCampaignService:
    return request.app.state.mail_campaign_service


async def get_mail_suppression_service(request: Request) -> MailSuppressionService:
    return request.app.state.mail_suppression_service


async def verify_email_intake_webhook_token(authorization: str | None = Header(default=None)) -> None:
    """Same shared-secret bearer-token check as verify_itf_webhook_token
    below -- 503 when EMAIL_INTAKE_WEBHOOK_TOKEN itself isn't configured
    (an operator/deployment gap), 401 for a missing/invalid token. Never
    logs the token and never echoes it back in an error detail."""
    if not settings.email_intake_webhook_token:
        raise HTTPException(status_code=503, detail="Email intake webhook is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.email_intake_webhook_token):
        raise HTTPException(status_code=401, detail="Invalid token.")


async def verify_itf_webhook_token(authorization: str | None = Header(default=None)) -> None:
    """
    Shared-secret bearer-token check for POST /sync/itf-contact. Runs as a
    route dependency, so a missing/invalid token is rejected before the
    route body -- which touches the CRM and the ingestion ledger -- ever
    runs. Never logs the token and never echoes it back in an error detail.

    503 (not 401) when ITF_WEBHOOK_TOKEN itself isn't configured -- that's
    an operator/deployment gap, not a caller authentication failure, and
    the two should be distinguishable in Apps Script's logs.
    """
    if not settings.itf_webhook_token:
        raise HTTPException(status_code=503, detail="ITF webhook is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.itf_webhook_token):
        raise HTTPException(status_code=401, detail="Invalid token.")
