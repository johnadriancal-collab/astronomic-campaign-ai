"""
Route-level tests for GET /campaign/{id}/sequence and
POST /campaign/{id}/sequence/sync. Exercises just the campaign router
against a fresh FastAPI app, isolated from the real SQLite file.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.dependencies import get_campaign_service, get_email_sequence_sync_service, get_lead_service
from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters, SequenceStep
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.lead_service import LeadService


def make_campaign(campaign_id: str, apollo_sequence_id: str | None = "apollo-seq-1") -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt="test prompt",
        created_at=datetime.now(timezone.utc),
        status=CampaignStatus.BUILT,
        plan=CampaignPlan(
            campaign_name="Test Campaign",
            filters=Filters(),
            sequence=[SequenceStep(day=0, subject="Subject", body="Body")],
        ),
        apollo_sequence_id=apollo_sequence_id,
    )


@pytest.fixture
def test_client():
    campaign_store = MemoryCampaignStore()
    lead_store = MemoryLeadStore()
    campaign_lead_store = MemoryCampaignLeadStore()
    sequence_store = MemoryEmailSequenceStore()
    step_store = MemoryEmailSequenceStepStore()

    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)
    campaign_service = CampaignService(
        store=campaign_store, lead_service=lead_service, campaign_lead_store=campaign_lead_store
    )
    fake_apollo = AsyncMock()
    sync_service = EmailSequenceSyncService(
        campaign_store=campaign_store, store=sequence_store, step_store=step_store, apollo=fake_apollo
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_lead_service] = lambda: lead_service
    app.dependency_overrides[get_email_sequence_sync_service] = lambda: sync_service

    with TestClient(app) as client:
        yield client, campaign_store, fake_apollo


def apollo_response(apollo_sequence_id: str) -> dict:
    return {
        "emailer_campaigns": [
            {
                "id": apollo_sequence_id,
                "active": True,
                "archived": False,
                "status_reason": "manual_approve",
                "unique_scheduled": 1,
                "unique_delivered": 1,
                "unique_opened": 0,
                "unique_clicked": 0,
                "unique_replied": 0,
                "unique_bounced": 0,
                "unique_unsubscribed": 0,
                "emailer_steps": [
                    {"id": "apollo-step-1", "position": 1, "wait_time": 0, "wait_mode": "day", "type": "auto_email"}
                ],
            }
        ]
    }


@pytest.mark.asyncio
async def test_get_sequence_404s_for_unknown_campaign(test_client):
    client, _campaign_store, _apollo = test_client
    resp = client.get("/campaign/does-not-exist/sequence")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_sequence_404s_when_never_synced(test_client):
    client, campaign_store, _apollo = test_client
    await campaign_store.create(make_campaign("c1"))

    resp = client.get("/campaign/c1/sequence")

    assert resp.status_code == 404
    assert "not been synced" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sync_then_get_returns_the_synced_sequence(test_client):
    client, campaign_store, apollo = test_client
    await campaign_store.create(make_campaign("c1"))
    apollo.search_sequences.return_value = apollo_response("apollo-seq-1")

    sync_resp = client.post("/campaign/c1/sequence/sync")
    assert sync_resp.status_code == 200
    body = sync_resp.json()
    assert body["campaign_id"] == "c1"
    assert body["status"] == "active"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["subject"] == "Subject"
    assert body["last_synced_at"] is not None

    get_resp = client.get("/campaign/c1/sequence")
    assert get_resp.status_code == 200
    assert get_resp.json()["email_sequence_id"] == body["email_sequence_id"]


@pytest.mark.asyncio
async def test_sync_requires_campaign_to_be_built_with_a_sequence(test_client):
    client, campaign_store, _apollo = test_client
    await campaign_store.create(make_campaign("c1", apollo_sequence_id=None))

    resp = client.post("/campaign/c1/sequence/sync")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sync_502s_and_leaves_no_synced_at_on_apollo_failure(test_client):
    client, campaign_store, apollo = test_client
    await campaign_store.create(make_campaign("c1"))
    apollo.search_sequences.side_effect = RuntimeError("Apollo is down")

    resp = client.post("/campaign/c1/sequence/sync")

    assert resp.status_code == 502
