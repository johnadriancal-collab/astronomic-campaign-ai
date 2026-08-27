"""
Route-level tests for the internal LumaQuestionMapping management API
(GET/POST/PATCH /crm/luma-question-mappings, POST .../deactivate). Mounts
the REAL routers AND the REAL session_auth_middleware, matching
tests/test_luma_api.py's convention -- these prove the routes are
actually behind the Hub session gate, not just that the service methods
work in isolation.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.luma import mapping_router, router as luma_router
from app.dependencies import get_auth_service, get_luma_sync_service
from app.models.crm import CrmCustomFieldDefinition, CustomFieldType
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
from app.session_auth_middleware import PUBLIC_PATHS, enforce_session_auth

REAL_PASSWORD = "correct horse battery staple"


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)


@pytest.fixture
def auth_svc():
    return AuthService(session_store=MemoryAuthSessionStore())


@pytest_asyncio.fixture
async def luma_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="investor_type", label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT, options=["Angel Investor"], active=True,
            created_at=_now(), updated_at=_now(),
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
    app.include_router(mapping_router)
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    app.dependency_overrides[get_luma_sync_service] = lambda: luma_service
    with TestClient(app) as c:
        yield c


def _login(client) -> None:
    resp = client.post("/auth/login", json={"email": "team@astronomic.com", "password": REAL_PASSWORD})
    assert resp.status_code == 200


# --- authentication ------------------------------------------------------


def test_list_mappings_requires_authentication(client):
    resp = client.get("/crm/luma-question-mappings")
    assert resp.status_code == 401


def test_create_mapping_requires_authentication(client):
    resp = client.post(
        "/crm/luma-question-mappings",
        json={"question_label": "LinkedIn Profile", "target_field_key": "linkedin_url"},
    )
    assert resp.status_code == 401


def test_update_mapping_requires_authentication(client):
    resp = client.patch("/crm/luma-question-mappings/some-id", json={"active": False})
    assert resp.status_code == 401


def test_deactivate_mapping_requires_authentication(client):
    resp = client.post("/crm/luma-question-mappings/some-id/deactivate")
    assert resp.status_code == 401


def test_mapping_routes_are_not_in_public_paths():
    assert "/crm/luma-question-mappings" not in PUBLIC_PATHS


# --- CRUD, authenticated ---------------------------------------------------


def test_create_then_list_mapping(client):
    _login(client)
    create_resp = client.post(
        "/crm/luma-question-mappings",
        json={"question_label": "LinkedIn Profile", "question_type": "linkedin", "target_field_key": "linkedin_url"},
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["question_label"] == "LinkedIn Profile"
    assert created["active"] is True

    list_resp = client.get("/crm/luma-question-mappings")
    assert list_resp.status_code == 200
    labels = [m["question_label"] for m in list_resp.json()]
    assert "LinkedIn Profile" in labels


def test_create_mapping_for_a_custom_field(client):
    _login(client)
    resp = client.post(
        "/crm/luma-question-mappings",
        json={"question_label": "Investor Type", "question_type": "multi-select", "target_field_key": "custom:investor_type"},
    )
    assert resp.status_code == 200
    assert resp.json()["target_field_key"] == "custom:investor_type"


def test_create_mapping_rejects_unknown_core_field(client):
    _login(client)
    resp = client.post(
        "/crm/luma-question-mappings",
        json={"question_label": "Made Up", "target_field_key": "this_field_does_not_exist"},
    )
    assert resp.status_code == 400


def test_create_mapping_rejects_unknown_custom_field(client):
    _login(client)
    resp = client.post(
        "/crm/luma-question-mappings",
        json={"question_label": "Made Up", "target_field_key": "custom:not_a_real_custom_field"},
    )
    assert resp.status_code == 400


def test_create_mapping_preserves_extract_key(client):
    _login(client)
    resp = client.post(
        "/crm/luma-question-mappings",
        json={
            "question_label": "Company",
            "question_type": "company",
            "target_field_key": "company",
            "extract_key": "company",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["extract_key"] == "company"


def test_update_mapping_changes_target_field(client):
    _login(client)
    created = client.post(
        "/crm/luma-question-mappings", json={"question_label": "Title", "target_field_key": "title"}
    ).json()

    resp = client.patch(f"/crm/luma-question-mappings/{created['luma_question_mapping_id']}", json={"target_field_key": "department"})
    assert resp.status_code == 200
    assert resp.json()["target_field_key"] == "department"


def test_update_mapping_rejects_invalid_target_field(client):
    _login(client)
    created = client.post(
        "/crm/luma-question-mappings", json={"question_label": "Title", "target_field_key": "title"}
    ).json()

    resp = client.patch(
        f"/crm/luma-question-mappings/{created['luma_question_mapping_id']}", json={"target_field_key": "nonexistent"}
    )
    assert resp.status_code == 400
    unchanged = client.get("/crm/luma-question-mappings").json()
    assert next(m for m in unchanged if m["luma_question_mapping_id"] == created["luma_question_mapping_id"])["target_field_key"] == "title"


def test_update_unknown_mapping_returns_404(client):
    _login(client)
    resp = client.patch("/crm/luma-question-mappings/does-not-exist", json={"active": False})
    assert resp.status_code == 404


def test_deactivate_mapping_sets_active_false_and_preserves_the_row(client):
    _login(client)
    created = client.post(
        "/crm/luma-question-mappings", json={"question_label": "LinkedIn Profile", "target_field_key": "linkedin_url"}
    ).json()

    resp = client.post(f"/crm/luma-question-mappings/{created['luma_question_mapping_id']}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # The row still exists (deactivated, not deleted) -- visible with include_inactive.
    all_mappings = client.get("/crm/luma-question-mappings?include_inactive=true").json()
    assert any(m["luma_question_mapping_id"] == created["luma_question_mapping_id"] for m in all_mappings)


def test_deactivate_unknown_mapping_returns_404(client):
    _login(client)
    resp = client.post("/crm/luma-question-mappings/does-not-exist/deactivate")
    assert resp.status_code == 404


def test_list_excludes_inactive_by_default_query_param(client):
    _login(client)
    created = client.post(
        "/crm/luma-question-mappings", json={"question_label": "LinkedIn Profile", "target_field_key": "linkedin_url"}
    ).json()
    client.post(f"/crm/luma-question-mappings/{created['luma_question_mapping_id']}/deactivate")

    active_only = client.get("/crm/luma-question-mappings?include_inactive=false").json()
    assert all(m["luma_question_mapping_id"] != created["luma_question_mapping_id"] for m in active_only)


# --- deactivated mapping is actually ignored by Luma ingestion -------------


@pytest.mark.asyncio
async def test_deactivated_mapping_is_ignored_by_luma_ingestion(client, luma_service):
    from tests.test_luma_sync_service import make_event, make_guest

    _login(client)
    created = client.post(
        "/crm/luma-question-mappings", json={"question_label": "LinkedIn Profile", "target_field_key": "linkedin_url"}
    ).json()

    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "https://linkedin.com/in/alice"}
        ]
    )
    result_active = await luma_service.process_guest_event(make_event(event_id="evt-a"), guest)
    assert result_active.contact.linkedin_url == "https://linkedin.com/in/alice"

    client.post(f"/crm/luma-question-mappings/{created['luma_question_mapping_id']}/deactivate")

    guest2 = make_guest(
        guest_id="gst-2",
        email="bob@example.com",
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "https://linkedin.com/in/bob"}
        ],
    )
    result_after_deactivation = await luma_service.process_guest_event(make_event(event_id="evt-a"), guest2)
    assert result_after_deactivation.contact.linkedin_url is None


# --- webhook cannot modify mappings -----------------------------------------


def test_webhook_route_has_no_mapping_mutation_capability(client, luma_service):
    """Structural: the webhook route only ever calls
    LumaSyncService.handle_webhook()/process_guest_event(), which only
    ever call mapping_store.list() -- never create/save. This asserts the
    actual source, not just behavior, so a future edit can't silently
    wire a mutation path in without this test catching it."""
    import inspect

    from app.services import luma_sync_service as luma_sync_service_module

    handle_webhook_source = inspect.getsource(luma_sync_service_module.LumaSyncService.handle_webhook)
    process_guest_event_source = inspect.getsource(luma_sync_service_module.LumaSyncService.process_guest_event)
    build_mapped_fields_source = inspect.getsource(luma_sync_service_module.LumaSyncService._build_mapped_fields)

    for source in (handle_webhook_source, process_guest_event_source, build_mapped_fields_source):
        assert "mapping_store.create" not in source
        assert "mapping_store.save" not in source


@pytest.mark.asyncio
async def test_webhook_payload_cannot_smuggle_a_mapping_write(client, luma_service):
    """Even a webhook payload that LOOKS like it's trying to create a
    mapping (e.g. nesting mapping-shaped data in an answer) has no effect
    on the mapping store -- there is no field in the guest/event schema
    this module reads as an instruction to write to mapping_store."""
    from tests.test_luma_sync_service import make_event, make_guest

    before = len(await luma_service.mapping_store.list())
    guest = make_guest(
        registration_answers=[
            {
                "label": "question_label",  # attempting to look like mapping-creation data
                "question_id": "q-1",
                "question_type": "text",
                "value": "target_field_key=linkedin_url",
            }
        ]
    )
    await luma_service.process_guest_event(make_event(), guest)
    after = len(await luma_service.mapping_store.list())
    assert after == before


# --- no frontend dependency --------------------------------------------------


def test_mapping_endpoints_never_referenced_in_frontend_source():
    import re
    from pathlib import Path

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    offenders = []
    for path in frontend_dir.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        if re.search(r"luma-question-mappings|luma_question_mapping", path.read_text(errors="ignore")):
            offenders.append(str(path))
    assert offenders == []
