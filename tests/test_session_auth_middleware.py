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
SERVICE_OPERATOR_TOKEN = "test-service-operator-token-value-not-a-real-secret"


@pytest.fixture(autouse=True)
def configured_credentials(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)
    # Unset by default in every test unless a test explicitly opts in via the
    # `configured_service_read_token`/`configured_service_operator_token`
    # fixtures below -- matches production's own "None until deliberately
    # configured" default for BOTH service identities independently.
    monkeypatch.setattr(auth_service_module.settings, "admin_service_read_token", None)
    monkeypatch.setattr(auth_service_module.settings, "admin_service_operator_token", None)


@pytest.fixture
def configured_service_read_token(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "admin_service_read_token", SERVICE_READ_TOKEN)


@pytest.fixture
def configured_service_operator_token(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "admin_service_operator_token", SERVICE_OPERATOR_TOKEN)


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

    # --- Stand-ins for the admin/service OPERATOR token's scope (Phase 2) --

    @app.get("/mail/campaigns")
    async def mail_campaigns_list():
        return [{"mail_campaign_id": "real-campaign-data"}]

    @app.post("/mail/campaigns")
    async def mail_campaigns_create():
        return {"mail_campaign_id": "would-have-been-created"}

    @app.get("/mail/campaigns/{campaign_id}")
    async def mail_campaign_get(campaign_id: str):
        return {"mail_campaign_id": campaign_id}

    @app.patch("/mail/campaigns/{campaign_id}")
    async def mail_campaign_patch(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "edited": True}

    @app.post("/mail/campaigns/{campaign_id}/ready")
    async def mail_campaign_ready(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "ready"}

    @app.post("/mail/campaigns/{campaign_id}/unlock")
    async def mail_campaign_unlock(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "draft"}

    @app.post("/mail/campaigns/{campaign_id}/activate")  # deliberately EXCLUDED from operator scope
    async def mail_campaign_activate(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "active"}

    @app.post("/mail/campaigns/{campaign_id}/pause")  # deliberately EXCLUDED
    async def mail_campaign_pause(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "paused"}

    @app.post("/mail/campaigns/{campaign_id}/resume")  # deliberately EXCLUDED
    async def mail_campaign_resume(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "active"}

    @app.post("/mail/campaigns/{campaign_id}/archive")  # deliberately EXCLUDED
    async def mail_campaign_archive(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "status": "archived"}

    @app.get("/mail/campaigns/{campaign_id}/review")
    async def mail_campaign_review(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "ready": True}

    @app.get("/mail/campaigns/{campaign_id}/enrollments")
    async def mail_campaign_enrollments(campaign_id: str):
        return [{"enrollment_id": "real-enrollment-data"}]

    @app.get("/mail/campaigns/{campaign_id}/workload")
    async def mail_campaign_workload(campaign_id: str):
        return {"mail_campaign_id": campaign_id, "total": 0, "pending": 0, "active": 0, "paused": 0, "completed": 0, "suppressed": 0, "failed": 0}

    @app.get("/mail/campaigns/{campaign_id}/batches")
    async def mail_campaign_batches(campaign_id: str):
        return [{"batch_id": "real-batch-data"}]

    @app.post("/mail/campaigns/{campaign_id}/prospects")
    async def mail_campaign_add_prospects(campaign_id: str):
        return {"batch_id": "real-new-batch-data", "mail_campaign_id": campaign_id}

    @app.get("/mail/campaigns/{campaign_id}/channels")
    async def mail_campaign_channels_get(campaign_id: str):
        return ["mbx-1"]

    @app.put("/mail/campaigns/{campaign_id}/channels")
    async def mail_campaign_channels_put(campaign_id: str):
        return ["mbx-1"]

    @app.get("/mail/campaigns/{campaign_id}/schedule")
    async def mail_campaign_schedule_get(campaign_id: str):
        return {"timezone": "UTC", "windows": []}

    @app.put("/mail/campaigns/{campaign_id}/schedule")
    async def mail_campaign_schedule_put(campaign_id: str):
        return {"timezone": "UTC", "windows": []}

    @app.get("/mail/campaigns/{campaign_id}/steps")
    async def mail_campaign_steps_get(campaign_id: str):
        return [{"step_id": "real-step-data"}]

    @app.post("/mail/campaigns/{campaign_id}/steps")
    async def mail_campaign_steps_post(campaign_id: str):
        return {"step_id": "would-have-been-created"}

    @app.patch("/mail/campaigns/{campaign_id}/steps/{step_id}")
    async def mail_campaign_step_patch(campaign_id: str, step_id: str):
        return {"step_id": step_id, "edited": True}

    @app.delete("/mail/campaigns/{campaign_id}/steps/{step_id}")
    async def mail_campaign_step_delete(campaign_id: str, step_id: str):
        return [{"step_id": "remaining-step"}]

    @app.post("/mail/campaigns/{campaign_id}/steps/reorder")
    async def mail_campaign_steps_reorder(campaign_id: str):
        return [{"step_id": "reordered-step"}]

    @app.get("/mail/suppressions")  # deliberately EXCLUDED
    async def mail_suppressions_list():
        return [{"email": "real-suppression-data"}]

    @app.post("/mail/execution/{step_id}/resolve-sent")  # deliberately EXCLUDED
    async def mail_execution_resolve_sent(step_id: str):
        return {"applied": True}

    @app.get("/mailboxes")
    async def mailboxes_list():
        return [{"mailbox_id": "mbx-1", "email": "victoria@useastronomic.com"}]

    @app.post("/mailboxes/{mailbox_id}/disconnect")  # deliberately EXCLUDED
    async def mailbox_disconnect(mailbox_id: str):
        return {"mailbox_id": mailbox_id, "status": "disconnected"}

    @app.get("/crm/lists")  # deliberately EXCLUDED from the OPERATOR token (covered by the read token instead)
    async def crm_lists_list():
        return [{"list_id": "real-list-data"}]

    @app.post("/crm/lists")
    async def crm_lists_create():
        return {"list_id": "would-have-been-created"}

    @app.patch("/crm/lists/{list_id}")
    async def crm_list_patch(list_id: str):
        return {"list_id": list_id, "edited": True}

    @app.delete("/crm/lists/{list_id}")  # deliberately EXCLUDED -- whole-list deletion
    async def crm_list_delete(list_id: str):
        return {"list_id": list_id, "status": "would-have-been-deleted"}

    @app.post("/crm/lists/{list_id}/contacts/bulk-add")
    async def crm_list_bulk_add(list_id: str):
        return {"added": 1}

    @app.post("/crm/lists/{list_id}/contacts/bulk-remove")
    async def crm_list_bulk_remove(list_id: str):
        return {"removed": 1}

    @app.delete("/crm/lists/{list_id}/contacts/{contact_id}")
    async def crm_list_contact_remove(list_id: str, contact_id: str):
        return {"list_id": list_id, "contact_id": contact_id}

    @app.post("/crm/custom-fields")  # deliberately EXCLUDED
    async def crm_custom_fields_create():
        return {"field_id": "would-have-been-created"}

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


# =====================================================================
# Admin/service OPERATOR token (Phase 2, 2026-09-03)
# =====================================================================
#
# Mirrors the service-read section above in structure, plus a dedicated
# cross-scope-isolation block proving the two identities' allowlists
# never leak into each other. Every allowed action from the approved
# scope gets its own "CAN" test; every explicitly-excluded action gets
# its own "CANNOT" test -- matching the exact bullet list requested.

_OPERATOR_HEADERS = {"Authorization": f"Bearer {SERVICE_OPERATOR_TOKEN}"}


# --- CAN: campaign create/read/edit -----------------------------------------


def test_operator_can_list_and_create_campaigns(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mail/campaigns", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/mail/campaigns", headers=_OPERATOR_HEADERS).status_code == 200


def test_operator_can_read_and_edit_ordinary_campaign_configuration(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mail/campaigns/c1", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.patch("/mail/campaigns/c1", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: steps --------------------------------------------------------------


def test_operator_can_manage_steps(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mail/campaigns/c1/steps", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/mail/campaigns/c1/steps", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.patch("/mail/campaigns/c1/steps/s1", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.delete("/mail/campaigns/c1/steps/s1", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/mail/campaigns/c1/steps/reorder", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: schedule -------------------------------------------------------


def test_operator_can_configure_schedule(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mail/campaigns/c1/schedule", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.put("/mail/campaigns/c1/schedule", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: channel selection (already-connected mailboxes only) -------------


def test_operator_can_select_an_already_connected_mailbox(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mailboxes", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.get("/mail/campaigns/c1/channels", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.put("/mail/campaigns/c1/channels", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: CRM list membership for campaign audience -------------------------


def test_operator_can_create_and_manage_crm_list_membership(client, configured_service_operator_token):
    c, _svc = client

    assert c.post("/crm/lists", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.patch("/crm/lists/list-1", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/crm/lists/list-1/contacts/bulk-add", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/crm/lists/list-1/contacts/bulk-remove", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.delete("/crm/lists/list-1/contacts/contact-1", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: Mark Ready / Unlock ------------------------------------------------


def test_operator_can_mark_ready_and_unlock(client, configured_service_operator_token):
    c, _svc = client

    assert c.post("/mail/campaigns/c1/ready", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.post("/mail/campaigns/c1/unlock", headers=_OPERATOR_HEADERS).status_code == 200


# --- CAN: Activate / Pause (Phase 2 additions, 2026-09-03 -- a matched pair
# of safety gates; see app/session_auth_middleware.py's module docstring for
# why exactly these two, and not resume/archive, were approved) -------------


def test_operator_can_activate(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/activate", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 200


def test_operator_can_pause(client, configured_service_operator_token):
    """Pause is Activate's safe inverse -- approved specifically because
    it can only ever stop an already-ACTIVE campaign, never start one."""
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/pause", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 200


# --- CAN: state inspection for verification ---------------------------------


def test_operator_can_inspect_campaign_review_and_enrollment_state(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/mail/campaigns/c1/review", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.get("/mail/campaigns/c1/enrollments", headers=_OPERATOR_HEADERS).status_code == 200


def test_operator_can_read_workload_and_batches(client, configured_service_operator_token):
    """Phase 2 (2026-09-03): the two new read-only routes added in Stage
    2 -- workload/batch history are independent of lifecycle status, see
    MailCampaignWorkload's own docstring."""
    c, _svc = client

    assert c.get("/mail/campaigns/c1/workload", headers=_OPERATOR_HEADERS).status_code == 200
    assert c.get("/mail/campaigns/c1/batches", headers=_OPERATOR_HEADERS).status_code == 200


def test_read_token_cannot_read_workload_or_batches(client, configured_service_read_token):
    """Cross-scope isolation: the read token's scope is /crm/* only -- it
    must not reach these two new /mail/* routes just because they're
    read-only, same as every other /mail/* route it was already excluded
    from."""
    c, _svc = client

    assert c.get("/mail/campaigns/c1/workload", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"}).status_code == 403
    assert c.get("/mail/campaigns/c1/batches", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"}).status_code == 403


def test_operator_can_add_prospects(client, configured_service_operator_token):
    """Stage 3 (2026-09-03): CRM-List Add Prospects is operator-token
    eligible -- see MailCampaignService.add_prospects()'s own docstring.
    This stand-in route is source-agnostic (the real CSV-vs-CRM-List
    distinction is enforced by the request body's Literal type at the
    real /mail/campaigns/{id}/prospects route, not by this middleware
    scope check), so this test only proves the PATH+METHOD is in scope."""
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/prospects", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 200


def test_read_token_cannot_add_prospects(client, configured_service_read_token):
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/prospects", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403


# --- CANNOT: lifecycle transitions other than ready/unlock/activate/pause ---


def test_operator_cannot_resume(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/resume", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "active" not in resp.text


def test_operator_cannot_archive(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns/c1/archive", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "archived" not in resp.text


# --- CANNOT: mail sending/suppression administration ------------------------


def test_operator_cannot_change_mail_suppression(client, configured_service_operator_token):
    c, _svc = client

    resp = c.get("/mail/suppressions", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "real-suppression-data" not in resp.text


def test_operator_cannot_access_mail_execution_admin_operations(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/execution/step1/resolve-sent", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403


# --- CANNOT: mailbox OAuth connect/disconnect/upgrade -----------------------


def test_operator_cannot_connect_disconnect_or_upgrade_mailbox_oauth(client, configured_service_operator_token):
    """/mailboxes/google/callback itself is not tested here -- it's a
    PUBLIC_PATH reachable unconditionally (Google's redirect physically
    cannot carry any credential of ours), so an Authorization header
    never even reaches the scope check for that one exact path. The
    OTHER OAuth/connection-management routes ARE gated and must reject
    this token."""
    c, _svc = client

    assert c.get("/mailboxes/mbx-1/google/gmail-send/start", headers=_OPERATOR_HEADERS).status_code == 403
    resp = c.post("/mailboxes/mbx-1/disconnect", headers=_OPERATOR_HEADERS)
    assert resp.status_code == 403
    assert "disconnected" not in resp.text


# --- CANNOT: CRM contact writes / custom fields / Luma mappings -------------


def test_operator_cannot_write_crm_contacts(client, configured_service_operator_token):
    c, _svc = client

    assert c.post("/crm/contacts", headers=_OPERATOR_HEADERS).status_code == 403
    assert c.patch("/crm/contacts/some-id", headers=_OPERATOR_HEADERS).status_code == 403


def test_operator_cannot_change_custom_fields(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/crm/custom-fields", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403


def test_operator_cannot_delete_a_whole_crm_list(client, configured_service_operator_token):
    """Deliberately narrower than the approved 'creation/editing and
    membership' grant -- whole-list deletion was not requested."""
    c, _svc = client

    resp = c.delete("/crm/lists/list-1", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "would-have-been-deleted" not in resp.text


# --- CANNOT: backup/import raw-data surfaces --------------------------------


def test_operator_cannot_access_backup_or_import(client, configured_service_operator_token):
    c, _svc = client

    assert c.get("/crm/backup/export", headers=_OPERATOR_HEADERS).status_code == 403
    assert c.get("/crm/import/some-batch-id", headers=_OPERATOR_HEADERS).status_code == 403


# --- CANNOT: unrelated API surfaces ------------------------------------------


def test_operator_cannot_reach_unrelated_surfaces(client, configured_service_operator_token):
    """/auth/login, /auth/session, etc. are not tested here -- they are
    PUBLIC_PATHS reachable unconditionally by design (the login flow
    itself can't require you to already have a credential), so an
    Authorization header never reaches the scope check for those exact
    paths regardless of this feature. /campaign stands in for the
    ordinary gated surface this token must not reach."""
    c, _svc = client

    assert c.get("/campaign", headers=_OPERATOR_HEADERS).status_code == 403


# --- Invalid/malformed operator token ----------------------------------------


def test_invalid_operator_token_is_rejected(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns", headers={"Authorization": "Bearer not-the-real-operator-token"})

    assert resp.status_code == 401
    assert "would-have-been-created" not in resp.text


def test_malformed_operator_authorization_header_is_rejected(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns", headers={"Authorization": SERVICE_OPERATOR_TOKEN})  # missing "Bearer "

    assert resp.status_code == 401


def test_operator_token_never_appears_in_any_response_body(client, configured_service_operator_token):
    c, _svc = client

    resp = c.post("/mail/campaigns", headers={"Authorization": "Bearer not-the-real-operator-token"})

    assert SERVICE_OPERATOR_TOKEN not in resp.text


# --- CANNOT: an invalid/out-of-scope operator token never falls through ----


def test_invalid_operator_token_never_falls_through_to_a_valid_cookie(client, configured_service_operator_token):
    c, _svc = client
    _login(c)  # this browser session is completely valid on its own

    resp = c.post("/mail/campaigns", headers={"Authorization": "Bearer not-the-real-operator-token"})

    assert resp.status_code == 401
    assert "would-have-been-created" not in resp.text


def test_out_of_scope_operator_token_never_falls_through_to_a_valid_cookie(client, configured_service_operator_token):
    c, _svc = client
    _login(c)

    resp = c.post("/mail/campaigns/c1/resume", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "active" not in resp.text


def test_operator_fails_closed_when_unconfigured(client):
    """No `configured_service_operator_token` fixture -- both tokens are
    None (the autouse fixture's default). Same 503 convention as the
    read-only token's own version of this test."""
    c, _svc = client

    resp = c.post("/mail/campaigns", headers={"Authorization": "Bearer anything-at-all"})

    assert resp.status_code == 503


def test_operator_scope_works_when_only_the_operator_token_is_configured():
    """The read token being unset must not 503 an operator-token request
    -- the two identities' configuration is independent. Uses its own
    app instance (not the shared `client` fixture) purely so this test's
    intent -- 'operator configured, read NOT configured' -- is explicit
    in the test body rather than relying on fixture-ordering."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.repositories.auth_session_store import MemoryAuthSessionStore
    from app.services.auth_service import AuthService

    app = FastAPI()
    app.middleware("http")(enforce_session_auth)
    app.state.auth_service = AuthService(session_store=MemoryAuthSessionStore())

    @app.post("/mail/campaigns")
    async def mail_campaigns_create():
        return {"mail_campaign_id": "would-have-been-created"}

    with TestClient(app) as c:
        auth_service_module.settings.admin_service_read_token = None
        auth_service_module.settings.admin_service_operator_token = SERVICE_OPERATOR_TOKEN
        try:
            resp = c.post("/mail/campaigns", headers=_OPERATOR_HEADERS)
        finally:
            auth_service_module.settings.admin_service_operator_token = None

    assert resp.status_code == 200


# --- No Authorization header: unaffected regardless of operator config -----


def test_no_authorization_header_preserves_cookie_behavior_with_operator_token_configured(
    client, configured_service_operator_token
):
    c, _svc = client

    unauthenticated = c.get("/mail/campaigns")
    assert unauthenticated.status_code == 401

    _login(c)
    authenticated = c.get("/mail/campaigns")
    assert authenticated.status_code == 200


def test_normal_session_auth_for_operator_scoped_routes_is_completely_unchanged(
    client, configured_service_operator_token
):
    """A logged-in Hub session can still do everything through the normal
    cookie path regardless of the operator token existing -- including
    actions the OPERATOR token itself cannot reach (e.g. resume, or
    deleting a whole CRM list)."""
    c, _svc = client
    _login(c)

    assert c.post("/mail/campaigns").status_code == 200
    assert c.post("/mail/campaigns/c1/resume").status_code == 200
    assert c.delete("/crm/lists/list-1").status_code == 200


# --- Cross-scope isolation: read token and operator token never overlap ----


def test_valid_read_token_gets_403_on_an_operator_only_route(
    client, configured_service_read_token, configured_service_operator_token
):
    """A read-scoped token must not be usable for a write action just
    because an operator token also happens to be configured."""
    c, _svc = client

    resp = c.post("/mail/campaigns", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})

    assert resp.status_code == 403


def test_valid_operator_token_gets_403_on_a_read_only_scoped_route(
    client, configured_service_read_token, configured_service_operator_token
):
    """The operator token's scope does not include generic /crm/* GET
    (deliberately -- see the module docstring) even though the read
    token, configured alongside it, does."""
    c, _svc = client

    resp = c.get("/crm/contacts", headers=_OPERATOR_HEADERS)

    assert resp.status_code == 403
    assert "real-crm-data" not in resp.text


def test_each_token_still_works_on_its_own_scope_when_both_are_configured(
    client, configured_service_read_token, configured_service_operator_token
):
    c, _svc = client

    read_resp = c.get("/crm/contacts", headers={"Authorization": f"Bearer {SERVICE_READ_TOKEN}"})
    operator_resp = c.post("/mail/campaigns", headers=_OPERATOR_HEADERS)

    assert read_resp.status_code == 200
    assert operator_resp.status_code == 200


def test_operator_token_value_does_not_authenticate_as_the_read_token():
    """The two secrets are compared independently -- presenting the
    OPERATOR token's value against a route that requires the READ
    token's scope must never accidentally succeed because both happen to
    be configured. (Cannot collide in practice since they're
    independently random, but this proves the comparison itself, not
    just the improbability of a collision.)"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.repositories.auth_session_store import MemoryAuthSessionStore
    from app.services.auth_service import AuthService

    app = FastAPI()
    app.middleware("http")(enforce_session_auth)
    app.state.auth_service = AuthService(session_store=MemoryAuthSessionStore())

    @app.get("/crm/contacts")
    async def crm_contacts_read():
        return [{"crm_contact_id": "real-crm-data"}]

    with TestClient(app) as c:
        auth_service_module.settings.admin_service_read_token = SERVICE_READ_TOKEN
        auth_service_module.settings.admin_service_operator_token = SERVICE_OPERATOR_TOKEN
        try:
            resp = c.get("/crm/contacts", headers=_OPERATOR_HEADERS)
        finally:
            auth_service_module.settings.admin_service_read_token = None
            auth_service_module.settings.admin_service_operator_token = None

    assert resp.status_code == 403  # operator token IS a recognized identity, just out of scope here
    assert "real-crm-data" not in resp.text
