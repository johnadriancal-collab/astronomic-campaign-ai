"""
Route-level tests for /client-crm/clients -- Client CRM Stage 1B
(2026-09-07). Exercises just the client_crm router against a fresh
FastAPI app (no session-auth middleware mounted here -- see
test_session_auth_middleware.py for the auth-boundary coverage: this
file is about request/response shape and HTTP status mapping only), same
isolation style as test_activity_api.py.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.client_crm import router as client_crm_router
from app.dependencies import get_client_crm_service
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_store import MemoryClientStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService


@pytest.fixture
def test_client():
    service = ClientCrmService(client_store=MemoryClientStore(), activity_log=ActivityLogService(MemoryActivityEventStore()))
    app = FastAPI()
    app.include_router(client_crm_router)
    app.dependency_overrides[get_client_crm_service] = lambda: service
    with TestClient(app) as client:
        yield client, service


def test_create_client_minimal(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Hive ASMBLD"
    assert body["client_id"]
    assert body["status"] == "active"
    assert body["relationship_classification"] is None
    assert body["archived"] is False


def test_create_client_with_full_fields(test_client):
    client, _service = test_client
    resp = client.post(
        "/client-crm/clients",
        json={
            "name": "Acme Co",
            "website": "https://acme.example.com",
            "industry": "Venture",
            "status": "inactive",
            "relationship_classification": "opportunity",
            "owner": "Chris",
            "next_action": "Schedule Q1 check-in",
            "next_action_due": "2027-01-15",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "inactive"
    assert body["relationship_classification"] == "opportunity"
    assert body["next_action_due"] == "2027-01-15"


def test_create_client_missing_name_is_422(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={})
    assert resp.status_code == 422  # Pydantic request validation, not a service-layer 400


def test_create_client_blank_name_is_400(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "   "})
    assert resp.status_code == 400


def test_create_client_invalid_status_enum_is_422(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "Hive ASMBLD", "status": "not_a_real_status"})
    assert resp.status_code == 422


def test_get_client_existing(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.get(f"/client-crm/clients/{created['client_id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Hive ASMBLD"


def test_get_client_missing_is_404(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients/does-not-exist")
    assert resp.status_code == 404


def test_list_clients_empty_state(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_clients_search_and_filter(test_client):
    client, _service = test_client
    client.post("/client-crm/clients", json={"name": "Hive ASMBLD", "status": "active"})
    client.post("/client-crm/clients", json={"name": "Acme Co", "status": "inactive"})

    resp = client.get("/client-crm/clients", params={"q": "hive"})
    assert resp.json()["total"] == 1

    resp = client.get("/client-crm/clients", params={"status": "inactive"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["name"] == "Acme Co"


def test_list_clients_invalid_sort_by_is_400(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients", params={"sort_by": "not_a_real_field"})
    assert resp.status_code == 400


def test_update_client_partial_patch(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(f"/client-crm/clients/{created['client_id']}", json={"industry": "Venture"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["industry"] == "Venture"
    assert body["name"] == "Hive ASMBLD"
    assert body["updated_at"] != created["updated_at"]


def test_update_client_ignores_unknown_fields_like_client_id(test_client):
    """Confirms the request model itself has no client_id/created_at
    field -- a real HTTP caller sending them gets them silently dropped
    by Pydantic's own default "ignore extra fields" behavior, never
    reaching the service at all."""
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(
        f"/client-crm/clients/{created['client_id']}",
        json={"client_id": "different-id", "created_at": "2000-01-01T00:00:00Z", "industry": "Venture"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == created["client_id"]
    assert body["created_at"] == created["created_at"]
    assert body["industry"] == "Venture"


def test_update_client_missing_is_404(test_client):
    client, _service = test_client
    resp = client.patch("/client-crm/clients/does-not-exist", json={"industry": "Venture"})
    assert resp.status_code == 404


def test_update_client_blank_name_is_400(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(f"/client-crm/clients/{created['client_id']}", json={"name": "   "})
    assert resp.status_code == 400


def test_archive_and_restore_via_patch(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client_id = created["client_id"]

    archived = client.patch(f"/client-crm/clients/{client_id}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    # Archived Client remains directly readable by id.
    fetched = client.get(f"/client-crm/clients/{client_id}")
    assert fetched.status_code == 200
    assert fetched.json()["archived"] is True

    # Hidden from the default list...
    default_list = client.get("/client-crm/clients")
    assert default_list.json()["total"] == 0

    # ...but included when asked for.
    included_list = client.get("/client-crm/clients", params={"include_archived": True})
    assert included_list.json()["total"] == 1

    restored = client.patch(f"/client-crm/clients/{client_id}", json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert client.get("/client-crm/clients").json()["total"] == 1
