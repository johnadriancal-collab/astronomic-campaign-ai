"""
Route-level tests for POST /sync/luma-event and POST /sync/luma-backfill.
Mounts the REAL luma router AND the REAL session_auth_middleware (not a
stand-in) so these prove exactly what's deployed, matching
tests/test_astro_ai_api.py's own convention.
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.luma import router as luma_router
from app.dependencies import get_auth_service, get_luma_sync_service
from app.models.crm import CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaBackfillStatus
from app.repositories.auth_session_store import MemoryAuthSessionStore
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncService
from app.services.password_hashing import hash_password
from app.session_auth_middleware import enforce_session_auth
from tests.test_luma_sync_service import make_event, make_guest

REAL_PASSWORD = "correct horse battery staple"
WEBHOOK_SECRET = "whsec_test_secret"


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)


@pytest.fixture(autouse=True)
def configured_luma_webhook_secret(monkeypatch):
    from app.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, "luma_webhook_secret", WEBHOOK_SECRET)


@pytest.fixture
def auth_svc():
    return AuthService(session_store=MemoryAuthSessionStore())


@pytest_asyncio.fixture
async def luma_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="investor_type",
            label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["Angel Investor"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    crm_service = CrmService(custom_field_store=custom_field_store)
    return LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(),
        luma_client=None,
    )


@pytest.fixture
def client(luma_service, auth_svc):
    app = FastAPI()
    app.middleware("http")(enforce_session_auth)
    app.state.auth_service = auth_svc
    app.include_router(auth_router)
    app.include_router(luma_router)
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    app.dependency_overrides[get_luma_sync_service] = lambda: luma_service
    with TestClient(app) as c:
        yield c


def _login(client) -> None:
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})
    assert resp.status_code == 200


def _webhook_body(event_type="guest.registered", guest_id="gst-1") -> bytes:
    data = {**make_guest(guest_id=guest_id), "event": make_event()}
    return json.dumps({"type": event_type, "data": data}).encode("utf-8")


# --- webhook signature enforcement -------------------------------------------


def test_webhook_with_valid_signature_is_accepted(client):
    body = _webhook_body()
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)

    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_with_invalid_signature_is_rejected(client):
    body = _webhook_body()
    timestamp = int(datetime.now(timezone.utc).timestamp())
    bad_signature = f"t={timestamp},v1=" + "0" * 64

    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": bad_signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_webhook_with_missing_signature_is_rejected(client):
    body = _webhook_body()
    resp = client.post(
        "/sync/luma-event", content=body, headers={"Webhook-Id": "wh-1", "Content-Type": "application/json"}
    )
    assert resp.status_code == 401


def test_webhook_with_stale_signature_is_rejected(client):
    from datetime import timedelta

    from app.luma.webhook_signature import MAX_SIGNATURE_AGE_SECONDS

    body = _webhook_body()
    stale_timestamp = int((datetime.now(timezone.utc) - timedelta(seconds=MAX_SIGNATURE_AGE_SECONDS + 120)).timestamp())
    signature = _sign(WEBHOOK_SECRET, stale_timestamp, body)

    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_webhook_unconfigured_secret_returns_503(client, monkeypatch):
    from app.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, "luma_webhook_secret", None)
    body = _webhook_body()
    resp = client.post(
        "/sync/luma-event", content=body, headers={"Webhook-Id": "wh-1", "Content-Type": "application/json"}
    )
    assert resp.status_code == 503


def test_webhook_missing_webhook_id_header_returns_400(client):
    body = _webhook_body()
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event", content=body, headers={"Webhook-Signature": signature, "Content-Type": "application/json"}
    )
    assert resp.status_code == 400


def test_webhook_route_never_requires_a_hub_session(client):
    """No _login() call here on purpose -- this route must work
    unauthenticated (Luma has no Hub session cookie), protected only by
    its signature."""
    body = _webhook_body()
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_webhook_id_over_the_real_api_is_a_no_op(client, luma_service):
    body1 = _webhook_body()
    timestamp1 = int(datetime.now(timezone.utc).timestamp())
    sig1 = _sign(WEBHOOK_SECRET, timestamp1, body1)
    client.post(
        "/sync/luma-event", content=body1, headers={"Webhook-Signature": sig1, "Webhook-Id": "wh-dup", "Content-Type": "application/json"}
    )

    # A second, differently-signed (fresh timestamp) delivery, but the SAME Webhook-Id,
    # carrying a DIFFERENT approval_status -- proves it's skipped purely on the
    # duplicate delivery id, not because the payload happens to be unchanged.
    body2 = json.dumps(
        {"type": "guest.updated", "data": {**make_guest(guest_id="gst-1", approval_status="declined"), "event": make_event()}}
    ).encode("utf-8")
    timestamp2 = int(datetime.now(timezone.utc).timestamp())
    sig2 = _sign(WEBHOOK_SECRET, timestamp2, body2)
    resp2 = client.post(
        "/sync/luma-event", content=body2, headers={"Webhook-Signature": sig2, "Webhook-Id": "wh-dup", "Content-Type": "application/json"}
    )
    assert resp2.status_code == 200

    registration = await luma_service.registration_store.get("gst-1")
    assert registration.approval_status.value == "approved"  # unchanged -- the "declined" delivery was never processed
    assert len(await luma_service.registration_store.list()) == 1


def test_malformed_json_body_returns_400(client):
    body = b"not json at all"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_unsupported_event_type_still_returns_200(client):
    """An out-of-scope-this-phase event type is a legitimate signed
    delivery we choose to ignore -- never a 4xx (that would make Luma
    retry a delivery retrying can't fix)."""
    body = json.dumps({"type": "event.created", "data": {"id": "evt-1", "name": "Something"}}).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200


# --- backfill route: session-authenticated, never public ---------------------


def test_backfill_route_requires_a_hub_session(client):
    resp = client.post("/sync/luma-backfill")
    assert resp.status_code == 401


def test_backfill_route_requires_a_configured_luma_client(client):
    _login(client)
    resp = client.post("/sync/luma-backfill")
    assert resp.status_code == 503  # luma_service fixture has luma_client=None
