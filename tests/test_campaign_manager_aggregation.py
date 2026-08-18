"""
Tests for GET /campaign-manager/campaigns -- the Campaign Manager
Integration Phase's read-side aggregation endpoint. Verifies the merge of
the existing Apollo Campaign list and Astronomic Mail campaign list into
UnifiedCampaignSummary rows, without touching either underlying store or
enum. Exercises just the campaign, mail, and campaign_manager routers
against a fresh FastAPI app -- not app.main's full lifespan.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.api.campaign_manager import router as campaign_manager_router
from app.api.mail import router as mail_router
from app.dependencies import get_campaign_service, get_email_sequence_sync_service, get_mail_campaign_service
from app.models.campaign import Campaign, CampaignPlan, Filters
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
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
async def test_apollo_campaigns_appear_with_correct_sending_method_and_route(test_client):
    client, apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(make_apollo_campaign("c1", "2026-01-01T00:00:00Z", total_matches=160, selected_prospects=[{"id": "p1"}]))

    resp = client.get("/campaign-manager/campaigns")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["id"] == "c1"
    assert item["sending_method"] == "apollo"
    assert item["name"] == "Campaign c1"
    assert item["raw_status"] == "draft"
    assert item["status_bucket"] == "draft"
    assert item["summary"] == "160 matches · 1 selected"
    assert item["detail_path"] == "/manager/campaigns/c1"


@pytest.mark.asyncio
async def test_apollo_campaign_with_no_search_yet_shows_neutral_summary(test_client):
    client, apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(make_apollo_campaign("c1", "2026-01-01T00:00:00Z"))

    resp = client.get("/campaign-manager/campaigns")

    assert resp.json()[0]["summary"] == "Not searched yet"


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
    from datetime import time as dt_time

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
    await mail_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    resp = client.get("/campaign-manager/campaigns")

    body = resp.json()[0]
    assert body["raw_status"] == "ready"
    assert body["status_bucket"] == "ready"
    assert body["summary"] == "1 step · 0 contacts eligible"


@pytest.mark.asyncio
async def test_both_providers_merge_and_sort_newest_first(test_client):
    client, apollo_store, mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(make_apollo_campaign("oldest", "2026-01-01T00:00:00Z"))
    await mail_service.create_campaign("middle")
    await apollo_store.create(make_apollo_campaign("newest", "2026-07-01T00:00:00Z"))

    # Force the mail campaign's created_at between the two Apollo ones so the
    # sort assertion actually exercises cross-provider ordering.
    mail_campaigns = await mail_service.list_campaigns()
    mail_campaigns[0].created_at = mail_campaigns[0].created_at.replace(year=2026, month=3, day=1)
    await mail_service.campaign_store.save(mail_campaigns[0])

    resp = client.get("/campaign-manager/campaigns")

    assert resp.status_code == 200
    ordered = [(item["sending_method"], item["id"]) for item in resp.json()]
    ids_only = [item[1] if item[0] == "apollo" else "middle" for item in ordered]
    assert ids_only == ["newest", "middle", "oldest"]


@pytest.mark.asyncio
async def test_archived_apollo_campaign_is_excluded_by_default(test_client):
    """Matches GET /campaign's existing default -- the aggregation never
    overrides include_archived, so the dashboard's archived-hiding behavior
    is unchanged."""
    from datetime import datetime, timezone

    from app.models.email_sequence import EmailSequence, EmailSequenceStatus

    client, apollo_store, _mail_service, _agent, _apollo, _ranker = test_client

    await apollo_store.create(make_apollo_campaign("visible", "2026-07-01T00:00:00Z"))
    await apollo_store.create(make_apollo_campaign("archived", "2026-07-02T00:00:00Z"))

    app = client.app
    sequence_sync_service = app.dependency_overrides[get_email_sequence_sync_service]()
    now = datetime.now(timezone.utc)
    await sequence_sync_service.store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="archived",
            apollo_sequence_id="apollo-seq-1",
            name="Archived one",
            status=EmailSequenceStatus.ARCHIVED,
            created_at=now,
            updated_at=now,
        )
    )

    resp = client.get("/campaign-manager/campaigns")

    ids = {item["id"] for item in resp.json()}
    assert ids == {"visible"}


def test_never_calls_claude_or_apollo(test_client):
    client, _apollo_store, _mail_service, fake_agent, fake_apollo, fake_ranker = test_client

    client.get("/campaign-manager/campaigns")

    fake_agent.generate_campaign_plan.assert_not_called()
    fake_ranker.rank.assert_not_called()
    fake_apollo.search_people.assert_not_called()
