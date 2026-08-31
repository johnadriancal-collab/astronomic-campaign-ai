"""
Tests for GET /campaign-manager/campaigns -- the Campaign Manager
dashboard's listing endpoint.

Product direction: Apollo Campaign/Sequence integration is disabled in the
Campaign Manager surface for now, so this endpoint returns Astronomic Mail
campaigns ONLY -- Apollo-backed Campaign records must never appear here,
even when they exist in the store. app/api/campaign.py's own router is
still mounted and exercised directly in these tests (via apollo_store) to
prove it remains fully functional on its own; it's just never called from
this aggregation endpoint anymore.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.api.campaign_manager import router as campaign_manager_router
from app.api.mail import router as mail_router
from app.dependencies import get_campaign_service, get_email_sequence_sync_service, get_mail_campaign_service
from app.models.campaign import Campaign, CampaignPlan, Filters
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.campaign_service import CampaignService
from app.services.crm_service import CrmService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.mail_campaign_service import MailCampaignService


def make_apollo_campaign(campaign_id: str, created_at: str, **overrides) -> Campaign:
    campaign = Campaign(
        campaign_id=campaign_id,
        original_prompt=f"prompt for {campaign_id}",
        created_at=created_at,
        plan=CampaignPlan(campaign_name=f"Campaign {campaign_id}", filters=Filters(), sequence=[]),
    )
    for key, value in overrides.items():
        setattr(campaign, key, value)
    return campaign


@pytest.fixture
def test_client():
    # apollo_store/campaign_service/sequence_sync_service exist here only to
    # (a) prove app/api/campaign.py's own router still works untouched, and
    # (b) prove the aggregation endpoint excludes Apollo campaigns even when
    # they're present in the store -- not because the aggregation endpoint
    # itself depends on them anymore.
    apollo_store = MemoryCampaignStore()
    fake_agent = AsyncMock()
    fake_apollo = AsyncMock()
    fake_ranker = AsyncMock()
    campaign_service = CampaignService(agent=fake_agent, apollo=fake_apollo, ranker=fake_ranker, store=apollo_store)

    sequence_store = MemoryEmailSequenceStore()
    sequence_sync_service = EmailSequenceSyncService(
        campaign_store=apollo_store, store=sequence_store, step_store=MemoryEmailSequenceStepStore(), apollo=fake_apollo
    )

    mail_service = MailCampaignService(
        campaign_store=MemoryMailCampaignStore(),
        step_store=MemoryMailSequenceStepStore(),
        enrollment_store=MemoryMailEnrollmentStore(),
        crm_service=CrmService(),
        activity_log=ActivityLogService(MemoryActivityEventStore()),
        mailbox_store=MemoryMailboxStore(),
        channel_store=MemoryMailCampaignMailboxStore(),
        window_store=MemoryMailSendWindowStore(),
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.include_router(mail_router)
    app.include_router(campaign_manager_router)
    app.dependency_overrides[get_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_email_sequence_sync_service] = lambda: sequence_sync_service
    app.dependency_overrides[get_mail_campaign_service] = lambda: mail_service

    with TestClient(app) as client:
        yield client, apollo_store, mail_service, fake_agent, fake_apollo, fake_ranker


def test_empty_returns_empty_array(test_client):
    client, _apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    resp = client.get("/campaign-manager/campaigns")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_apollo_campaigns_are_excluded_even_when_present(test_client):
    """The core regression this file now exists to guard: an Apollo-backed
    Campaign record existing in the store must never surface in the
    Campaign Manager dashboard's response."""
    client, apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(
        make_apollo_campaign("c1", "2026-01-01T00:00:00Z", total_matches=160, selected_prospects=[{"id": "p1"}])
    )

    resp = client.get("/campaign-manager/campaigns")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_apollo_router_still_works_directly_even_though_excluded_here(test_client):
    """app/api/campaign.py's own GET /campaign must remain fully functional
    on its own -- only the campaign-manager aggregation stopped calling it."""
    client, apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(make_apollo_campaign("c1", "2026-01-01T00:00:00Z"))

    resp = client.get("/campaign")

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["campaign_id"] == "c1"


@pytest.mark.asyncio
async def test_mail_campaigns_appear_with_correct_sending_method_and_route(test_client):
    client, _apollo_store, mail_service, _agent, _apollo, _ranker = test_client

    campaign = await mail_service.create_campaign("Q1 Investor Outreach")

    resp = client.get("/campaign-manager/campaigns")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["id"] == campaign.mail_campaign_id
    assert item["sending_method"] == "astronomic_mail"
    assert item["name"] == "Q1 Investor Outreach"
    assert item["raw_status"] == "draft"
    assert item["status_bucket"] == "draft"
    assert item["summary"] == "0 steps · audience not yet locked"
    assert item["detail_path"] == f"/manager/campaigns/mail/{campaign.mail_campaign_id}"


@pytest.mark.asyncio
async def test_mail_campaign_ready_status_shows_eligible_count_not_fake_zero(test_client):
    client, _apollo_store, mail_service, _agent, _apollo, _ranker = test_client

    crm_list = await mail_service.crm_service.create_contact_list("Test List")
    campaign = await mail_service.create_campaign("Q1 Investor Outreach")
    await mail_service.add_step(campaign.mail_campaign_id, subject="Hi {{first_name}}", body="Hello")
    await mail_service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": crm_list.list_id,
            "sending_days": [0, 1, 2],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "America/Chicago",
        },
    )
    now = datetime.now(timezone.utc)
    await mail_service.mailbox_store.create(
        Mailbox(
            mailbox_id="mbx-aggregation-test",
            provider=MailboxProvider.GOOGLE,
            email="aggregation-test@example.com",
            display_name="Victoria Bennett",
            status=MailboxStatus.CONNECTED,
            google_user_id="google-user-1",
            connected_at=now,
            updated_at=now,
        )
    )
    await mail_service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-aggregation-test"])
    await mail_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    resp = client.get("/campaign-manager/campaigns")

    body = resp.json()[0]
    assert body["raw_status"] == "ready"
    assert body["status_bucket"] == "ready"
    assert body["summary"] == "1 step · 0 contacts eligible"


@pytest.mark.asyncio
async def test_mail_campaigns_sort_newest_first(test_client):
    client, _apollo_store, mail_service, _agent, _apollo, _ranker = test_client

    await mail_service.create_campaign("oldest")
    await mail_service.create_campaign("newest")

    campaigns = await mail_service.list_campaigns()
    oldest = next(c for c in campaigns if c.name == "oldest")
    newest = next(c for c in campaigns if c.name == "newest")
    oldest.created_at = oldest.created_at.replace(year=2020)
    newest.created_at = newest.created_at.replace(year=2027)
    await mail_service.campaign_store.save(oldest)
    await mail_service.campaign_store.save(newest)

    resp = client.get("/campaign-manager/campaigns")

    names = [item["name"] for item in resp.json()]
    assert names == ["newest", "oldest"]


def test_never_calls_claude_or_apollo(test_client):
    client, _apollo_store, _mail_service, fake_agent, fake_apollo, fake_ranker = test_client

    client.get("/campaign-manager/campaigns")

    fake_agent.generate_campaign_plan.assert_not_called()
    fake_ranker.rank.assert_not_called()
    fake_apollo.search_people.assert_not_called()
    fake_apollo.list_sequences.assert_not_called()
    fake_apollo.get_sequence.assert_not_called()
