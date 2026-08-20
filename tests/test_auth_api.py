"""
Route-level tests for /auth/* -- exercises just the auth router against a
fresh FastAPI app, matching this suite's established isolation pattern
(see e.g. tests/test_mailbox_api.py).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from app.api.auth import router as auth_router
from app.dependencies import get_auth_service
from app.repositories.auth_session_store import MemoryAuthSessionStore
from app.services import auth_service as auth_service_module
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.services.password_hashing import hash_password

REAL_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def configured_credentials(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)  # TestClient isn't real HTTPS


@pytest.fixture
def auth_svc():
    return AuthService(session_store=MemoryAuthSessionStore())


@pytest.fixture
def client(auth_svc):
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    with TestClient(app) as c:
        yield c


def test_login_with_correct_credentials_succeeds(client):
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True}


def test_login_sets_an_httponly_cookie(client):
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_login_with_wrong_password_is_rejected(client):
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": "wrong"})

    assert resp.status_code == 401


def test_login_with_wrong_email_is_rejected(client):
    resp = client.post("/auth/login", json={"email": "nobody@astronomic.com", "password": REAL_PASSWORD})

    assert resp.status_code == 401


def test_login_response_never_contains_the_password(client):
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    assert REAL_PASSWORD not in resp.text


def test_login_when_not_configured_returns_503(client, monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", None)

    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    assert resp.status_code == 503


def test_session_check_with_no_cookie_is_unauthenticated(client):
    resp = client.get("/auth/session")

    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_session_check_after_login_is_authenticated(client):
    client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})

    resp = client.get("/auth/session")

    assert resp.json() == {"authenticated": True}


def test_logout_clears_the_session(client):
    client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})
    assert client.get("/auth/session").json() == {"authenticated": True}

    logout_resp = client.post("/auth/logout")

    assert logout_resp.status_code == 200
    assert client.get("/auth/session").json() == {"authenticated": False}


def test_logout_with_no_active_session_still_succeeds(client):
    resp = client.post("/auth/logout")

    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False}


def test_login_never_logs_the_password():
    """Uses a real loguru sink (caplog does not capture loguru output) --
    same pattern as tests/test_mailbox_api.py's token-logging check."""
    app = FastAPI()
    app.include_router(auth_router)
    svc = AuthService(session_store=MemoryAuthSessionStore())
    app.dependency_overrides[get_auth_service] = lambda: svc

    logged_messages: list[str] = []
    sink_id = loguru_logger.add(lambda message: logged_messages.append(str(message)), level="DEBUG")
    try:
        with TestClient(app) as client:
            client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})
    finally:
        loguru_logger.remove(sink_id)

    for message in logged_messages:
        assert REAL_PASSWORD not in message
