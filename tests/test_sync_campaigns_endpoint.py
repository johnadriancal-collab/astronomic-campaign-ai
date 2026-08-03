"""
Route-level tests for POST /sync/campaigns.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sync import router as sync_router
from app.dependencies import get_campaign_sync_service
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.services.campaign_sync_service import CampaignSyncService


@pytest.fixture
def test_client():
    fake_apollo = AsyncMock()
    service = CampaignSyncService(
        campaign_store=MemoryCampaignStore(),
        sequence_store=MemoryEmailSequenceStore(),
        step_store=MemoryEmailSequenceStepStore(),
        apollo=fake_apollo,
    )

    app = FastAPI()
    app.include_router(sync_router)
    app.dependency_overrides[get_campaign_sync_service] = lambda: service

    with TestClient(app) as client:
        yield client, fake_apollo


def test_sync_campaigns_returns_report(test_client):
    client, apollo = test_client
    apollo.list_sequences.return_value = {
        "emailer_campaigns": [
            {"id": "apollo-1", "name": "Lumen Analytics", "active": True, "archived": False, "emailer_steps": []}
        ],
        "pagination": {"page": 1, "per_page": 100, "total_entries": 1, "total_pages": 1},
    }

    resp = client.post("/sync/campaigns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] == 1
    assert body["created"] == 1
    assert body["updated"] == 0
    assert body["archived"] == 0
    assert body["unchanged"] == 0
    assert body["duration_ms"] >= 0


def test_sync_campaigns_502s_on_apollo_failure(test_client):
    client, apollo = test_client
    apollo.list_sequences.side_effect = RuntimeError("Apollo is down")

    resp = client.post("/sync/campaigns")

    assert resp.status_code == 502
