"""
Route-level tests for GET /crm/activity and POST /crm/activity/exports.
Exercises just the activity router against a fresh FastAPI app, same
isolation style as test_campaign_lifecycle_endpoints.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.activity import router as activity_router
from app.dependencies import get_activity_log_service
from app.models.activity import ActivityCategory, ActivitySource
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.activity_log_service import ActivityLogService


@pytest.fixture
def test_client():
    service = ActivityLogService(store=MemoryActivityEventStore())
    app = FastAPI()
    app.include_router(activity_router)
    app.dependency_overrides[get_activity_log_service] = lambda: service
    with TestClient(app) as client:
        yield client, service


def test_list_activity_events_empty_state(test_client):
    client, _service = test_client
    resp = client.get("/crm/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_list_activity_events_category_filter(test_client):
    client, service = test_client
    await service.record("itf.submission_received", ActivityCategory.ITF, ActivitySource.ITF_AUTOMATION, "itf event")
    await service.record("list.created", ActivityCategory.LISTS, ActivitySource.LISTS, "list event")

    resp = client.get("/crm/activity", params={"category": "itf"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["summary"] == "itf event"


@pytest.mark.asyncio
async def test_list_activity_events_search(test_client):
    client, service = test_client
    await service.record(
        "contact.created", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM,
        "Amos Ben-Meir was created.", entity_name="Amos Ben-Meir",
    )
    resp = client.get("/crm/activity", params={"q": "amos"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_list_activity_events_pagination(test_client):
    client, service = test_client
    for i in range(3):
        await service.record(f"e-{i}", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, f"event {i}")

    resp = client.get("/crm/activity", params={"page": 1, "page_size": 2})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_log_export_creates_a_contacts_exported_event(test_client):
    client, service = test_client
    resp = client.post(
        "/crm/activity/exports",
        json={"source": "more_filters", "contact_count": 127, "format": "csv"},
    )
    assert resp.status_code == 200

    page = await service.list_events()
    assert page.total == 1
    event = page.items[0]
    assert event.event_type == "contacts.exported"
    assert event.category == ActivityCategory.EXPORTS
    assert event.metadata["contact_count"] == 127
    assert event.metadata["source"] == "more_filters"
    assert "127" in event.summary


@pytest.mark.asyncio
async def test_log_export_for_a_list_includes_list_entity(test_client):
    client, service = test_client
    resp = client.post(
        "/crm/activity/exports",
        json={"source": "list", "contact_count": 5, "list_id": "l1", "list_name": "Austin Family Offices"},
    )
    assert resp.status_code == 200

    page = await service.list_events()
    event = page.items[0]
    assert event.entity_type == "list"
    assert event.entity_id == "l1"
    assert event.entity_name == "Austin Family Offices"
    assert "Austin Family Offices" in event.summary
