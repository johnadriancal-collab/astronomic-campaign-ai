"""
Route-level tests for the campaign-lifecycle endpoints added this pass:
GET /campaign/{id}/leads, POST /campaign/{id}/ready|activate|pause.

Exercises just the campaign router against a fresh FastAPI app -- not
app.main's full lifespan -- so this stays isolated from the real SQLite file
and from other tests' store state.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.dependencies import get_campaign_service, get_lead_service
from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.lead_service import LeadService


def make_campaign(campaign_id: str, status: CampaignStatus = CampaignStatus.DRAFT) -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt=f"prompt for {campaign_id}",
        created_at=datetime.now(timezone.utc),
        status=status,
        plan=CampaignPlan(campaign_name=f"Campaign {campaign_id}", filters=Filters(), sequence=[]),
    )


@pytest.fixture
def test_client():
    campaign_store = MemoryCampaignStore()
    lead_store = MemoryLeadStore()
    campaign_lead_store = MemoryCampaignLeadStore()
    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)

    fake_apollo = AsyncMock()
    campaign_service = CampaignService(
        apollo=fake_apollo,
        store=campaign_store,
        lead_service=lead_service,
        campaign_lead_store=campaign_lead_store,
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_lead_service] = lambda: lead_service

    with TestClient(app) as client:
        yield client, campaign_store, lead_store, campaign_lead_store, fake_apollo


def test_leads_endpoint_404s_for_unknown_campaign(test_client):
    client, *_ = test_client
    resp = client.get("/campaign/does-not-exist/leads")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_leads_endpoint_returns_campaign_specific_fields(test_client):
    client, campaign_store, lead_store, campaign_lead_store, _apollo = test_client
    from app.models.lead import CampaignLead, Lead, LeadStatus

    await campaign_store.create(make_campaign("c1"))
    now = datetime.now(timezone.utc)
    await lead_store.create(
        Lead(
            lead_id="l1",
            apollo_contact_id="contact-1",
            first_name="Riley",
            title="VP of Sales",
            company="Acme",
            email=None,
            status=LeadStatus.NEW,
            created_at=now,
            updated_at=now,
        )
    )
    await campaign_lead_store.add(
        CampaignLead(campaign_id="c1", lead_id="l1", added_at=now, claude_score=82, claude_reason="Good fit.")
    )

    resp = client.get("/campaign/c1/leads")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["first_name"] == "Riley"
    assert body[0]["claude_score"] == 82
    assert body[0]["claude_reason"] == "Good fit."
    assert body[0]["lead_status"] == "new"
    assert body[0]["campaign_status"] == "added"


@pytest.mark.asyncio
async def test_ready_requires_built_status(test_client):
    client, campaign_store, *_ = test_client
    await campaign_store.create(make_campaign("c1", CampaignStatus.DRAFT))

    resp = client.post("/campaign/c1/ready")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ready_succeeds_from_built(test_client):
    client, campaign_store, *_ = test_client
    await campaign_store.create(make_campaign("c1", CampaignStatus.BUILT))

    resp = client.post("/campaign/c1/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_activate_persists_only_on_apollo_success(test_client):
    client, campaign_store, _lead_store, _cl_store, apollo = test_client
    campaign = make_campaign("c1", CampaignStatus.READY)
    campaign.apollo_sequence_id = "seq-1"
    await campaign_store.create(campaign)
    apollo.activate_sequence.return_value = {}

    resp = client.post("/campaign/c1/activate")

    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert resp.json()["activated"] is True


@pytest.mark.asyncio
async def test_activate_502s_and_leaves_state_unchanged_on_apollo_failure(test_client):
    client, campaign_store, _lead_store, _cl_store, apollo = test_client
    campaign = make_campaign("c1", CampaignStatus.READY)
    campaign.apollo_sequence_id = "seq-1"
    await campaign_store.create(campaign)
    apollo.activate_sequence.side_effect = RuntimeError("Apollo down")

    resp = client.post("/campaign/c1/activate")

    assert resp.status_code == 502
    stored = await campaign_store.get("c1")
    assert stored.status == CampaignStatus.READY
    assert stored.activated is False
