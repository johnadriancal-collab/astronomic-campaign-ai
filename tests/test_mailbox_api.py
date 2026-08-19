"""
Route-level tests for /mailboxes/* -- exercises just the mailboxes router
against a fresh FastAPI app with a fake Google OAuth client (see
tests/test_mailbox_service.py's FakeGoogleOAuthClient), never a real
network call. Also asserts, at the HTTP-response-body level, that no
refresh/access token or Authorization header value ever appears anywhere
in a response -- the strongest possible proof that the split between
Mailbox (public) and MailboxCredential (internal-only) actually holds at
the API boundary, not just in the Python type system.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from app.api.mailboxes import router as mailboxes_router
from app.config import settings
from app.dependencies import get_mailbox_service
from app.repositories.mailbox_credential_store import MemoryMailboxCredentialStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services import token_encryption
from app.services.mailbox_service import MailboxService
from tests.test_mailbox_service import FakeGoogleOAuthClient


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "frontend_origin", "https://astronomic-campaign-ai.vercel.app")


@pytest.fixture
def oauth_client():
    return FakeGoogleOAuthClient()


@pytest.fixture
def mailbox_service(oauth_client):
    return MailboxService(
        mailbox_store=MemoryMailboxStore(),
        credential_store=MemoryMailboxCredentialStore(),
        oauth_client=oauth_client,
    )


@pytest.fixture
def client(mailbox_service):
    app = FastAPI()
    app.include_router(mailboxes_router)
    app.dependency_overrides[get_mailbox_service] = lambda: mailbox_service
    with TestClient(app) as c:
        yield c


def _connect(client) -> dict:
    """Drives a full, real HTTP round trip through /google/start and
    /google/callback (using the fake oauth_client under the hood) and
    returns the connected Mailbox as JSON."""
    start_resp = client.get("/mailboxes/google/start")
    assert start_resp.status_code == 200
    authorize_url = start_resp.json()["authorize_url"]
    state = authorize_url.split("state=")[1]

    callback_resp = client.get(
        "/mailboxes/google/callback", params={"code": "fake-code", "state": state}, follow_redirects=False
    )
    assert callback_resp.status_code in (302, 307)

    return client.get("/mailboxes").json()[0]


def test_list_empty_returns_empty_array(client):
    resp = client.get("/mailboxes")

    assert resp.status_code == 200
    assert resp.json() == []


def test_google_start_returns_an_authorize_url(client):
    resp = client.get("/mailboxes/google/start")

    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://accounts.google.com")


def test_google_start_not_configured_returns_503(client, mailbox_service):
    from app.google.oauth_client import GoogleOAuthClient

    mailbox_service.oauth_client = GoogleOAuthClient()  # real client, no settings configured

    resp = client.get("/mailboxes/google/start")

    assert resp.status_code == 503


def test_callback_success_redirects_to_emails_with_connected_flag(client):
    start_resp = client.get("/mailboxes/google/start")
    state = start_resp.json()["authorize_url"].split("state=")[1]

    resp = client.get(
        "/mailboxes/google/callback", params={"code": "fake-code", "state": state}, follow_redirects=False
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://astronomic-campaign-ai.vercel.app/manager/emails?connected=1"


def test_callback_appears_in_the_mailbox_list_afterward(client):
    mailbox = _connect(client)

    assert mailbox["email"] == "chris@astronomic.io"
    assert mailbox["status"] == "connected"
    assert mailbox["provider"] == "google"


def test_callback_state_mismatch_redirects_with_error_code(client):
    resp = client.get(
        "/mailboxes/google/callback",
        params={"code": "fake-code", "state": "never-issued"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith("/manager/emails?error=state_mismatch")


def test_callback_access_denied_redirects_with_error_code(client):
    start_resp = client.get("/mailboxes/google/start")
    state = start_resp.json()["authorize_url"].split("state=")[1]

    resp = client.get(
        "/mailboxes/google/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )

    assert resp.headers["location"].endswith("/manager/emails?error=access_denied")


def test_callback_missing_code_redirects_with_error_code(client):
    start_resp = client.get("/mailboxes/google/start")
    state = start_resp.json()["authorize_url"].split("state=")[1]

    resp = client.get("/mailboxes/google/callback", params={"state": state}, follow_redirects=False)

    assert resp.headers["location"].endswith("/manager/emails?error=missing_code")


def test_callback_token_exchange_failure_redirects_with_error_code(client, oauth_client):
    oauth_client.exchange_should_fail = True
    start_resp = client.get("/mailboxes/google/start")
    state = start_resp.json()["authorize_url"].split("state=")[1]

    resp = client.get(
        "/mailboxes/google/callback", params={"code": "fake-code", "state": state}, follow_redirects=False
    )

    assert resp.headers["location"].endswith("/manager/emails?error=token_exchange_failed")


def test_callback_without_frontend_origin_configured_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "frontend_origin", None)

    resp = client.get("/mailboxes/google/callback", params={"code": "fake-code", "state": "anything"})

    assert resp.status_code == 503


def test_reconnecting_same_account_does_not_duplicate_via_http(client):
    first = _connect(client)

    start_resp = client.get("/mailboxes/google/start")
    state = start_resp.json()["authorize_url"].split("state=")[1]
    client.get("/mailboxes/google/callback", params={"code": "another-code", "state": state}, follow_redirects=False)

    all_mailboxes = client.get("/mailboxes").json()
    assert len(all_mailboxes) == 1
    assert all_mailboxes[0]["mailbox_id"] == first["mailbox_id"]


def test_disconnect_marks_mailbox_disconnected(client):
    mailbox = _connect(client)

    resp = client.post(f"/mailboxes/{mailbox['mailbox_id']}/disconnect")

    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"


def test_disconnect_missing_mailbox_returns_404(client):
    resp = client.post("/mailboxes/does-not-exist/disconnect")

    assert resp.status_code == 404


# --- tokens never appear in any API response --------------------------------


def test_no_response_body_ever_contains_the_refresh_or_access_token(client):
    mailbox = _connect(client)

    responses = [
        client.get("/mailboxes"),
        client.get("/mailboxes/google/start"),
        client.post(f"/mailboxes/{mailbox['mailbox_id']}/disconnect"),
    ]

    for resp in responses:
        body = resp.text
        assert "fake-refresh-token" not in body
        assert "fake-access-token" not in body


def test_mailbox_list_response_has_no_credential_shaped_field(client):
    _connect(client)

    body = client.get("/mailboxes").json()[0]

    assert "encrypted_refresh_token" not in body
    assert "refresh_token" not in body
    assert "access_token" not in body


# --- tokens never appear in logs --------------------------------------------
# This app logs via loguru (not stdlib logging), which pytest's built-in
# `caplog` fixture does not capture -- a plain caplog-based test here would
# silently pass without checking anything. Adding a real loguru sink for
# the duration of the test is the only way to actually observe what gets
# logged.


def test_connecting_and_disconnecting_never_logs_the_refresh_token(client):
    logged_messages: list[str] = []
    sink_id = loguru_logger.add(lambda message: logged_messages.append(str(message)), level="DEBUG")
    try:
        mailbox = _connect(client)
        client.post(f"/mailboxes/{mailbox['mailbox_id']}/disconnect")
    finally:
        loguru_logger.remove(sink_id)

    assert logged_messages, "expected at least the 'Mailbox connected'/'disconnected' log lines"
    for message in logged_messages:
        assert "fake-refresh-token" not in message
        assert "fake-access-token" not in message
