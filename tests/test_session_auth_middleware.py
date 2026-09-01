"""
Tests for app/session_auth_middleware.py -- the REAL security boundary
(see that module's docstring). Mounts the ACTUAL production middleware
function onto a small test app with representative routes, rather than
re-implementing similar logic here, so these tests prove exactly what's
deployed.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.dependencies import get_auth_service
from app.repositories.auth_session_store import MemoryAuthSessionStore
from app.services import auth_service as auth_service_module
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.services.password_hashing import hash_password
from app.session_auth_middleware import enforce_session_auth

REAL_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def configured_credentials(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)


@pytest.fixture
def auth_svc():
    return AuthService(session_store=MemoryAuthSessionStore())


@pytest.fixture
def client(auth_svc):
    app = FastAPI()
    app.middleware("http")(enforce_session_auth)
    app.state.auth_service = auth_svc  # the middleware reads this directly, matching main.py's real wiring
    app.include_router(auth_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/campaign")  # stands in for any real private data route
    async def campaign_list():
        return [{"campaign_id": "real-private-data"}]

    @app.get("/mailboxes/google/callback")  # stands in for the real OAuth callback
    async def oauth_callback():
        return {"ok": True}

    @app.get("/mailboxes/{mailbox_id}/google/gmail-send/start")  # stands in for the Gmail-send upgrade route
    async def gmail_send_upgrade_start(mailbox_id: str):
        return {"authorize_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}

    @app.post("/sync/itf-contact")  # stands in for the ITF webhook
    async def itf_webhook():
        return {"ok": True}

    @app.post("/sync/email-intake")  # stands in for the email-intake webhook
    async def email_intake_webhook():
        return {"ok": True}

    with TestClient(app) as c:
        yield c, auth_svc


def _login(client) -> None:
    client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})


# --- protected by default ----------------------------------------------


def test_unauthenticated_request_to_a_private_route_is_rejected(client):
    c, _svc = client

    resp = c.get("/campaign")

    assert resp.status_code == 401
    assert "real-private-data" not in resp.text


def test_authenticated_request_to_a_private_route_succeeds(client):
    c, _svc = client
    _login(c)

    resp = c.get("/campaign")

    assert resp.status_code == 200
    assert resp.json() == [{"campaign_id": "real-private-data"}]


def test_request_with_a_forged_cookie_value_is_rejected(client):
    c, _svc = client

    c.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")
    resp = c.get("/campaign")

    assert resp.status_code == 401


def test_request_with_an_expired_session_is_rejected(client):
    import asyncio
    from datetime import datetime, timedelta, timezone

    c, svc = client

    async def create_and_expire_session() -> str:
        raw_token, _ = await svc.create_session()
        session_hash = auth_service_module._hash_token(raw_token)
        stored = await svc.session_store.get(session_hash)
        await svc.session_store.create(
            stored.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        )
        return raw_token

    raw_token = asyncio.run(create_and_expire_session())

    c.cookies.set(SESSION_COOKIE_NAME, raw_token)
    resp = c.get("/campaign")

    assert resp.status_code == 401


# --- explicit public allowlist -------------------------------------------


def test_health_is_reachable_with_no_session(client):
    c, _svc = client

    resp = c.get("/health")

    assert resp.status_code == 200


def test_oauth_callback_is_reachable_with_no_session(client):
    """Google's redirect lands here directly -- it cannot possibly carry
    our session cookie (different origin), so this route must never
    require one."""
    c, _svc = client

    resp = c.get("/mailboxes/google/callback")

    assert resp.status_code == 200


def test_gmail_send_upgrade_start_requires_a_session(client):
    """Unlike the callback above, this route is reached by an ordinary
    same-origin fetch from an already-loaded (and therefore already-
    authenticated) Hub page -- it is NOT in PUBLIC_PATHS and must reject
    an unauthenticated request exactly like any other private route."""
    c, _svc = client

    resp = c.get("/mailboxes/some-mailbox-id/google/gmail-send/start")

    assert resp.status_code == 401


def test_gmail_send_upgrade_start_succeeds_once_authenticated(client):
    c, _svc = client
    _login(c)

    resp = c.get("/mailboxes/some-mailbox-id/google/gmail-send/start")

    assert resp.status_code == 200


def test_itf_webhook_is_reachable_with_no_session(client):
    c, _svc = client

    resp = c.post("/sync/itf-contact")

    assert resp.status_code == 200


def test_email_intake_webhook_is_reachable_with_no_session(client):
    c, _svc = client

    resp = c.post("/sync/email-intake")

    assert resp.status_code == 200


def test_login_itself_is_reachable_with_no_prior_session(client):
    c, _svc = client

    resp = c.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    assert resp.status_code == 200


def test_session_check_is_reachable_with_no_prior_session(client):
    c, _svc = client

    resp = c.get("/auth/session")

    assert resp.status_code == 200


def test_logout_is_reachable_with_no_prior_session(client):
    c, _svc = client

    resp = c.post("/auth/logout")

    assert resp.status_code == 200
