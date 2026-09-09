"""
Route-level tests for GET /integrations/contacts/photo -- auth, email
normalization, response shape, and the "never leaks other Contact data"
guarantee. Mirrors tests/test_sync_itf_contact_endpoint.py's conventions
for testing this codebase's shared-secret bearer-token pattern.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.integrations import router as integrations_router
from app.config import settings
from app.dependencies import get_crm_service
from app.models.crm import CrmContact
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.crm_service import CrmService

pytestmark = pytest.mark.asyncio

ROUTE = "/integrations/contacts/photo"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def auth_headers(token="test-integrations-token"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def crm_service():
    return CrmService(contact_store=MemoryCrmContactStore())


@pytest.fixture
def test_client(monkeypatch, crm_service):
    monkeypatch.setattr(settings, "integrations_api_token", "test-integrations-token")

    app = FastAPI()
    app.include_router(integrations_router)
    app.dependency_overrides[get_crm_service] = lambda: crm_service

    with TestClient(app) as client:
        yield client


# --- auth --------------------------------------------------------------


def test_missing_token_is_rejected_with_401(test_client):
    resp = test_client.get(ROUTE, params={"email": "ada@example.com"})
    assert resp.status_code == 401


def test_wrong_token_is_rejected_with_401(test_client):
    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers("wrong-token"))
    assert resp.status_code == 401


def test_malformed_authorization_header_is_rejected_with_401(test_client):
    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers={"Authorization": "test-integrations-token"})
    assert resp.status_code == 401


def test_integrations_api_not_configured_returns_503(monkeypatch, crm_service):
    monkeypatch.setattr(settings, "integrations_api_token", None)
    app = FastAPI()
    app.include_router(integrations_router)
    app.dependency_overrides[get_crm_service] = lambda: crm_service

    with TestClient(app) as client:
        resp = client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())
    assert resp.status_code == 503


def test_correct_token_with_no_matching_contact_still_requires_no_further_auth(test_client):
    """Sanity: a correctly-authenticated request for a genuinely absent
    Contact must succeed at the AUTH layer (never itself an auth failure)."""
    resp = test_client.get(ROUTE, params={"email": "nobody@example.com"}, headers=auth_headers())
    assert resp.status_code == 200


# --- happy path / found-vs-not-found semantics --------------------------


async def test_contact_with_photo_returns_found_true_and_the_url(test_client, crm_service, monkeypatch):
    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com")
    contact = make_contact(email="ada@example.com", profile_photo_key="avatars/11111111-1111-1111-1111-111111111111.jpg")
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["profile_photo_url"] == "https://photos.astronomicconnect.com/avatars/11111111-1111-1111-1111-111111111111.jpg"


async def test_contact_without_photo_returns_found_true_and_null_url(test_client, crm_service):
    contact = make_contact(email="ada@example.com")
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["profile_photo_url"] is None


def test_nonexistent_contact_returns_found_false_and_null_url(test_client):
    resp = test_client.get(ROUTE, params={"email": "nobody@example.com"}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["profile_photo_url"] is None


async def test_contact_with_a_cdn_base_url_unset_still_returns_null_not_broken(test_client, crm_service, monkeypatch):
    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", None)
    contact = make_contact(email="ada@example.com", profile_photo_key="avatars/11111111-1111-1111-1111-111111111111.jpg")
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())

    assert resp.json() == {"found": True, "profile_photo_url": None}


# --- email normalization -------------------------------------------------


async def test_email_lookup_is_case_and_whitespace_insensitive(test_client, crm_service, monkeypatch):
    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com")
    contact = make_contact(email="Ada.Lovelace@Example.com", profile_photo_key="avatars/22222222-2222-2222-2222-222222222222.jpg")
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "  ADA.LOVELACE@EXAMPLE.COM  "}, headers=auth_headers())

    assert resp.status_code == 200
    assert resp.json()["found"] is True
    assert resp.json()["profile_photo_url"] is not None


# --- blank/malformed email -----------------------------------------------


def test_blank_email_is_rejected_with_422(test_client):
    resp = test_client.get(ROUTE, params={"email": ""}, headers=auth_headers())
    assert resp.status_code == 422


def test_whitespace_only_email_is_rejected_with_422(test_client):
    resp = test_client.get(ROUTE, params={"email": "   "}, headers=auth_headers())
    assert resp.status_code == 422


def test_missing_email_query_param_entirely_is_rejected_with_422(test_client):
    resp = test_client.get(ROUTE, headers=auth_headers())
    assert resp.status_code == 422


# --- response shape / no data leakage ------------------------------------


async def test_response_contains_exactly_the_two_approved_fields(test_client, crm_service):
    contact = make_contact(email="ada@example.com", profile_photo_key="avatars/33333333-3333-3333-3333-333333333333.jpg")
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())

    assert set(resp.json().keys()) == {"found", "profile_photo_url"}


async def test_endpoint_never_leaks_any_other_contact_data(test_client, crm_service):
    """A Contact carrying rich, sensitive data everywhere -- name, phone,
    LinkedIn, investor custom fields, notes -- must never surface any of
    it through this route, no matter how it's serialized."""
    contact = make_contact(
        email="ada@example.com",
        first_name="Ada",
        last_name="Lovelace",
        phone="+15551234567",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace-secret",
        profile_photo_key="avatars/44444444-4444-4444-4444-444444444444.jpg",
        custom_fields={"investor_type": ["Angel Investor"], "notes": "Extremely sensitive private note"},
    )
    await crm_service.contact_store.create(contact)

    resp = test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())
    raw_text = resp.text

    for leaked_value in [
        "Ada", "Lovelace", "+15551234567", "ada-lovelace-secret",
        "Angel Investor", "Extremely sensitive private note", contact.crm_contact_id,
    ]:
        assert leaked_value not in raw_text


async def test_endpoint_makes_no_contact_mutation(test_client, crm_service):
    contact = make_contact(email="ada@example.com")
    await crm_service.contact_store.create(contact)
    before = await crm_service.contact_store.get(contact.crm_contact_id)

    test_client.get(ROUTE, params={"email": "ada@example.com"}, headers=auth_headers())

    after = await crm_service.contact_store.get(contact.crm_contact_id)
    assert before == after
