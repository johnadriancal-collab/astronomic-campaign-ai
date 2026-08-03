"""
Campaign Manager's Leads routes -- read-only. Leads themselves are only
ever created inside CampaignService.build() (see that file and
app/services/lead_service.py); nothing here creates, edits, or
transitions a Lead's status.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_lead_service
from app.models.lead import LeadDetail, LeadListItem
from app.services.lead_service import LeadService

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("", response_model=list[LeadListItem])
async def list_leads(service: LeadService = Depends(get_lead_service)):
    """Every stored lead, newest first, with how many campaigns each belongs to."""
    return await service.list_with_campaign_counts()


@router.get("/{lead_id}", response_model=LeadDetail)
async def get_lead(lead_id: str, service: LeadService = Depends(get_lead_service)):
    """A lead's full stored detail, plus the campaigns it belongs to."""
    detail = await service.get_detail(lead_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Lead not found: {lead_id}")
    return detail
