"""
Campaign Manager Integration Phase -- the ONE new backend surface for
"Option D" (read-side aggregation, approved architecture).

Product direction: Apollo Campaign/Sequence integration is disabled in the
Campaign Manager surface for now, so this router returns Astronomic Mail
campaigns ONLY. It used to also merge in the
Apollo-backed Campaign list (app.api.campaign's list_campaigns); that half
is intentionally removed from this endpoint's response, NOT deleted from
the codebase -- app/api/campaign.py, CampaignService, CampaignSyncService,
and EmailSequenceSyncService are untouched and still fully functional if
called directly. Nothing here writes to any store, calls Claude, or calls
Apollo.
"""

from fastapi import APIRouter, Depends

from app.api.mail import list_campaigns as list_mail_campaigns
from app.dependencies import get_mail_campaign_service
from app.models.campaign_manager import (
    MAIL_STATUS_BUCKET as _MAIL_STATUS_BUCKET,
)
from app.models.campaign_manager import SendingMethod, UnifiedCampaignSummary
from app.models.mail import MailCampaign
from app.services.mail_campaign_service import MailCampaignService

router = APIRouter(prefix="/campaign-manager", tags=["campaign-manager"])


async def _mail_summary(campaign: MailCampaign, mail_service: MailCampaignService) -> UnifiedCampaignSummary:
    steps = await mail_service.list_steps(campaign.mail_campaign_id)
    step_word = "step" if len(steps) == 1 else "steps"

    # A DRAFT campaign has no audience snapshot yet (MailEnrollment rows are
    # only created by mark_ready()) -- showing an "eligible" count here would
    # be reading the CRM List's current size, not this campaign's actual
    # locked-in audience, so this is a neutral state instead of a number
    # that would silently change every time the list changes.
    if campaign.status.value == "draft":
        summary = f"{len(steps)} {step_word} · audience not yet locked"
    else:
        enrollments = await mail_service.list_enrollments(campaign.mail_campaign_id)
        eligible = sum(1 for e in enrollments if e.status.value == "pending")
        summary = f"{len(steps)} {step_word} · {eligible} contacts eligible"

    return UnifiedCampaignSummary(
        id=campaign.mail_campaign_id,
        sending_method=SendingMethod.ASTRONOMIC_MAIL,
        name=campaign.name,
        status_bucket=_MAIL_STATUS_BUCKET[campaign.status.value],
        raw_status=campaign.status.value,
        summary=summary,
        created_at=campaign.created_at,
        detail_path=f"/manager/campaigns/mail/{campaign.mail_campaign_id}",
    )


@router.get("/campaigns", response_model=list[UnifiedCampaignSummary])
async def list_unified_campaigns(
    mail_service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """
    Read-side listing for the Campaign Manager dashboard: Astronomic Mail
    campaigns only (GET /mail/campaigns). Apollo Campaign/Sequence records
    are deliberately excluded -- see module docstring. Read-only; never
    calls Claude or Apollo.
    """
    mail_campaigns = await list_mail_campaigns(service=mail_service)

    summaries = [await _mail_summary(mail_campaign, mail_service) for mail_campaign in mail_campaigns]

    return sorted(summaries, key=lambda s: s.created_at, reverse=True)
