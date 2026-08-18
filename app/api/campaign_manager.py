"""
Campaign Manager Integration Phase -- the ONE new backend surface for
"Option D" (read-side aggregation, approved architecture). This router
merges the existing Apollo Campaign list and the existing Astronomic Mail
campaign list into a single presentation DTO for the unified dashboard.

It deliberately calls the two existing list routes' functions directly
(list_campaigns from app.api.campaign, list_campaigns from app.api.mail)
instead of duplicating their filtering logic -- both remain the single
source of truth for what "the Apollo campaign list" / "the Mail campaign
list" even means. Nothing here ever writes to either store, calls Claude,
or calls Apollo. Nothing here merges the two underlying models -- Campaign
and MailCampaign are read as-is and reshaped into UnifiedCampaignSummary
rows only for this endpoint's response.
"""

from fastapi import APIRouter, Depends

from app.api.campaign import list_campaigns as list_apollo_campaigns
from app.api.mail import list_campaigns as list_mail_campaigns
from app.dependencies import get_campaign_service, get_email_sequence_sync_service, get_mail_campaign_service
from app.models.campaign import Campaign
from app.models.campaign_manager import CampaignStatusBucket, SendingMethod, UnifiedCampaignSummary
from app.models.mail import MailCampaign
from app.services.campaign_service import CampaignService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.mail_campaign_service import MailCampaignService

router = APIRouter(prefix="/campaign-manager", tags=["campaign-manager"])

# Presentation-only bucketing -- never written back to either store, and
# recomputed from the real enum value on every read. See this module's and
# app/models/campaign_manager.py's docstrings for why this exists only for
# the unified dashboard rather than replacing either status enum.
_APOLLO_STATUS_BUCKET: dict[str, CampaignStatusBucket] = {
    "draft": CampaignStatusBucket.DRAFT,
    "searched": CampaignStatusBucket.IN_PROGRESS,
    "building": CampaignStatusBucket.IN_PROGRESS,
    "built": CampaignStatusBucket.IN_PROGRESS,
    "failed": CampaignStatusBucket.FAILED,
    "ready": CampaignStatusBucket.READY,
    "active": CampaignStatusBucket.ACTIVE,
    "paused": CampaignStatusBucket.PAUSED,
}

_MAIL_STATUS_BUCKET: dict[str, CampaignStatusBucket] = {
    "draft": CampaignStatusBucket.DRAFT,
    "ready": CampaignStatusBucket.READY,
    "archived": CampaignStatusBucket.ARCHIVED,
}


def _apollo_summary(campaign: Campaign) -> UnifiedCampaignSummary:
    # Astronomic Mail has no sending yet, so no sent/reply/open metrics
    # exist for it -- Apollo's summary line only ever describes information
    # Apollo campaigns actually have (matches/selection), never a fabricated
    # send metric either. `total_matches is None` means search() hasn't run
    # yet -- a neutral state, not a fake "0 matches".
    if campaign.total_matches is None:
        summary = "Not searched yet"
    else:
        summary = f"{campaign.total_matches} matches · {campaign.selected_prospect_count} selected"

    return UnifiedCampaignSummary(
        id=campaign.campaign_id,
        sending_method=SendingMethod.APOLLO,
        name=campaign.plan.campaign_name,
        status_bucket=_APOLLO_STATUS_BUCKET[campaign.status.value],
        raw_status=campaign.status.value,
        summary=summary,
        created_at=campaign.created_at,
        detail_path=f"/manager/campaigns/{campaign.campaign_id}",
    )


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
    campaign_service: CampaignService = Depends(get_campaign_service),
    sequence_sync_service: EmailSequenceSyncService = Depends(get_email_sequence_sync_service),
    mail_service: MailCampaignService = Depends(get_mail_campaign_service),
):
    """
    Read-side aggregation for the Campaign Manager dashboard: merges the
    existing Apollo Campaign list (GET /campaign, archived excluded -- same
    default the dashboard already used) with the existing Astronomic Mail
    campaign list (GET /mail/campaigns). Read-only against both stores;
    never calls Claude or Apollo.
    """
    apollo_campaigns = await list_apollo_campaigns(
        service=campaign_service, sequence_sync_service=sequence_sync_service
    )
    mail_campaigns = await list_mail_campaigns(service=mail_service)

    summaries = [_apollo_summary(c) for c in apollo_campaigns]
    for mail_campaign in mail_campaigns:
        summaries.append(await _mail_summary(mail_campaign, mail_service))

    return sorted(summaries, key=lambda s: s.created_at, reverse=True)
