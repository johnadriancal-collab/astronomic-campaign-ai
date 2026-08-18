"""
Route-level tests for /mail/* -- auth-free (no shared-secret token exists
for this internal API, matching /crm/* convention), focused on: the routes
actually work end-to-end through real services, and -- most importantly --
there is NO route capable of activating/sending a campaign (H. Launch
safety) and NO route capable of touching Apollo/ITF/Email Intake/CSV
import (J. Isolation).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail import router as mail_router
from app.dependencies import get_mail_campaign_service, get_mail_suppression_service
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_suppression_service import MailSuppressionService


@pytest.fixture
def crm():
    return CrmService()


@pytest.fixture
def campaign_service(crm):
    return MailCampaignService(
        campaign_store=MemoryMailCampaignStore(),
        step_store=MemoryMailSequenceStepStore(),
        enrollment_store=MemoryMailEnrollmentStore(),
        crm_service=crm,
        activity_log=ActivityLogService(MemoryActivityEventStore()),
    )


@pytest.fixture
def suppression_service():
    return MailSuppressionService(store=MemoryMailSuppressionStore(), activity_log=ActivityLogService(MemoryActivityEventStore()))


@pytest.fixture
def client(campaign_service, suppression_service):
    app = FastAPI()
    app.include_router(mail_router)
    app.dependency_overrides[get_mail_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_mail_suppression_service] = lambda: suppression_service
    with TestClient(app) as c:
        yield c


# --- Basic CRUD through the API ------------------------------------------


def test_create_and_get_campaign(client):
    created = client.post("/mail/campaigns", json={"name": "Q1 Outreach"}).json()
    assert created["status"] == "draft"

    fetched = client.get(f"/mail/campaigns/{created['mail_campaign_id']}").json()
    assert fetched["name"] == "Q1 Outreach"


def test_get_missing_campaign_returns_404(client):
    resp = client.get("/mail/campaigns/does-not-exist")
    assert resp.status_code == 404


# --- Campaign Manager Integration Phase: Create Campaign modal fields -----


def test_create_campaign_with_full_config_in_one_call(client):
    """The Create Campaign modal submits everything in one POST -- this
    must persist all of it without a second PATCH round-trip."""
    resp = client.post(
        "/mail/campaigns",
        json={
            "name": "Austin Founder Outreach — August 2026",
            "sharing": "only_me",
            "sending_days": [0, 1, 2, 3, 4],
            "start_time": "08:00",
            "end_time": "18:00",
            "timezone": "America/Chicago",
            "all_hours": False,
            "start_immediately": True,
            "daily_lead_start_limit": 50,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["sharing"] == "only_me"
    assert body["sending_days"] == [0, 1, 2, 3, 4]
    assert body["start_time"].startswith("08:00")
    assert body["end_time"].startswith("18:00")
    assert body["timezone"] == "America/Chicago"
    assert body["start_immediately"] is True
    assert body["daily_lead_start_limit"] == 50


def test_create_campaign_with_only_name_keeps_prior_defaults(client):
    """Callers that only ever send `name` (every pre-existing client) get
    exactly the same shape as before this phase."""
    resp = client.post("/mail/campaigns", json={"name": "Just A Name"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sharing"] == "everyone"
    assert body["all_hours"] is False
    assert body["start_immediately"] is False
    assert body["daily_lead_start_limit"] is None
    assert body["sending_days"] == []
    assert body["timezone"] is None


def test_create_campaign_with_all_hours_forces_full_day_bounds(client):
    resp = client.post(
        "/mail/campaigns",
        json={"name": "All Hours", "all_hours": True, "sending_days": [0, 1], "timezone": "UTC"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_hours"] is True
    assert body["start_time"].startswith("00:00")
    assert body["end_time"].startswith("23:59")


def test_create_campaign_rejects_invalid_daily_lead_start_limit(client):
    resp = client.post("/mail/campaigns", json={"name": "Bad Limit", "daily_lead_start_limit": 0})
    assert resp.status_code == 400


def test_create_campaign_rejects_invalid_sharing_value(client):
    resp = client.post("/mail/campaigns", json={"name": "Bad Sharing", "sharing": "team"})
    assert resp.status_code == 422  # Pydantic enum validation on the request model itself


def test_create_campaign_rejects_invalid_timezone(client):
    resp = client.post("/mail/campaigns", json={"name": "Bad TZ", "timezone": "Nowhere/Real"})
    assert resp.status_code == 400


def test_update_campaign_sharing_and_daily_limit(client):
    created = client.post("/mail/campaigns", json={"name": "Settings Edit"}).json()
    cid = created["mail_campaign_id"]

    resp = client.patch(f"/mail/campaigns/{cid}", json={"sharing": "only_me", "daily_lead_start_limit": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sharing"] == "only_me"
    assert body["daily_lead_start_limit"] == 25


def test_update_campaign_rejects_non_positive_daily_limit(client):
    created = client.post("/mail/campaigns", json={"name": "Settings Edit"}).json()
    resp = client.patch(f"/mail/campaigns/{created['mail_campaign_id']}", json={"daily_lead_start_limit": -1})
    assert resp.status_code == 400


def test_update_campaign_all_hours_toggle(client):
    created = client.post("/mail/campaigns", json={"name": "Toggle Hours"}).json()
    cid = created["mail_campaign_id"]

    on = client.patch(f"/mail/campaigns/{cid}", json={"all_hours": True})
    assert on.status_code == 200
    assert on.json()["start_time"].startswith("00:00")
    assert on.json()["end_time"].startswith("23:59")

    off = client.patch(f"/mail/campaigns/{cid}", json={"all_hours": False, "start_time": "09:00", "end_time": "17:00"})
    assert off.status_code == 200
    assert off.json()["all_hours"] is False
    assert off.json()["start_time"].startswith("09:00")


def test_start_immediately_never_appears_alongside_an_active_status(client):
    """No matter what start_immediately is set to, status stays draft --
    there is no status value this campaign can reach that implies sending."""
    resp = client.post("/mail/campaigns", json={"name": "Immediate", "start_immediately": True})
    body = resp.json()
    assert body["start_immediately"] is True
    assert body["status"] == "draft"
    schema = client.get("/openapi.json").json()
    status_enum = schema["components"]["schemas"]["MailCampaignStatus"]["enum"]
    assert "active" not in status_enum


def test_full_wizard_flow_through_ready_and_review(client, crm):
    import asyncio

    async def seed():
        contact_list = await crm.create_contact_list("API Test Audience")
        c1 = await crm.create_contact({"email": "a@example.com"})
        c2 = await crm.create_contact({"email": "b@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c2.crm_contact_id])
        return contact_list.list_id

    list_id = asyncio.run(seed())

    created = client.post("/mail/campaigns", json={"name": "Wizard Flow"}).json()
    cid = created["mail_campaign_id"]

    patched = client.patch(
        f"/mail/campaigns/{cid}",
        json={
            "source_list_id": list_id,
            "sending_days": [0, 1, 2, 3, 4],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "America/Chicago",
        },
    )
    assert patched.status_code == 200

    step_resp = client.post(f"/mail/campaigns/{cid}/steps", json={"subject": "Hi {{first_name}}", "body": "Body"})
    assert step_resp.status_code == 200
    assert step_resp.json()["step_number"] == 1

    review = client.get(f"/mail/campaigns/{cid}/review").json()
    assert review["total_contacts"] == 2
    assert review["contacts_eligible"] == 2
    assert review["sequence_step_count"] == 1
    assert review["theoretical_total_sends"] == 2
    assert review["daily_capacity_estimate"] is None

    ready = client.post(f"/mail/campaigns/{cid}/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"

    enrollments = client.get(f"/mail/campaigns/{cid}/enrollments").json()
    assert len(enrollments) == 2


def test_mark_ready_returns_422_with_reasons_when_incomplete(client):
    created = client.post("/mail/campaigns", json={"name": "Incomplete"}).json()
    resp = client.post(f"/mail/campaigns/{created['mail_campaign_id']}/ready")
    assert resp.status_code == 422
    assert "audience" in resp.json()["detail"].lower()


def test_unknown_variable_returns_400(client):
    created = client.post("/mail/campaigns", json={"name": "Vars"}).json()
    resp = client.post(
        f"/mail/campaigns/{created['mail_campaign_id']}/steps",
        json={"subject": "Hi {{deal_size}}", "body": "Body"},
    )
    assert resp.status_code == 400


def test_editing_a_ready_campaign_returns_409(client, crm):
    import asyncio

    async def seed():
        contact_list = await crm.create_contact_list("Lock Test Audience")
        c1 = await crm.create_contact({"email": "locked@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])
        return contact_list.list_id

    list_id = asyncio.run(seed())

    created = client.post("/mail/campaigns", json={"name": "Locked"}).json()
    cid = created["mail_campaign_id"]
    client.patch(
        f"/mail/campaigns/{cid}",
        json={
            "source_list_id": list_id,
            "sending_days": [0],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "UTC",
        },
    )
    client.post(f"/mail/campaigns/{cid}/steps", json={"subject": "S", "body": "B"})

    still_draft = client.patch(f"/mail/campaigns/{cid}", json={"name": "Still editable while draft"})
    assert still_draft.status_code == 200  # editable before ready

    ready = client.post(f"/mail/campaigns/{cid}/ready")
    assert ready.status_code == 200

    locked = client.patch(f"/mail/campaigns/{cid}", json={"name": "Should be rejected"})
    assert locked.status_code == 409

    locked_step = client.post(f"/mail/campaigns/{cid}/steps", json={"subject": "New", "body": "B"})
    assert locked_step.status_code == 409


# --- Suppression ----------------------------------------------------------


def test_suppress_and_check_status(client):
    resp = client.post("/mail/suppressions", json={"email": "amos@example.com", "reason": "unsubscribed"})
    assert resp.status_code == 200
    assert resp.json()["email_normalized"] == "amos@example.com"

    status = client.get("/mail/suppressions/amos@example.com").json()
    assert status["suppressed"] is True
    assert status["reason"] == "unsubscribed"


def test_status_for_never_suppressed_email_is_not_a_404(client):
    resp = client.get("/mail/suppressions/never@example.com")
    assert resp.status_code == 200
    assert resp.json()["suppressed"] is False


def test_unsuppress_never_suppressed_returns_404(client):
    resp = client.post("/mail/suppressions/unsuppress", json={"email": "never@example.com"})
    assert resp.status_code == 404


def test_unsuppress_round_trip(client):
    client.post("/mail/suppressions", json={"email": "amos@example.com"})
    resp = client.post("/mail/suppressions/unsuppress", json={"email": "amos@example.com"})
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    status = client.get("/mail/suppressions/amos@example.com").json()
    assert status["suppressed"] is False


# --- H. Launch safety: no route exists that can activate/send ------------


def test_no_launch_activate_send_or_queue_route_exists(client):
    """Direct proof, not just an omission -- every plausible route name for
    starting a send is probed and must be absent (404, since FastAPI has no
    matching route) or otherwise never succeed."""
    campaign = client.post("/mail/campaigns", json={"name": "Safety Check"}).json()
    cid = campaign["mail_campaign_id"]

    forbidden_paths = [
        f"/mail/campaigns/{cid}/launch",
        f"/mail/campaigns/{cid}/activate",
        f"/mail/campaigns/{cid}/start",
        f"/mail/campaigns/{cid}/send",
        f"/mail/campaigns/{cid}/send-now",
        f"/mail/campaigns/{cid}/queue",
        f"/mail/campaigns/{cid}/dispatch",
        "/mail/send",
        "/mail/queue/run",
        "/mail/worker/run",
    ]
    for path in forbidden_paths:
        resp = client.post(path)
        assert resp.status_code == 404, f"expected 404 (no such route) for {path}, got {resp.status_code}"


def test_mail_campaign_status_enum_never_exposes_active(client):
    """The OpenAPI schema itself must never advertise 'active'/'paused'/
    'completed' as a possible MailCampaign.status value -- confirms the
    enum restriction is visible at the API contract level, not just in
    Python."""
    schema = client.get("/openapi.json").json()
    status_enum = schema["components"]["schemas"]["MailCampaignStatus"]["enum"]
    assert set(status_enum) == {"draft", "ready", "archived"}


# --- J. Isolation: unrelated systems are never touched by this router ----


def test_mail_router_declares_no_routes_outside_mail_prefix():
    for route in mail_router.routes:
        assert route.path.startswith("/mail"), f"unexpected route outside /mail: {route.path}"


def test_mail_api_module_imports_nothing_from_apollo_or_itf_or_email_intake():
    """Static proof of isolation -- app/api/mail.py's own IMPORT STATEMENTS
    (not prose/comments, which may mention these names descriptively) never
    reference the Apollo Campaign system, ITF, Email Intake, or QuickMail/
    Gmail/SMTP/OAuth integrations."""
    import ast
    import inspect

    import app.api.mail as mail_api

    tree = ast.parse(inspect.getsource(mail_api))
    imported_module_paths = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]

    forbidden_exact_modules = {
        "app.services.campaign_service",
        "app.services.campaign_sync_service",
        "app.services.itf_ingestion_service",
        "app.services.email_intake_service",
        "app.apollo",
    }
    forbidden_substrings = ("apollo", "itf_ingestion", "email_intake", "quickmail", "gmail", "smtplib", "oauth")

    assert not (set(imported_module_paths) & forbidden_exact_modules), imported_module_paths
    joined = " ".join(imported_module_paths).lower()
    for forbidden in forbidden_substrings:
        assert forbidden not in joined, f"app/api/mail.py unexpectedly imports from a module matching '{forbidden}': {imported_module_paths}"
