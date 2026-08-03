"""
Tests for GET /campaign (Campaign Manager's Campaigns list). Exercises just
the campaign router against a fresh FastAPI app -- not app.main's full
lifespan -- so this doesn't touch the real SQLite file and stays isolated
from any other test's store state.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.campaign import router as campaign_router
from app.dependencies import get_campaign_service, get_email_sequence_sync_service
from app.models.campaign import Campaign, CampaignPlan, Filters
from app.models.email_sequence import EmailSequence, EmailSequenceStatus
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.services.campaign_service import CampaignService
from app.services.email_sequence_sync_service import EmailSequenceSyncService


def make_campaign(campaign_id: str, created_at: str) -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt=f"prompt for {campaign_id}",
        created_at=created_at,
        plan=CampaignPlan(campaign_name=f"Campaign {campaign_id}", filters=Filters(), sequence=[]),
    )


@pytest.fixture
def test_client():
    """
    A minimal app hosting only the campaign router, with CampaignService
    swapped for one backed by a fresh MemoryCampaignStore and mocked
    agent/apollo/ranker -- so any accidental Claude/Apollo call during
    listing fails loudly instead of silently succeeding against a real API.
    """
    store = MemoryCampaignStore()
    fake_agent = AsyncMock()
    fake_apollo = AsyncMock()
    fake_ranker = AsyncMock()
    service = CampaignService(agent=fake_agent, apollo=fake_apollo, ranker=fake_ranker, store=store)

    sequence_store = MemoryEmailSequenceStore()
    sequence_sync_service = EmailSequenceSyncService(
        campaign_store=store, store=sequence_store, step_store=MemoryEmailSequenceStepStore(), apollo=fake_apollo
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: service
    app.dependency_overrides[get_email_sequence_sync_service] = lambda: sequence_sync_service

    with TestClient(app) as client:
        yield client, store, fake_agent, fake_apollo, fake_ranker, sequence_store


def test_list_empty_returns_empty_array(test_client):
    client, _store, _agent, _apollo, _ranker, _seq_store = test_client

    resp = client.get("/campaign")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_returns_all_campaigns_sorted_newest_first(test_client):
    client, store, _agent, _apollo, _ranker, _seq_store = test_client

    await store.create(make_campaign("oldest", "2026-01-01T00:00:00Z"))
    await store.create(make_campaign("middle", "2026-06-01T00:00:00Z"))
    await store.create(make_campaign("newest", "2026-07-01T00:00:00Z"))

    resp = client.get("/campaign")

    assert resp.status_code == 200
    ids = [c["campaign_id"] for c in resp.json()]
    assert ids == ["newest", "middle", "oldest"]


@pytest.mark.asyncio
async def test_list_includes_the_fields_the_manager_ui_needs(test_client):
    client, store, _agent, _apollo, _ranker, _seq_store = test_client

    campaign = make_campaign("c1", "2026-07-01T00:00:00Z")
    campaign.total_matches = 160
    campaign.selected_prospects = [{"id": "p1"}, {"id": "p2"}]
    await store.create(campaign)

    resp = client.get("/campaign")

    assert resp.status_code == 200
    body = resp.json()[0]
    assert body["campaign_id"] == "c1"
    assert body["status"] == "draft"
    assert body["original_prompt"] == "prompt for c1"
    assert body["plan"]["campaign_name"] == "Campaign c1"
    assert body["total_matches"] == 160
    assert body["selected_prospect_count"] == 2
    assert body["created_at"].startswith("2026-07-01")


@pytest.mark.asyncio
async def test_list_hides_archived_by_default_but_include_archived_shows_it(test_client):
    client, store, _agent, _apollo, _ranker, seq_store = test_client

    await store.create(make_campaign("visible", "2026-07-01T00:00:00Z"))
    await store.create(make_campaign("archived", "2026-07-02T00:00:00Z"))
    now = datetime.now(timezone.utc)
    await seq_store.create(
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

    default_resp = client.get("/campaign")
    assert [c["campaign_id"] for c in default_resp.json()] == ["visible"]

    included_resp = client.get("/campaign?include_archived=true")
    ids = {c["campaign_id"] for c in included_resp.json()}
    assert ids == {"visible", "archived"}


def test_list_never_calls_claude_or_apollo(test_client):
    """
    Listing is read-only against the store -- it must never touch Claude
    (agent/ranker) or Apollo, preserving the plan/ranking determinism
    guarantees the rest of the pipeline relies on.
    """
    client, _store, fake_agent, fake_apollo, fake_ranker, _seq_store = test_client

    client.get("/campaign")

    fake_agent.generate_campaign_plan.assert_not_called()
    fake_ranker.rank.assert_not_called()
    fake_apollo.search_people.assert_not_called()
