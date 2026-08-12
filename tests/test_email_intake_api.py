"""
Route-level tests for POST /sync/email-intake and the /crm/email-intake/*
review API -- auth, validation, and response wiring. Business-logic
behavior (matching/extraction/approval merge rules) is covered in
test_email_intake_service.py; this file only checks the HTTP layer.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.email_intake import crm_router, sync_router
from app.config import settings
from app.dependencies import get_email_intake_service
from app.repositories.email_intake_store import MemoryEmailIntakeStore
from app.services.crm_service import CrmService
from app.services.email_intake_service import EmailIntakeService


def valid_payload(**overrides):
    payload = {
        "gmail_message_id": "gmail-msg-1",
        "sender": "amos@example.com",
        "subject": "Update",
        "body_text": "Just checking in.",
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def test_client(monkeypatch):
    monkeypatch.setattr(settings, "email_intake_webhook_token", "test-secret-token")

    crm_service = CrmService()
    service = EmailIntakeService(store=MemoryEmailIntakeStore(), crm_service=crm_service)

    app = FastAPI()
    app.include_router(sync_router)
    app.include_router(crm_router)
    app.dependency_overrides[get_email_intake_service] = lambda: service

    with TestClient(app) as client:
        yield client, service, crm_service


def auth_headers(token="test-secret-token"):
    return {"Authorization": f"Bearer {token}"}


# --- webhook auth ---


def test_missing_token_is_rejected_with_401(test_client):
    client, _, _ = test_client
    resp = client.post("/sync/email-intake", json=valid_payload())
    assert resp.status_code == 401


def test_invalid_token_is_rejected_with_401(test_client):
    client, _, _ = test_client
    resp = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers("wrong-token"))
    assert resp.status_code == 401


def test_malformed_authorization_header_is_rejected_with_401(test_client):
    client, _, _ = test_client
    resp = client.post("/sync/email-intake", json=valid_payload(), headers={"Authorization": "test-secret-token"})
    assert resp.status_code == 401


def test_error_body_never_echoes_the_configured_token(test_client):
    client, _, _ = test_client
    resp = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers("wrong-token"))
    assert "test-secret-token" not in resp.text


def test_webhook_not_configured_returns_503(monkeypatch):
    monkeypatch.setattr(settings, "email_intake_webhook_token", None)
    crm_service = CrmService()
    service = EmailIntakeService(store=MemoryEmailIntakeStore(), crm_service=crm_service)
    app = FastAPI()
    app.include_router(sync_router)
    app.dependency_overrides[get_email_intake_service] = lambda: service
    with TestClient(app) as client:
        resp = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers())
    assert resp.status_code == 503


# --- webhook validation / happy path ---


def test_malformed_payload_returns_422(test_client):
    client, _, _ = test_client
    resp = client.post(
        "/sync/email-intake", json={"subject": "missing required fields"}, headers=auth_headers()
    )
    assert resp.status_code == 422


def test_valid_authenticated_payload_creates_item(test_client):
    client, service, _ = test_client
    resp = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_processed"] is False
    assert body["intake_id"]


def test_duplicate_gmail_message_id_returns_already_processed(test_client):
    client, _, _ = test_client
    first = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers())
    second = client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers())
    assert first.json()["intake_id"] == second.json()["intake_id"]
    assert second.json()["already_processed"] is True


# --- review API ---


def test_list_email_intake_items(test_client):
    client, _, _ = test_client
    client.post("/sync/email-intake", json=valid_payload(), headers=auth_headers())
    resp = client.get("/crm/email-intake")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_get_missing_item_returns_404(test_client):
    client, _, _ = test_client
    resp = client.get("/crm/email-intake/does-not-exist")
    assert resp.status_code == 404


def test_manual_match_missing_contact_returns_404(test_client):
    client, service, _ = test_client
    client.post("/sync/email-intake", json=valid_payload(gmail_message_id="m1"), headers=auth_headers())
    items = client.get("/crm/email-intake").json()["items"]
    intake_id = items[0]["intake_id"]
    resp = client.post(f"/crm/email-intake/{intake_id}/match", json={"crm_contact_id": "does-not-exist"})
    assert resp.status_code == 404


def test_approve_zero_selected_fields_returns_422(test_client):
    client, service, crm_service = test_client
    import asyncio

    contact = asyncio.run(crm_service.create_contact({"email": "amos@example.com", "company": "Massive Capital"}))
    resp = client.post(
        "/sync/email-intake",
        json=valid_payload(sender="amos@example.com", body_text="Amos is now at Massive Ventures."),
        headers=auth_headers(),
    )
    intake_id = resp.json()["intake_id"]
    approve_resp = client.post(f"/crm/email-intake/{intake_id}/approve", json={"field_keys": []})
    assert approve_resp.status_code == 422


def test_approve_happy_path_returns_approved_status(test_client):
    client, service, crm_service = test_client
    import asyncio

    asyncio.run(crm_service.create_contact({"email": "amos@example.com", "company": "Massive Capital"}))
    resp = client.post(
        "/sync/email-intake",
        json=valid_payload(sender="amos@example.com", body_text="Amos is now at Massive Ventures."),
        headers=auth_headers(),
    )
    intake_id = resp.json()["intake_id"]
    approve_resp = client.post(f"/crm/email-intake/{intake_id}/approve", json={"field_keys": ["company"]})
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"


def test_reject_happy_path(test_client):
    client, _, crm_service = test_client
    import asyncio

    asyncio.run(crm_service.create_contact({"email": "amos@example.com", "company": "Massive Capital"}))
    resp = client.post(
        "/sync/email-intake",
        json=valid_payload(sender="amos@example.com", body_text="Amos is now at Massive Ventures."),
        headers=auth_headers(),
    )
    intake_id = resp.json()["intake_id"]
    reject_resp = client.post(f"/crm/email-intake/{intake_id}/reject")
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "rejected"
