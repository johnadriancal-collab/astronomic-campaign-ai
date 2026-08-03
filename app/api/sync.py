"""
Generic synchronization routes -- deliberately its own router/namespace,
not nested under /campaign, so it reads as "the sync engine" rather than
"a campaign sub-action." Today this only exposes campaign sync; future
sync operations (leads, messages, other providers) belong here too
rather than each inventing their own ad hoc trigger path.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.dependencies import get_campaign_sync_service
from app.models.campaign_sync import CampaignSyncReport
from app.services.campaign_sync_service import CampaignSyncService

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
