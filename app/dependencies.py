"""
Shared FastAPI dependencies.

CampaignService/LeadService are constructed once at app startup (see
main.py's lifespan handler) and stored on app.state -- these dependencies
just read them back out, so route modules don't each need their own
import-time singleton.
"""

from fastapi import Request

from app.services.campaign_service import CampaignService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.lead_service import LeadService


async def get_campaign_service(request: Request) -> CampaignService:
    return request.app.state.campaign_service


async def get_lead_service(request: Request) -> LeadService:
    return request.app.state.lead_service


async def get_email_sequence_sync_service(request: Request) -> EmailSequenceSyncService:
    return request.app.state.email_sequence_sync_service


async def get_email_message_sync_service(request: Request) -> EmailMessageSyncService:
    return request.app.state.email_message_sync_service
