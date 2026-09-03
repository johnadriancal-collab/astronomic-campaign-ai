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


SERVICE_READ_TOKEN = "test-service-read-token-value-not-a-real-secret"


@pytest.fixture(autouse=True)
def configured_credentials(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)
    # Unset by default in every test unless a test explicitly opts in via the
    # `configured_service_read_token` fixture below -- matches production's
    # own "None until deliberately configured" default.
    monkeypatch.setattr(auth_service_module.settings, "admin_service_read_token", None)


@pytest.fixture
def configured_service_read_token(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "admin_service_read_token", SERVICE_READ_TOKEN)


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

    @app.get("/crm/contacts")  # stands in for a real CRM read route
    async def crm_contacts_read():
        return [{"crm_contact_id": "real-crm-data"}]

    @app.get("/crm/backup/export")  # stands in for the real full-database backup export
    async def crm_backup_export():
        return {"contacts": ["every-contact-in-the-database"]}

    @app.get("/crm/backup/something-nested")  # a hypothetical future route under the same excluded namespace
    async def crm_backup_nested():
        return {"ok": True}

    @app.get("/crm/backupfoo")  # NOT the backup namespace -- must not be mistakenly excluded
    async def crm_backupfoo():
        return {"crm_contact_id": "unrelated-route-that-merely-starts-with-the-same-letters"}

    @app.get("/crm/import/some-batch-id")  # stands in for the real raw CSV import batch route
    async def crm_import_batch():
        return {"rows": ["raw-csv-row-1", "raw-csv-row-2"]}

    @app.get("/crm/import/some-batch-id/nested")  # a hypothetical future route under the same excluded namespace
    async def crm_import_nested():
        return {"ok": True}

    @app.get("/crm/importfoo")  # NOT the import namespace -- must not be mistakenly excluded
    async def crm_importfoo():
        return {"crm_contact_id": "unrelated-route-that-merely-starts-with-the-same-letters"}

    @app.post("/crm/contacts")  # stands in for a real CRM write route
    async def crm_contacts_write():
        return {"crm_contact_id": "would-have-been-created"}

    @app.patch("/crm/contacts/some-id")  # stands in for a real CRM write route
    async def crm_contact_patch():
        return {"crm_contact_id": "would-have-been-modified"}

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


# --- admin/service read-only token (Phase 1) ----------------------------


def test_service_read_token_can_get_a_permitted_crm_endpoint(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/contacts", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 200
    assert resp.json() == [{"crm_contact_id": "real-crm-data"}]


@pytest.mark.parametrize("method", ["post", "patch"])
def test_service_read_token_gets_403_for_a_crm_write(client, configured_service_read_token, method):
    c, _svc = client
    path = "/crm/contacts" if method == "post" else "/crm/contacts/some-id"

    resp = getattr(c, method)(path, headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403
    assert "would-have-been" not in resp.text


def test_service_read_token_gets_403_outside_crm_scope(client, configured_service_read_token):
    """Same valid token, but /campaign is not under /crm/ -- must be
    rejected outright, not silently allowed just because the token itself
    checks out."""
    c, _svc = client

    resp = c.get("/campaign", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403
    assert "real-private-data" not in resp.text


def test_invalid_service_token_is_rejected(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/contacts", headers={"Authorization": "Bearer not-the-real-token"})

    assert resp.status_code == 401
    assert "real-crm-data" not in resp.text


def test_malformed_authorization_header_is_rejected(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/contacts", headers={"Authorization": SERVICE_READ_TOKEN})  # missing "Bearer " prefix

    assert resp.status_code == 401


def test_invalid_service_token_never_falls_through_to_a_valid_cookie(client, configured_service_read_token):
    """The core determinism requirement: presenting ANY Authorization
    header commits the request to the service-token auth mode -- even a
    genuinely logged-in browser session must not rescue a bad/out-of-
    scope service-token request."""
    c, _svc = client
    _login(c)  # this browser session is completely valid on its own

    resp = c.get("/crm/contacts", headers={"Authorization": "Bearer not-the-real-token"})

    assert resp.status_code == 401
    assert "real-crm-data" not in resp.text


def test_out_of_scope_service_token_never_falls_through_to_a_valid_cookie(client, configured_service_read_token):
    c, _svc = client
    _login(c)

    resp = c.post("/crm/contacts", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403
    assert "would-have-been" not in resp.text


def test_no_authorization_header_preserves_existing_cookie_behavior_unauthenticated(client, configured_service_read_token):
    """Regression guard: adding this whole mechanism must not change what
    happens when no Authorization header is sent at all, token configured
    or not."""
    c, _svc = client

    resp = c.get("/crm/contacts")

    assert resp.status_code == 401


def test_no_authorization_header_preserves_existing_cookie_behavior_authenticated(client, configured_service_read_token):
    c, _svc = client
    _login(c)

    resp = c.get("/crm/contacts")

    assert resp.status_code == 200
    assert resp.json() == [{"crm_contact_id": "real-crm-data"}]


def test_valid_browser_session_is_completely_unaffected_by_this_feature_existing(client, configured_service_read_token):
    """Every pre-existing cookie-auth test above already proves this
    implicitly (they all run with admin_service_read_token configured via
    the autouse fixture change), but this one states it explicitly as its
    own regression guard."""
    c, _svc = client
    _login(c)

    resp = c.get("/campaign")

    assert resp.status_code == 200
    assert resp.json() == [{"campaign_id": "real-private-data"}]


def test_service_token_attempt_fails_closed_when_unconfigured(client):
    """No `configured_service_read_token` fixture here -- admin_service_read_token
    is None (the autouse fixture's default), matching production before
    this feature is deliberately turned on. Same 503 "not configured"
    convention as every other webhook token in this codebase, not a 401 --
    an operator/deployment gap is distinguishable from a bad credential."""
    c, _svc = client

    resp = c.get("/crm/contacts", headers={"Authorization": "Bearer anything-at-all"})

    assert resp.status_code == 503


def test_service_token_never_appears_in_any_response_body(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/contacts", headers={"Authorization": "Bearer not-the-real-token"})

    assert SERVICE_READ_TOKEN not in resp.text


# --- /crm/backup is explicitly excluded from service-read scope ------------


def test_service_read_token_gets_403_for_backup_export(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/backup/export", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403
    assert "every-contact-in-the-database" not in resp.text


def test_service_read_token_gets_403_for_a_nested_backup_path(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/backup/something-nested", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403


def test_backupfoo_is_not_mistakenly_treated_as_the_backup_namespace(client, configured_service_read_token):
    """Precision guard: a hypothetical unrelated route that merely starts
    with the same characters as "/crm/backup" must NOT be excluded --
    only "/crm/backup" itself and paths under "/crm/backup/"."""
    c, _svc = client

    resp = c.get("/crm/backupfoo", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 200


def test_normal_session_auth_for_backup_routes_is_completely_unchanged(client, configured_service_read_token):
    """The exclusion applies ONLY to the service-read code path -- a
    logged-in browser session must retain exactly its pre-existing access
    to /crm/backup/export (unauthenticated still 401, authenticated still
    200), regardless of whether a service-read token is configured at
    all."""
    c, _svc = client

    unauthenticated = c.get("/crm/backup/export")
    assert unauthenticated.status_code == 401

    _login(c)
    authenticated = c.get("/crm/backup/export")
    assert authenticated.status_code == 200
    assert authenticated.json() == {"contacts": ["every-contact-in-the-database"]}


# --- /crm/import is also explicitly excluded from service-read scope -------


def test_service_read_token_gets_403_for_an_import_batch(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/import/some-batch-id", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403
    assert "raw-csv-row-1" not in resp.text


def test_service_read_token_gets_403_for_a_nested_import_path(client, configured_service_read_token):
    c, _svc = client

    resp = c.get("/crm/import/some-batch-id/nested", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403


def test_importfoo_is_not_mistakenly_treated_as_the_import_namespace(client, configured_service_read_token):
    """Precision guard: a hypothetical unrelated route that merely starts
    with the same characters as "/crm/import" must NOT be excluded --
    only "/crm/import" itself and paths under "/crm/import/"."""
    c, _svc = client

    resp = c.get("/crm/importfoo", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 200


def test_normal_session_auth_for_import_routes_is_completely_unchanged(client, configured_service_read_token):
    c, _svc = client

    unauthenticated = c.get("/crm/import/some-batch-id")
    assert unauthenticated.status_code == 401

    _login(c)
    authenticated = c.get("/crm/import/some-batch-id")
    assert authenticated.status_code == 200
    assert authenticated.json() == {"rows": ["raw-csv-row-1", "raw-csv-row-2"]}
