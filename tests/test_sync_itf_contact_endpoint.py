"""
Route-level tests for POST /sync/itf-contact -- auth, validation, and
response wiring. Business-logic behavior (dedup/merge/classification) is
covered in test_itf_ingestion_service.py; this file only checks the HTTP
layer: token enforcement, payload validation, and status-code mapping.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sync import router as sync_router
from app.config import settings
from app.dependencies import get_itf_ingestion_service
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.itf_ingestion_log_store import MemoryItfIngestionLogStore
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.itf_ingestion_service import ItfIngestionService

VALID_HEADERS = ["Timestamp", "First Name", "Last Name", "Email Address"]
VALID_VALUES = ["8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com"]


@pytest.fixture
def test_client(monkeypatch):
    monkeypatch.setattr(settings, "itf_webhook_token", "test-secret-token")

    crm_service = CrmService()
    import_service = CrmImportService(crm_service=crm_service, batch_store=MemoryCrmImportBatchStore())
    service = ItfIngestionService(import_service=import_service, log_store=MemoryItfIngestionLogStore())

    app = FastAPI()
    app.include_router(sync_router)
    app.dependency_overrides[get_itf_ingestion_service] = lambda: service

    with TestClient(app) as client:
        yield client


def auth_headers(token="test-secret-token"):
    return {"Authorization": f"Bearer {token}"}


def valid_payload(**overrides):
    payload = {"row_number": 2, "headers": VALID_HEADERS, "values": VALID_VALUES}
    payload.update(overrides)
    return payload


# --- auth ---


def test_missing_token_is_rejected_with_401(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload())
    assert resp.status_code == 401


def test_invalid_token_is_rejected_with_401(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers("wrong-token"))
    assert resp.status_code == 401


def test_malformed_authorization_header_is_rejected_with_401(test_client):
    resp = test_client.post(
        "/sync/itf-contact", json=valid_payload(), headers={"Authorization": "test-secret-token"}
    )
    assert resp.status_code == 401


def test_error_body_never_echoes_the_configured_token(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers("wrong-token"))
    assert "test-secret-token" not in resp.text


def test_webhook_not_configured_returns_503(monkeypatch):
    monkeypatch.setattr(settings, "itf_webhook_token", None)
    crm_service = CrmService()
    import_service = CrmImportService(crm_service=crm_service, batch_store=MemoryCrmImportBatchStore())
    service = ItfIngestionService(import_service=import_service, log_store=MemoryItfIngestionLogStore())

    app = FastAPI()
    app.include_router(sync_router)
    app.dependency_overrides[get_itf_ingestion_service] = lambda: service

    with TestClient(app) as client:
        resp = client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers())
    assert resp.status_code == 503


# --- happy path ---


def test_valid_authenticated_submission_creates_a_contact(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "created"
    assert body["contact_id"] is not None
    assert body["dry_run"] is False


def test_dry_run_query_param_never_writes(test_client):
    resp = test_client.post("/sync/itf-contact?dry_run=true", json=valid_payload(), headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["contact_id"] is None
    assert body["mapped_fields"]["email"] == "ada@example.com"


def test_repeated_identical_call_is_idempotent(test_client):
    first = test_client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers())
    second = test_client.post("/sync/itf-contact", json=valid_payload(), headers=auth_headers())
    assert first.json()["status"] == "created"
    assert second.json()["status"] == "already_processed"
    assert second.json()["contact_id"] == first.json()["contact_id"]


# --- validation ---


def test_missing_row_number_is_rejected_with_422(test_client):
    payload = {"headers": VALID_HEADERS, "values": VALID_VALUES}
    resp = test_client.post("/sync/itf-contact", json=payload, headers=auth_headers())
    assert resp.status_code == 422


def test_zero_row_number_is_rejected_with_422(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload(row_number=0), headers=auth_headers())
    assert resp.status_code == 422


def test_missing_headers_is_rejected_with_422(test_client):
    payload = {"row_number": 2, "values": VALID_VALUES}
    resp = test_client.post("/sync/itf-contact", json=payload, headers=auth_headers())
    assert resp.status_code == 422


def test_empty_headers_list_is_rejected_with_422(test_client):
    resp = test_client.post("/sync/itf-contact", json=valid_payload(headers=[]), headers=auth_headers())
    assert resp.status_code == 422


def test_malformed_json_body_is_rejected_with_422(test_client):
    resp = test_client.post(
        "/sync/itf-contact", content="not json", headers={**auth_headers(), "Content-Type": "application/json"}
    )
    assert resp.status_code == 422


def test_empty_values_still_processes_headers_only(test_client):
    """values shorter than headers (or entirely empty) is a normal shape --
    Google Sheets omits trailing empty cells -- not a validation error."""
    resp = test_client.post("/sync/itf-contact", json=valid_payload(values=[]), headers=auth_headers())
    assert resp.status_code == 200
