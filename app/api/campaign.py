"""
Campaign Builder + Manager routes: preview / search / build / get / leads /
ready / activate / pause.

Split out of main.py so main.py doesn't keep growing as more feature areas
(Campaign Manager: inbox, analytics, ...) add their own routers alongside
this one -- see docs/ARCHITECTURE.md for the Builder/Manager split this is
preparing for.

No endpoint after /campaign/preview ever regenerates a CampaignPlan --
every later stage loads the plan already stored on the Campaign.

ready/activate/pause map directly to CampaignService's own guarantee: the
stored Campaign is only ever mutated after Apollo confirms success (or,
for `ready`, after an explicit human action with no Apollo call at all).
A ValueError here means the requested transition isn't valid from the
campaign's current state; a bare Exception means Apollo itself failed --
in both cases nothing was persisted.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.dependencies import (
    get_campaign_service,
    get_email_message_sync_service,
    get_email_sequence_sync_service,
    get_lead_service,
)
from app.models.campaign import BuildRequest, Campaign, PreviewRequest, SearchRequest
from app.models.email_message import EmailMessageEvent, EmailMessageWithEventCounts
from app.models.email_sequence import EmailSequenceStatus, EmailSequenceWithSteps
from app.models.lead import CampaignLeadView
from app.repositories.campaign_store import CampaignNotFoundError
from app.repositories.email_message_store import EmailMessageNotFoundError
from app.services.campaign_service import CampaignService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.lead_service import LeadService

router = APIRouter(prefix="/campaign", tags=["campaign"])


@router.get("", response_model=list[Campaign])
async def list_campaigns(
    include_archived: bool = False,
    service: CampaignService = Depends(get_campaign_service),
    sequence_sync_service: EmailSequenceSyncService = Depends(get_email_sequence_sync_service),
):
    """
    Lists every stored campaign, newest first -- for Campaign Manager's
    Campaigns view. Read-only: loads from CampaignStore only, never calls
    Claude or Apollo. Registered ahead of GET /{campaign_id} below so it
    can't be shadowed by that route (not that it could collide anyway --
    this path has no additional segment for {campaign_id} to capture).

    Archived campaigns (their EmailSequence.status == ARCHIVED, set by
    CampaignSyncService's reconciliation pass) are hidden by default --
    never deleted, just not shown unless include_archived=true. This
    applies regardless of a campaign's source: a NATIVE campaign whose
    Apollo sequence gets archived is hidden here too, same as a SYNCED one.
    """
    campaigns = await service.store.list()
    if not include_archived:
        visible = []
        for campaign in campaigns:
            sequence = await sequence_sync_service.store.get_by_campaign_id(campaign.campaign_id)
            if sequence is not None and sequence.status == EmailSequenceStatus.ARCHIVED:
                continue
            visible.append(campaign)
        campaigns = visible
    return sorted(campaigns, key=lambda c: c.created_at, reverse=True)


@router.post("/preview", response_model=Campaign)
async def preview_campaign(
    req: PreviewRequest, service: CampaignService = Depends(get_campaign_service)
):
    """
    Generates a CampaignPlan -- the ONE and ONLY Claude call for this
    campaign's plan -- creates a new Campaign, stores it, and returns it.
    Every later endpoint operates on this campaign's id instead.
    """
    try:
        return await service.preview(req.prompt, req.desired_prospect_count)
    except Exception as e:
        logger.error(f"Preview failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/search", response_model=Campaign)
async def search_prospects(
    req: SearchRequest, service: CampaignService = Depends(get_campaign_service)
):
    """
    Loads the Campaign by id and searches Apollo using its STORED plan --
    never regenerates it. One Apollo search, one Claude ranking call.
    Calling this again on an already-searched campaign returns the cached
    result rather than re-searching.
    """
    try:
        return await service.search(req.campaign_id)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/build", response_model=Campaign)
async def build_campaign_endpoint(
    req: BuildRequest,
    auto_launch: bool = False,
    service: CampaignService = Depends(get_campaign_service),
):
    """
    Loads the Campaign by id and builds it in Apollo from its STORED plan
    and STORED selected_prospects -- never re-searches, never regenerates
    the plan. Idempotent per Apollo-ID field, so calling this again after a
    partial failure resumes rather than restarts.

    Does NOT send any emails, regardless of what Claude's plan sets for
    `launch` — pass auto_launch=true explicitly, or mark the campaign
    ready and call /campaign/{campaign_id}/activate once you've reviewed it.
    """
    try:
        return await service.build(req.campaign_id, auto_launch=auto_launch)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Campaign build failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{campaign_id}", response_model=Campaign)
async def get_campaign(
    campaign_id: str, service: CampaignService = Depends(get_campaign_service)
):
    """Full current state of a campaign -- for polling, resuming a draft, or history."""
    campaign = await service.store.get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")
    return campaign


@router.get("/{campaign_id}/leads", response_model=list[CampaignLeadView])
async def list_campaign_leads(
    campaign_id: str,
    campaign_service: CampaignService = Depends(get_campaign_service),
    lead_service: LeadService = Depends(get_lead_service),
):
    """
    The real, persisted Leads belonging to this campaign (via CampaignLead),
    each with this campaign's own status/score/reason -- not the raw
    ephemeral Campaign.selected_prospects snapshot.
    """
    campaign = await campaign_service.store.get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")
    return await lead_service.list_for_campaign(campaign_id)


@router.post("/{campaign_id}/ready", response_model=Campaign)
async def mark_campaign_ready(
    campaign_id: str, service: CampaignService = Depends(get_campaign_service)
):
    """Explicit human approval that this built campaign is ready to activate. No Apollo call."""
    try:
        return await service.mark_ready(campaign_id)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/activate", response_model=Campaign)
async def activate_campaign(
    campaign_id: str, service: CampaignService = Depends(get_campaign_service)
):
    """
    Activates the campaign's Apollo sequence. Local state (status=ACTIVE)
    is only updated after Apollo confirms success -- a failure here leaves
    the stored Campaign completely unchanged.
    """
    try:
        return await service.activate(campaign_id)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Activation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{campaign_id}/pause", response_model=Campaign)
async def pause_campaign(
    campaign_id: str, service: CampaignService = Depends(get_campaign_service)
):
    """Mirrors /activate exactly, via Apollo's deactivate_sequence."""
    try:
        return await service.pause(campaign_id)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Pause failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


def _sequence_response(sequence, steps) -> EmailSequenceWithSteps:
    return EmailSequenceWithSteps(**sequence.model_dump(), steps=steps)


@router.get("/{campaign_id}/sequence", response_model=EmailSequenceWithSteps)
async def get_campaign_sequence(
    campaign_id: str,
    campaign_service: CampaignService = Depends(get_campaign_service),
    sync_service: EmailSequenceSyncService = Depends(get_email_sequence_sync_service),
):
    """
    Read-only: the currently stored EmailSequence + steps. Never calls
    Apollo -- returns 404 if this campaign's sequence has never been
    synced yet (call POST .../sequence/sync first).
    """
    campaign = await campaign_service.store.get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")

    result = await sync_service.get_for_campaign(campaign_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Campaign {campaign_id}'s sequence has not been synced yet",
        )
    sequence, steps = result
    return _sequence_response(sequence, steps)


@router.post("/{campaign_id}/sequence/sync", response_model=EmailSequenceWithSteps)
async def sync_campaign_sequence(
    campaign_id: str, sync_service: EmailSequenceSyncService = Depends(get_email_sequence_sync_service)
):
    """
    Explicit, manual sync against Apollo's /emailer_campaigns/search --
    creates our deployed-configuration snapshot on first call, then
    refreshes status/aggregate stats/last_synced_at on every call. Never
    scheduled automatically; only ever runs when this endpoint is hit.
    A failed Apollo call leaves everything exactly as it was before.
    """
    try:
        sequence, steps = await sync_service.sync(campaign_id)
    except CampaignNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Sequence sync failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    return _sequence_response(sequence, steps)


@router.get("/{campaign_id}/messages", response_model=list[EmailMessageWithEventCounts])
async def list_campaign_messages(
    campaign_id: str,
    campaign_service: CampaignService = Depends(get_campaign_service),
    message_sync_service: EmailMessageSyncService = Depends(get_email_message_sync_service),
):
    """
    Read-only: every stored EmailMessage for this campaign's sequence --
    both real, Apollo-synced messages and any locally-generated test
    fixtures (see POST .../messages/fixtures), each carrying its own
    `source` field so the UI can label which is which. Never calls Apollo.
    """
    campaign = await campaign_service.store.get(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail=f"Campaign not found: {campaign_id}")
    return await message_sync_service.list_for_campaign(campaign_id)


@router.post("/{campaign_id}/messages/sync", response_model=list[EmailMessageWithEventCounts])
async def sync_campaign_messages(
    campaign_id: str, message_sync_service: EmailMessageSyncService = Depends(get_email_message_sync_service)
):
    """
    Explicit, manual sync against Apollo's /emailer_messages/search --
    pages through every message for this campaign's sequence and upserts
    by apollo_message_id. Never scheduled automatically. A failed Apollo
    call leaves everything exactly as it was before (messages_last_synced_at
    is only advanced once the full paginated sweep succeeds).
    """
    try:
        _sequence, messages = await message_sync_service.sync_messages(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Message sync failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    return messages


@router.post("/{campaign_id}/messages/fixtures", response_model=list[EmailMessageWithEventCounts])
async def generate_campaign_message_fixtures(
    campaign_id: str, message_sync_service: EmailMessageSyncService = Depends(get_email_message_sync_service)
):
    """
    Generates clearly-labeled local test-fixture messages/events for this
    campaign -- makes ZERO Apollo calls. Exists so the Messages/Opens/
    Clicks UI has something real to render while this Apollo account's
    sending mailboxes are unavailable (see
    docs/APOLLO_MESSAGE_API_FINDINGS.md #9). Idempotent -- a second call is
    a no-op if fixtures already exist for this campaign.
    """
    try:
        return await message_sync_service.generate_test_fixtures(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{campaign_id}/messages/{message_id}/events", response_model=list[EmailMessageEvent])
async def list_message_events(
    campaign_id: str,
    message_id: str,
    message_sync_service: EmailMessageSyncService = Depends(get_email_message_sync_service),
):
    """Read-only: this message's currently stored open/click events. Never calls Apollo."""
    return await message_sync_service.list_events(message_id)


@router.post("/{campaign_id}/messages/{message_id}/sync-events", response_model=list[EmailMessageEvent])
async def sync_message_events(
    campaign_id: str,
    message_id: str,
    message_sync_service: EmailMessageSyncService = Depends(get_email_message_sync_service),
):
    """
    Explicit, manual sync of ONE message's /activities -- deliberately
    per-message rather than bundled into /messages/sync, since Apollo has
    no bulk-events endpoint across a sequence. 400 if the message is a
    test fixture (there is no Apollo message to sync events from).
    """
    try:
        return await message_sync_service.sync_message_events(message_id)
    except EmailMessageNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Message event sync failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
