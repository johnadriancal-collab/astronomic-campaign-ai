"""
Route-level tests for POST /sync/sale-onboarding -- auth, request/response
shape, and idempotency/conflict status-code mapping. Mirrors
tests/test_integrations_photo_api.py's conventions for testing this
codebase's shared-secret bearer-token pattern.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.sale_onboarding import get_sale_onboarding_service, router as sale_onboarding_router
from app.config import settings
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.crm_service import CrmService
from app.services.sale_onboarding_service import SaleOnboardingService

pytestmark = pytest.mark.asyncio

ROUTE = "/sync/sale-onboarding"


def _payload(**overrides) -> dict:
    defaults = dict(
        sale_id="sale-001",
        docusign_envelope_id="env-001",
        mercury_invoice_id="merc-001",
        qb_invoice_id="qb-inv-001",
        qb_customer_id="qb-cust-001",
        qb_amount=5000.0,
        client_company="Hive ASMBLD",
        primary_contact="Jane Doe",
        contact_email="jane@hiveasmbld.com",
        signer_name="John Roe",
        signer_email="john@hiveasmbld.com",
        service_sold="Investor Dinner",
        city="Austin",
        event_date="10/15/2026",
        internal_owner="Chris",
        referral_source="Warm intro",
        special_terms="Net 15",
    )
    defaults.update(overrides)
    return defaults


def auth_headers(token="test-sale-bot-token"):
    return {"Authorization": f"Bearer {token}"}


def _build_service() -> SaleOnboardingService:
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    luma_event_store = MemoryLumaEventStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    activity_log = ActivityLogService(store=MemoryActivityEventStore())
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    client_crm_service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        engagement_store=engagement_store,
        engagement_closeout_store=engagement_closeout_store,
        engagement_participant_store=engagement_participant_store,
        luma_event_store=luma_event_store,
        client_touchpoint_store=client_touchpoint_store,
        contact_engagement_signal_service=signal_service,
    )
    crm_service = CrmService(contact_store=crm_contact_store, activity_log=activity_log)
    return SaleOnboardingService(client_crm_service=client_crm_service, crm_service=crm_service)


@pytest.fixture
def onboarding_service():
    return _build_service()


@pytest.fixture
def test_client(monkeypatch, onboarding_service):
    monkeypatch.setattr(settings, "sale_bot_webhook_token", "test-sale-bot-token")

    app = FastAPI()
    app.include_router(sale_onboarding_router)
    app.dependency_overrides[get_sale_onboarding_service] = lambda: onboarding_service

    with TestClient(app) as client:
        yield client


# --- auth --------------------------------------------------------------


async def _assert_zero_writes(onboarding_service):
    client_crm_service = onboarding_service.client_crm_service
    assert await client_crm_service.client_store.list() == []
    assert await client_crm_service.crm_contact_store.list() == []


async def test_missing_token_is_rejected_with_401_and_makes_zero_writes(test_client, onboarding_service):
    resp = test_client.post(ROUTE, json=_payload())
    assert resp.status_code == 401
    await _assert_zero_writes(onboarding_service)


async def test_wrong_token_is_rejected_with_401_and_makes_zero_writes(test_client, onboarding_service):
    resp = test_client.post(ROUTE, json=_payload(), headers=auth_headers("wrong-token"))
    assert resp.status_code == 401
    await _assert_zero_writes(onboarding_service)


async def test_malformed_authorization_header_is_rejected_with_401_and_makes_zero_writes(test_client, onboarding_service):
    resp = test_client.post(ROUTE, json=_payload(), headers={"Authorization": "test-sale-bot-token"})
    assert resp.status_code == 401
    await _assert_zero_writes(onboarding_service)


async def test_sale_bot_webhook_not_configured_returns_503_and_makes_zero_writes(monkeypatch, onboarding_service):
    monkeypatch.setattr(settings, "sale_bot_webhook_token", None)
    app = FastAPI()
    app.include_router(sale_onboarding_router)
    app.dependency_overrides[get_sale_onboarding_service] = lambda: onboarding_service

    with TestClient(app) as client:
        resp = client.post(ROUTE, json=_payload(), headers=auth_headers())
    assert resp.status_code == 503
    await _assert_zero_writes(onboarding_service)


async def test_correct_token_is_accepted(test_client, onboarding_service):
    """The explicit positive case requirement 6 asks for, alongside the
    three rejection cases above: a syntactically well-formed, matching
    token is accepted (falls through to the route body, not rejected by
    auth) and DOES result in real writes -- the mirror image of every
    zero-writes assertion above."""
    resp = test_client.post(ROUTE, json=_payload(), headers=auth_headers())
    assert resp.status_code == 200
    client_crm_service = onboarding_service.client_crm_service
    assert len(await client_crm_service.client_store.list()) == 1
    assert len(await client_crm_service.crm_contact_store.list()) == 1


def test_auth_is_checked_before_the_request_body_is_processed(test_client):
    """A malformed/empty body with a bad token must fail on AUTH (401),
    never leak into a 422 body-validation error first."""
    resp = test_client.post(ROUTE, json={}, headers=auth_headers("wrong-token"))
    assert resp.status_code == 401


# --- happy path ----------------------------------------------------------


def test_correct_token_new_sale_returns_200_with_full_result(test_client):
    resp = test_client.post(ROUTE, json=_payload(), headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_processed"] is False
    assert body["created_new_client"] is True
    assert body["created_new_contact"] is True
    assert body["event_date_parsed"] is True
    assert body["warnings"] == []
    assert body["client_id"]
    assert body["engagement_id"]
    assert body["client_contact_id"]
    assert body["crm_contact_id"]


# --- idempotency / conflict status codes -----------------------------------


def test_repeated_request_returns_200_already_processed_not_a_duplicate(test_client):
    first = test_client.post(ROUTE, json=_payload(), headers=auth_headers())
    second = test_client.post(ROUTE, json=_payload(), headers=auth_headers())

    assert second.status_code == 200
    assert second.json()["already_processed"] is True
    assert second.json()["engagement_id"] == first.json()["engagement_id"]


def test_docusign_envelope_conflict_under_a_different_sale_id_returns_409(test_client):
    test_client.post(ROUTE, json=_payload(sale_id="sale-001", docusign_envelope_id="env-shared"), headers=auth_headers())
    resp = test_client.post(
        ROUTE, json=_payload(sale_id="sale-002", docusign_envelope_id="env-shared"), headers=auth_headers()
    )
    assert resp.status_code == 409


def test_missing_contact_email_returns_422_not_a_500(test_client):
    resp = test_client.post(ROUTE, json=_payload(contact_email=""), headers=auth_headers())
    assert resp.status_code == 422


# --- request shape ---------------------------------------------------------


def test_missing_required_field_is_rejected_with_422(test_client):
    payload = _payload()
    del payload["sale_id"]
    resp = test_client.post(ROUTE, json=payload, headers=auth_headers())
    assert resp.status_code == 422


def test_optional_fields_can_be_omitted(test_client):
    payload = _payload()
    del payload["referral_source"]
    del payload["special_terms"]
    del payload["internal_owner"]
    resp = test_client.post(ROUTE, json=payload, headers=auth_headers())
    assert resp.status_code == 200
