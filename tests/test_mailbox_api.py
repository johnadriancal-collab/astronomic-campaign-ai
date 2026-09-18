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
from app.dependencies import get_mailbox_metrics_service, get_mailbox_service
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mailbox_credential_store import MemoryMailboxCredentialStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services import token_encryption
from app.services.mailbox_metrics_service import MailboxMetricsService
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
def mailbox_metrics_service():
    return MailboxMetricsService(
        campaign_store=MemoryMailCampaignStore(),
        channel_store=MemoryMailCampaignMailboxStore(),
        enrollment_store=MemoryMailEnrollmentStore(),
        enrollment_step_store=MemoryMailEnrollmentStepStore(),
    )


@pytest.fixture
def client(mailbox_service, mailbox_metrics_service):
    app = FastAPI()
    app.include_router(mailboxes_router)
    app.dependency_overrides[get_mailbox_service] = lambda: mailbox_service
    app.dependency_overrides[get_mailbox_metrics_service] = lambda: mailbox_metrics_service
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


# --- Gmail-send upgrade start route ------------------------------------------


def test_gmail_send_upgrade_start_missing_mailbox_returns_404(client):
    resp = client.get("/mailboxes/does-not-exist/google/gmail-send/start")

    assert resp.status_code == 404


def test_gmail_send_upgrade_start_returns_an_authorize_url_for_a_real_mailbox(client):
    mailbox = _connect(client)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-send/start")

    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://accounts.google.com")


def test_gmail_send_upgrade_start_requests_base_scopes_plus_gmail_send(client, oauth_client):
    mailbox = _connect(client)
    oauth_client.requested_scopes.clear()

    client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-send/start")

    assert oauth_client.requested_scopes == [
        (
            "openid", "email", "profile",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.metadata",
            "https://www.googleapis.com/auth/gmail.readonly",
        )
    ]


def test_gmail_send_upgrade_start_never_mutates_the_mailbox(client):
    """Initiation only registers a pending OAuth state -- see
    MailboxService.begin_gmail_send_upgrade()'s own docstring. Every
    actual scope/status change happens exclusively inside the callback.
    `authorized_age_seconds` is excluded from the comparison -- it's a
    live, computed-at-read-time value (see MailboxListItem) that ticks
    up between the two GET calls even with zero mailbox mutation."""
    mailbox = _connect(client)
    before = client.get("/mailboxes").json()[0]

    client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-send/start")

    after = client.get("/mailboxes").json()[0]
    before.pop("authorized_age_seconds")
    after.pop("authorized_age_seconds")
    assert after == before
    assert "gmail.send" not in " ".join(after["granted_scopes"])
    assert after["status"] == "connected"


def test_gmail_send_upgrade_start_not_configured_returns_503(client, mailbox_service):
    from app.google.oauth_client import GoogleOAuthClient

    mailbox = _connect(client)
    mailbox_service.oauth_client = GoogleOAuthClient()  # real client, no settings configured

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-send/start")

    assert resp.status_code == 503


def test_gmail_send_upgrade_pending_state_carries_the_correct_flow_type_and_mailbox(client, mailbox_service):
    from app.services.mailbox_service import OAuthFlowType

    mailbox = _connect(client)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-send/start")
    state = resp.json()["authorize_url"].split("state=")[1]

    pending = mailbox_service._pending_states[state]
    assert pending.flow_type == OAuthFlowType.GMAIL_SEND_UPGRADE
    assert pending.expected_mailbox_id == mailbox["mailbox_id"]


def test_ordinary_connect_flow_remains_base_scope_only_after_upgrade_route_exists(client, oauth_client):
    """Regression guard: adding the upgrade route must never change what
    the ORDINARY connect flow requests."""
    oauth_client.requested_scopes.clear()

    client.get("/mailboxes/google/start")

    assert oauth_client.requested_scopes == [("openid", "email", "profile")]


# --- Gmail routine reconnect start route (2026-09-17) ------------------------


def test_gmail_reconnect_start_missing_mailbox_returns_404(client):
    resp = client.get("/mailboxes/does-not-exist/google/gmail-reconnect/start")

    assert resp.status_code == 404


def test_gmail_reconnect_start_returns_an_authorize_url_for_a_real_mailbox(client):
    mailbox = _connect(client)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-reconnect/start")

    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://accounts.google.com")


def test_gmail_reconnect_start_requests_only_the_mailboxs_own_current_scopes(client, oauth_client):
    """A freshly-connected mailbox only has the base three scopes -- the
    reconnect route must request exactly those, never the full upgrade
    set, even though this same mailbox_id could separately be sent
    through /google/gmail-send/start for real capability escalation."""
    mailbox = _connect(client)
    oauth_client.requested_scopes.clear()

    client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-reconnect/start")

    assert oauth_client.requested_scopes == [("email", "openid", "profile")]


def test_gmail_reconnect_start_never_mutates_the_mailbox(client):
    mailbox = _connect(client)
    before = client.get("/mailboxes").json()[0]

    client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-reconnect/start")

    after = client.get("/mailboxes").json()[0]
    assert after["granted_scopes"] == before["granted_scopes"]
    assert after["status"] == "connected"


def test_gmail_reconnect_pending_state_carries_the_correct_flow_type_and_mailbox(client, mailbox_service):
    from app.services.mailbox_service import OAuthFlowType

    mailbox = _connect(client)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-reconnect/start")
    state = resp.json()["authorize_url"].split("state=")[1]

    pending = mailbox_service._pending_states[state]
    assert pending.flow_type == OAuthFlowType.GMAIL_RECONNECT
    assert pending.expected_mailbox_id == mailbox["mailbox_id"]


def test_gmail_reconnect_callback_missing_refresh_token_redirects_with_reconnect_error_code(client, oauth_client):
    mailbox = _connect(client)
    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/google/gmail-reconnect/start")
    state = resp.json()["authorize_url"].split("state=")[1]
    oauth_client.token_response = {"access_token": "fake-access-token", "scope": "openid email profile"}  # no refresh_token

    callback_resp = client.get(
        "/mailboxes/google/callback", params={"code": "fake-code", "state": state}, follow_redirects=False
    )

    assert callback_resp.status_code in (302, 307)
    assert "error=reconnect_needs_retry" in callback_resp.headers["location"]


# --- GET /mailboxes' authorization-health fields (2026-09-17) ----------------


def test_list_mailboxes_includes_authorization_health_fields(client):
    mailbox = _connect(client)

    assert mailbox["authorization_health"] == "connected"
    assert mailbox["authorized_at"] is not None
    assert mailbox["authorized_at_is_estimated"] is False
    assert mailbox["estimated_expires_at"] is not None


def test_disconnected_mailbox_has_null_authorization_health(client):
    mailbox = _connect(client)
    client.post(f"/mailboxes/{mailbox['mailbox_id']}/disconnect")

    after = client.get("/mailboxes").json()[0]
    assert after["authorization_health"] is None


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


# --- Gmail read-only diagnostics (2026-09-15) --------------------------------


class FakeGmailThreadReaderClient:
    def __init__(self, thread_result=None, message_result=None, raise_error=None):
        self.thread_result = thread_result
        self.message_result = message_result
        self.raise_error = raise_error
        self.thread_calls: list[dict] = []
        self.message_calls: list[dict] = []

    async def get_thread(self, *, access_token: str, thread_id: str) -> dict:
        self.thread_calls.append({"access_token": access_token, "thread_id": thread_id})
        if self.raise_error is not None:
            raise self.raise_error
        return self.thread_result

    async def get_message(self, *, access_token: str, message_id: str) -> dict:
        self.message_calls.append({"access_token": access_token, "message_id": message_id})
        if self.raise_error is not None:
            raise self.raise_error
        return self.message_result


def _message_shape(message_id: str, thread_id: str, headers: dict) -> dict:
    return {"id": message_id, "threadId": thread_id, "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]}}


def test_gmail_diagnostic_thread_returns_headers_only_never_body(client, monkeypatch):
    mailbox = _connect(client)
    fake_reader = FakeGmailThreadReaderClient(
        thread_result={
            "id": "thr-1",
            "messages": [
                _message_shape("msg-1", "thr-1", {"Subject": "Hi", "Message-ID": "<a@x.com>"}),
                _message_shape("msg-2", "thr-1", {"Subject": "Hi", "Message-ID": "<b@x.com>", "In-Reply-To": "<a@x.com>"}),
            ],
        }
    )
    monkeypatch.setattr("app.api.mailboxes.GmailThreadReaderClient", lambda: fake_reader)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/gmail-diagnostic/threads/thr-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == "thr-1"
    assert body["message_count"] == 2
    assert body["messages"][0]["headers"]["Subject"] == "Hi"
    assert body["messages"][1]["headers"]["In-Reply-To"] == "<a@x.com>"
    # Structurally the only keys ever present -- gmail.metadata cannot
    # return message body/snippet content at all, and extract_headers()
    # only ever pulls the fixed METADATA_HEADERS allowlist.
    for message in body["messages"]:
        assert set(message.keys()) == {"id", "threadId", "headers"}
    assert fake_reader.thread_calls == [{"access_token": "fake-refreshed-access-token", "thread_id": "thr-1"}]


def test_gmail_diagnostic_message_returns_headers_only(client, monkeypatch):
    mailbox = _connect(client)
    fake_reader = FakeGmailThreadReaderClient(
        message_result=_message_shape("msg-2", "thr-1", {"Subject": "Hi", "From": "a@x.com"})
    )
    monkeypatch.setattr("app.api.mailboxes.GmailThreadReaderClient", lambda: fake_reader)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/gmail-diagnostic/messages/msg-2")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"id": "msg-2", "threadId": "thr-1", "headers": {"Subject": "Hi", "From": "a@x.com"}}


def test_gmail_diagnostic_missing_mailbox_returns_404(client):
    resp = client.get("/mailboxes/does-not-exist/gmail-diagnostic/threads/thr-1")
    assert resp.status_code == 404


def test_gmail_diagnostic_invalid_grant_returns_409(client, oauth_client):
    mailbox = _connect(client)
    oauth_client.refresh_outcome = "invalid_grant"

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/gmail-diagnostic/threads/thr-1")

    assert resp.status_code == 409


def test_gmail_diagnostic_gmail_read_error_returns_502(client, monkeypatch):
    from app.google.gmail_thread_reader_client import GmailReadNotFoundError

    mailbox = _connect(client)
    fake_reader = FakeGmailThreadReaderClient(raise_error=GmailReadNotFoundError("no such thread"))
    monkeypatch.setattr("app.api.mailboxes.GmailThreadReaderClient", lambda: fake_reader)

    resp = client.get(f"/mailboxes/{mailbox['mailbox_id']}/gmail-diagnostic/threads/does-not-exist")

    assert resp.status_code == 502


def test_gmail_diagnostic_never_logs_the_access_token(client, monkeypatch):
    mailbox = _connect(client)
    fake_reader = FakeGmailThreadReaderClient(thread_result={"id": "thr-1", "messages": []})
    monkeypatch.setattr("app.api.mailboxes.GmailThreadReaderClient", lambda: fake_reader)

    logged_messages: list[str] = []
    sink_id = loguru_logger.add(lambda message: logged_messages.append(str(message)), level="DEBUG")
    try:
        client.get(f"/mailboxes/{mailbox['mailbox_id']}/gmail-diagnostic/threads/thr-1")
    finally:
        loguru_logger.remove(sink_id)

    for message in logged_messages:
        assert "fake-refreshed-access-token" not in message
