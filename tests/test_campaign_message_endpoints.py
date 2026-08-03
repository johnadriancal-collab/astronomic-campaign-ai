"""
Route-level tests for the /campaign/{id}/messages endpoints. Exercises
just the campaign router against a fresh FastAPI app, isolated from the
real SQLite file -- same pattern as test_campaign_sequence_endpoints.py.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.dependencies import (
    get_campaign_service,
    get_email_message_sync_service,
    get_email_sequence_sync_service,
    get_lead_service,
)
from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters, SequenceStep
from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.models.lead import CampaignLead, CampaignLeadStatus, Lead, LeadStatus
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_message_event_store import MemoryEmailMessageEventStore
from app.repositories.email_message_store import MemoryEmailMessageStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.lead_service import LeadService


@pytest.fixture
def test_client():
    campaign_store = MemoryCampaignStore()
    lead_store = MemoryLeadStore()
    campaign_lead_store = MemoryCampaignLeadStore()
    sequence_store = MemoryEmailSequenceStore()
    step_store = MemoryEmailSequenceStepStore()
    message_store = MemoryEmailMessageStore()
    event_store = MemoryEmailMessageEventStore()

    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)
    campaign_service = CampaignService(
        store=campaign_store, lead_service=lead_service, campaign_lead_store=campaign_lead_store
    )
    fake_apollo = AsyncMock()
    sequence_sync_service = EmailSequenceSyncService(
        campaign_store=campaign_store, store=sequence_store, step_store=step_store, apollo=fake_apollo
    )
    message_sync_service = EmailMessageSyncService(
        sequence_store=sequence_store,
        step_store=step_store,
        message_store=message_store,
        event_store=event_store,
        lead_store=lead_store,
        campaign_lead_store=campaign_lead_store,
        apollo=fake_apollo,
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_lead_service] = lambda: lead_service
    app.dependency_overrides[get_email_sequence_sync_service] = lambda: sequence_sync_service
    app.dependency_overrides[get_email_message_sync_service] = lambda: message_sync_service

    with TestClient(app) as client:
        yield client, campaign_store, lead_store, campaign_lead_store, sequence_store, step_store, fake_apollo


async def seed_campaign_with_lead_and_sequence(stores):
    _client, campaign_store, lead_store, campaign_lead_store, sequence_store, step_store, _apollo = stores
    now = datetime.now(timezone.utc)

    await campaign_store.create(
        Campaign(
            campaign_id="c1",
            original_prompt="test",
            created_at=now,
            status=CampaignStatus.BUILT,
            plan=CampaignPlan(campaign_name="Test", filters=Filters(), sequence=[SequenceStep(day=0, subject="S", body="B")]),
            apollo_sequence_id="apollo-seq-1",
        )
    )
    await lead_store.create(
        Lead(
            lead_id="lead-1",
            apollo_contact_id="apollo-contact-1",
            first_name="Test",
            last_name="Lead",
            email="t@example.com",
            title=None,
            company=None,
            company_domain=None,
            status=LeadStatus.NEW,
            created_at=now,
            updated_at=now,
            apollo_snapshot={},
        )
    )
    await campaign_lead_store.add(
        CampaignLead(campaign_id="c1", lead_id="lead-1", status=CampaignLeadStatus.ADDED, added_at=now)
    )
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="c1",
            apollo_sequence_id="apollo-seq-1",
            name="Test",
            status=EmailSequenceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    await step_store.create(
        EmailSequenceStep(
            email_sequence_step_id="step-1",
            email_sequence_id="seq-1",
            apollo_step_id="apollo-step-1",
            position=1,
            day=0,
            subject="S",
            body="B",
        )
    )


def make_raw_message(apollo_message_id: str) -> dict:
    return {
        "id": apollo_message_id,
        "contact_id": "apollo-contact-1",
        "emailer_step_id": "apollo-step-1",
        "status": "completed",
        "created_at": "2026-07-31T00:00:00.000Z",
    }


@pytest.mark.asyncio
async def test_list_messages_404s_for_unknown_campaign(test_client):
    client, *_ = test_client
    resp = client.get("/campaign/does-not-exist/messages")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_messages_empty_before_any_sync(test_client):
    client, *stores = test_client
    await seed_campaign_with_lead_and_sequence((client, *stores))

    resp = client.get("/campaign/c1/messages")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_sync_then_list_returns_the_synced_message(test_client):
    client, *stores = test_client
    apollo = stores[-1]
    await seed_campaign_with_lead_and_sequence((client, *stores))
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}

    sync_resp = client.post("/campaign/c1/messages/sync")
    assert sync_resp.status_code == 200
    body = sync_resp.json()
    assert len(body) == 1
    assert body[0]["apollo_message_id"] == "apollo-m1"
    assert body[0]["source"] == "apollo_sync"
    assert body[0]["lead_id"] == "lead-1"

    list_resp = client.get("/campaign/c1/messages")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1


@pytest.mark.asyncio
async def test_sync_messages_requires_sequence_to_exist(test_client):
    client, *_ = test_client
    resp = client.post("/campaign/c1/messages/sync")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sync_messages_502s_on_apollo_failure(test_client):
    client, *stores = test_client
    apollo = stores[-1]
    await seed_campaign_with_lead_and_sequence((client, *stores))
    apollo.search_messages.side_effect = RuntimeError("Apollo is down")

    resp = client.post("/campaign/c1/messages/sync")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_generate_fixtures_then_list_labels_source_as_test_fixture(test_client):
    client, *stores = test_client
    await seed_campaign_with_lead_and_sequence((client, *stores))

    resp = client.post("/campaign/c1/messages/fixtures")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["source"] == "test_fixture"
    assert body[0]["apollo_message_id"] is None


@pytest.mark.asyncio
async def test_events_roundtrip_via_sync_and_get(test_client):
    client, *stores = test_client
    apollo = stores[-1]
    await seed_campaign_with_lead_and_sequence((client, *stores))
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}
    sync_resp = client.post("/campaign/c1/messages/sync")
    message_id = sync_resp.json()[0]["email_message_id"]

    apollo.get_message_activities.return_value = {
        "activities": [
            {
                "event_group_type": "open",
                "emailer_message_events": [
                    {"id": "evt-1", "type": "open", "created_at": "2026-07-31T01:00:00.000Z"}
                ],
            }
        ]
    }

    sync_events_resp = client.post(f"/campaign/c1/messages/{message_id}/sync-events")
    assert sync_events_resp.status_code == 200
    assert len(sync_events_resp.json()) == 1

    get_events_resp = client.get(f"/campaign/c1/messages/{message_id}/events")
    assert get_events_resp.status_code == 200
    assert len(get_events_resp.json()) == 1
    assert get_events_resp.json()[0]["event_type"] == "open"


@pytest.mark.asyncio
async def test_sync_events_400s_for_fixture_message(test_client):
    client, *stores = test_client
    await seed_campaign_with_lead_and_sequence((client, *stores))
    fixtures_resp = client.post("/campaign/c1/messages/fixtures")
    message_id = fixtures_resp.json()[0]["email_message_id"]

    resp = client.post(f"/campaign/c1/messages/{message_id}/sync-events")
    assert resp.status_code == 400
