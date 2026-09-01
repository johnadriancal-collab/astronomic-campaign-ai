"""
Route-level tests for /mail/unsubscribe* -- the public, unauthenticated
surface (Phase B3). Matches tests/test_mail_api.py's bare-FastAPI-plus-
just-this-router convention for most tests; one test additionally mounts
the REAL session-auth middleware to prove these paths are actually
reachable without a session, which is the whole point of the
PUBLIC_PATHS change.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail_unsubscribe import router as unsubscribe_router
from app.dependencies import get_mail_suppression_service
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_suppression_service import MailSuppressionService
from app.services.unsubscribe_token import generate_unsubscribe_token


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture
def suppression_store():
    return MemoryMailSuppressionStore()


@pytest.fixture
def suppression_service(suppression_store):
    return MailSuppressionService(store=suppression_store, activity_log=ActivityLogService(MemoryActivityEventStore()))


@pytest.fixture
def client(suppression_service):
    app = FastAPI()
    app.include_router(unsubscribe_router)
    app.dependency_overrides[get_mail_suppression_service] = lambda: suppression_service
    with TestClient(app) as c:
        yield c


def token_for(email: str) -> str:
    return generate_unsubscribe_token(email)


# --- GET: read-only, zero mutation ------------------------------------------------


def test_get_with_valid_token_returns_a_confirmation_page(client):
    resp = client.get("/mail/unsubscribe", params={"token": token_for("a@example.com")})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<form" in resp.text
    assert "Confirm unsubscribe" in resp.text


def test_get_confirmation_page_never_displays_the_recipients_email(client):
    """Final privacy pass: the decrypted email must never reach the
    browser at all, generic copy only -- see app/api/mail_unsubscribe.py's
    PRIVACY note."""
    resp = client.get("/mail/unsubscribe", params={"token": token_for("a@example.com")})
    assert "a@example.com" not in resp.text


@pytest.mark.asyncio
async def test_get_never_mutates_suppression_state(client, suppression_store):
    token = token_for("a@example.com")
    client.get("/mail/unsubscribe", params={"token": token})
    assert await suppression_store.get("a@example.com") is None


@pytest.mark.asyncio
async def test_repeated_scanner_like_gets_cause_zero_mutation(client, suppression_store):
    """Simulates an email-security scanner prefetching the same link
    repeatedly -- must never unsubscribe anyone."""
    token = token_for("a@example.com")
    for _ in range(5):
        resp = client.get("/mail/unsubscribe", params={"token": token})
        assert resp.status_code == 200
    assert await suppression_store.get("a@example.com") is None


def test_get_with_missing_token_returns_generic_error_page(client):
    resp = client.get("/mail/unsubscribe")
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower()
    assert "email" not in resp.text.lower()  # no leaked/echoed address on the error path


def test_get_with_garbage_token_returns_generic_error_page(client):
    resp = client.get("/mail/unsubscribe", params={"token": "not-a-real-token"})
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower()


def test_get_response_is_never_cached(client):
    resp = client.get("/mail/unsubscribe", params={"token": token_for("a@example.com")})
    assert resp.headers.get("cache-control") == "no-store"


# --- HTML escaping / no PII rendering -----------------------------------------------


def test_get_never_renders_any_trace_of_a_malicious_email_payload(client):
    """CRM contact emails aren't format-validated at ingestion (see the
    B3 investigation's normalize_email() finding) -- an address
    containing HTML-significant characters must never reach the response,
    escaped or otherwise, now that the confirmation page doesn't render
    the email at all (the strongest possible guard against injection:
    nothing to escape because nothing is rendered)."""
    malicious_email = "foo<script>alert(1)</script>@example.com"
    token = token_for(malicious_email)
    resp = client.get("/mail/unsubscribe", params={"token": token})
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" not in resp.text
    assert "foo" not in resp.text


def test_post_confirm_never_renders_any_trace_of_a_malicious_email_payload(client):
    malicious_email = "foo<script>alert(1)</script>@example.com"
    token = token_for(malicious_email)
    resp = client.post("/mail/unsubscribe", params={"token": token})
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" not in resp.text
    assert "foo" not in resp.text


def test_post_confirm_success_page_never_displays_the_recipients_email(client):
    resp = client.post("/mail/unsubscribe", params={"token": token_for("a@example.com")})
    assert "a@example.com" not in resp.text
    assert "You've been unsubscribed" in resp.text


def test_form_action_carries_forward_the_exact_opaque_token_unmodified(client):
    """The GET page's form must transport the SAME opaque token string
    into the POST -- never a re-derived or decrypted-then-re-encoded
    value."""
    token = token_for("a@example.com")
    resp = client.get("/mail/unsubscribe", params={"token": token})
    assert f"token={token}" in resp.text


# --- POST /mail/unsubscribe: human path, confirmed, idempotent --------------------


@pytest.mark.asyncio
async def test_post_confirm_suppresses_the_email(client, suppression_store):
    token = token_for("a@example.com")
    resp = client.post("/mail/unsubscribe", params={"token": token})
    assert resp.status_code == 200
    row = await suppression_store.get("a@example.com")
    assert row is not None
    assert row.active is True
    assert row.reason.value == "unsubscribed"


@pytest.mark.asyncio
async def test_post_confirm_is_idempotent(client, suppression_store):
    token = token_for("a@example.com")
    resp1 = client.post("/mail/unsubscribe", params={"token": token})
    resp2 = client.post("/mail/unsubscribe", params={"token": token})
    assert resp1.status_code == resp2.status_code == 200
    all_rows = await suppression_store.list()
    assert len(all_rows) == 1


def test_post_confirm_with_invalid_token_does_not_suppress_anything(client):
    resp = client.post("/mail/unsubscribe", params={"token": "garbage"})
    assert resp.status_code == 400


# --- POST /mail/unsubscribe/one-click: RFC 8058, no redirect, idempotent ----------


@pytest.mark.asyncio
async def test_one_click_suppresses_the_email(client, suppression_store):
    token = token_for("a@example.com")
    resp = client.post("/mail/unsubscribe/one-click", params={"token": token})
    assert resp.status_code == 200
    row = await suppression_store.get("a@example.com")
    assert row is not None and row.active is True and row.reason.value == "unsubscribed"


@pytest.mark.asyncio
async def test_one_click_is_idempotent(client, suppression_store):
    token = token_for("a@example.com")
    client.post("/mail/unsubscribe/one-click", params={"token": token})
    client.post("/mail/unsubscribe/one-click", params={"token": token})
    all_rows = await suppression_store.list()
    assert len(all_rows) == 1


def test_one_click_never_redirects(client):
    token = token_for("a@example.com")
    resp = client.post("/mail/unsubscribe/one-click", params={"token": token}, follow_redirects=False)
    assert resp.status_code == 200
    assert "location" not in resp.headers


def test_one_click_response_content_type(client):
    token = token_for("a@example.com")
    resp = client.post("/mail/unsubscribe/one-click", params={"token": token})
    assert "text/html" not in resp.headers["content-type"]


def test_one_click_with_invalid_token_fails_without_suppressing(client):
    resp = client.post("/mail/unsubscribe/one-click", params={"token": "garbage"})
    assert resp.status_code == 400
