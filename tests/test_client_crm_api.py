"""
Route-level tests for /client-crm/clients -- Client CRM Stage 1B
(2026-09-07). Exercises just the client_crm router against a fresh
FastAPI app (no session-auth middleware mounted here -- see
test_session_auth_middleware.py for the auth-boundary coverage: this
file is about request/response shape and HTTP status mapping only), same
isolation style as test_activity_api.py.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.client_crm import router as client_crm_router
from app.dependencies import get_client_crm_service
from app.models.crm import CrmContact
from app.models.luma import LumaEvent
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService


@pytest.fixture
def test_client():
    service = ClientCrmService(
        client_store=MemoryClientStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()),
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=MemoryCrmContactStore(),
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=MemoryLumaEventStore(),
    )
    app = FastAPI()
    app.include_router(client_crm_router)
    app.dependency_overrides[get_client_crm_service] = lambda: service
    with TestClient(app) as client:
        yield client, service


@pytest.fixture
def contact_test_client():
    """Same as test_client, but also exposes the CrmContactStore directly
    so a test can seed a canonical Contact before linking it."""
    crm_contact_store = MemoryCrmContactStore()
    service = ClientCrmService(
        client_store=MemoryClientStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()),
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=crm_contact_store,
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=MemoryLumaEventStore(),
    )
    app = FastAPI()
    app.include_router(client_crm_router)
    app.dependency_overrides[get_client_crm_service] = lambda: service
    with TestClient(app) as client:
        yield client, service, crm_contact_store


@pytest.fixture
def luma_test_client():
    """Same as test_client, but also exposes the LumaEventStore directly
    so a test can seed a persisted Luma event before linking an Engagement
    to it (Client CRM Stage 1H-A)."""
    luma_event_store = MemoryLumaEventStore()
    service = ClientCrmService(
        client_store=MemoryClientStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()),
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=MemoryCrmContactStore(),
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=luma_event_store,
    )
    app = FastAPI()
    app.include_router(client_crm_router)
    app.dependency_overrides[get_client_crm_service] = lambda: service
    with TestClient(app) as client:
        yield client, service, luma_event_store


def _seed_luma_event(luma_event_store, luma_event_id="luma-123", name="Hive ASMBLD SF Investor Dinner", **overrides) -> None:
    now = datetime.now(timezone.utc)
    event = LumaEvent(luma_event_id=luma_event_id, name=name, synced_at=now, updated_at=now, **overrides)
    asyncio.run(luma_event_store.save(event))


def _seed_crm_contact(crm_contact_store, **overrides) -> None:
    now = datetime.now(timezone.utc)
    fields = {
        "crm_contact_id": "ethan-1",
        "first_name": "Ethan",
        "last_name": "Wong",
        "email": "ethan@hiveasmbld.example.com",
        "title": "Co-CEO",
        "company": "Hive ASMBLD",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    asyncio.run(crm_contact_store.create(CrmContact(**fields)))


def _seed_ethan(crm_contact_store) -> None:
    _seed_crm_contact(crm_contact_store)


def test_create_client_minimal(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Hive ASMBLD"
    assert body["client_id"]
    assert body["status"] == "active"
    assert body["relationship_classification"] is None
    assert body["archived"] is False


def test_create_client_with_full_fields(test_client):
    client, _service = test_client
    resp = client.post(
        "/client-crm/clients",
        json={
            "name": "Acme Co",
            "website": "https://acme.example.com",
            "industry": "Venture",
            "status": "inactive",
            "relationship_classification": "opportunity",
            "owner": "Chris",
            "next_action": "Schedule Q1 check-in",
            "next_action_due": "2027-01-15",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "inactive"
    assert body["relationship_classification"] == "opportunity"
    assert body["next_action_due"] == "2027-01-15"


def test_create_client_missing_name_is_422(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={})
    assert resp.status_code == 422  # Pydantic request validation, not a service-layer 400


def test_create_client_blank_name_is_400(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "   "})
    assert resp.status_code == 400


def test_create_client_invalid_status_enum_is_422(test_client):
    client, _service = test_client
    resp = client.post("/client-crm/clients", json={"name": "Hive ASMBLD", "status": "not_a_real_status"})
    assert resp.status_code == 422


def test_get_client_existing(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.get(f"/client-crm/clients/{created['client_id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Hive ASMBLD"


def test_get_client_missing_is_404(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients/does-not-exist")
    assert resp.status_code == 404


def test_list_clients_empty_state(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_list_clients_search_and_filter(test_client):
    client, _service = test_client
    client.post("/client-crm/clients", json={"name": "Hive ASMBLD", "status": "active"})
    client.post("/client-crm/clients", json={"name": "Acme Co", "status": "inactive"})

    resp = client.get("/client-crm/clients", params={"q": "hive"})
    assert resp.json()["total"] == 1

    resp = client.get("/client-crm/clients", params={"status": "inactive"})
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["name"] == "Acme Co"


def test_list_clients_invalid_sort_by_is_400(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients", params={"sort_by": "not_a_real_field"})
    assert resp.status_code == 400


def test_update_client_partial_patch(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(f"/client-crm/clients/{created['client_id']}", json={"industry": "Venture"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["industry"] == "Venture"
    assert body["name"] == "Hive ASMBLD"
    assert body["updated_at"] != created["updated_at"]


def test_update_client_ignores_unknown_fields_like_client_id(test_client):
    """Confirms the request model itself has no client_id/created_at
    field -- a real HTTP caller sending them gets them silently dropped
    by Pydantic's own default "ignore extra fields" behavior, never
    reaching the service at all."""
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(
        f"/client-crm/clients/{created['client_id']}",
        json={"client_id": "different-id", "created_at": "2000-01-01T00:00:00Z", "industry": "Venture"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == created["client_id"]
    assert body["created_at"] == created["created_at"]
    assert body["industry"] == "Venture"


def test_update_client_missing_is_404(test_client):
    client, _service = test_client
    resp = client.patch("/client-crm/clients/does-not-exist", json={"industry": "Venture"})
    assert resp.status_code == 404


def test_update_client_blank_name_is_400(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(f"/client-crm/clients/{created['client_id']}", json={"name": "   "})
    assert resp.status_code == 400


def test_archive_and_restore_via_patch(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client_id = created["client_id"]

    archived = client.patch(f"/client-crm/clients/{client_id}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    # Archived Client remains directly readable by id.
    fetched = client.get(f"/client-crm/clients/{client_id}")
    assert fetched.status_code == 200
    assert fetched.json()["archived"] is True

    # Hidden from the default list...
    default_list = client.get("/client-crm/clients")
    assert default_list.json()["total"] == 0

    # ...but included when asked for.
    included_list = client.get("/client-crm/clients", params={"include_archived": True})
    assert included_list.json()["total"] == 1

    restored = client.patch(f"/client-crm/clients/{client_id}", json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert client.get("/client-crm/clients").json()["total"] == 1


# =====================================================================
# ClientContact -- Client CRM Stage 1D (2026-09-07)
# =====================================================================


def test_list_client_contacts_missing_client_is_404(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients/does-not-exist/contacts")
    assert resp.status_code == 404


def test_create_client_contact_missing_client_is_404(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    resp = client.post("/client-crm/clients/does-not-exist/contacts", json={"crm_contact_id": "ethan-1"})
    assert resp.status_code == 404


def test_create_client_contact_missing_crm_contact_id_is_422(contact_test_client):
    client, _service, _crm_contact_store = contact_test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(f"/client-crm/clients/{created['client_id']}/contacts", json={})
    assert resp.status_code == 422


def test_create_client_contact_unknown_crm_contact_id_is_400(contact_test_client):
    client, _service, _crm_contact_store = contact_test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/contacts", json={"crm_contact_id": "does-not-exist"}
    )
    assert resp.status_code == 400


def test_create_client_contact_links_and_populates_snapshot(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()

    resp = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts",
        json={"crm_contact_id": "ethan-1", "is_primary_contact": True, "title": "Co-CEO"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == created_client["client_id"]
    assert body["crm_contact_id"] == "ethan-1"
    assert body["first_name"] == "Ethan"
    assert body["last_name"] == "Wong"
    assert body["is_primary_contact"] is True
    assert body["archived"] is False


def test_list_client_contacts_after_create(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client.post(f"/client-crm/clients/{created_client['client_id']}/contacts", json={"crm_contact_id": "ethan-1"})

    resp = client.get(f"/client-crm/clients/{created_client['client_id']}/contacts")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["first_name"] == "Ethan"


def test_update_client_contact_partial_patch(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    contact = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts", json={"crm_contact_id": "ethan-1"}
    ).json()

    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/contacts/{contact['client_contact_id']}",
        json={"role_notes": "Introduced us to the CFO"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role_notes"] == "Introduced us to the CFO"
    assert body["is_primary_contact"] is False  # untouched


def test_update_client_contact_missing_is_404(contact_test_client):
    client, _service, _crm_contact_store = contact_test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/contacts/does-not-exist", json={"role_notes": "x"}
    )
    assert resp.status_code == 404


def test_archive_and_restore_client_contact_via_patch(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    contact = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts", json={"crm_contact_id": "ethan-1"}
    ).json()
    contact_url = f"/client-crm/clients/{created_client['client_id']}/contacts/{contact['client_contact_id']}"

    archived = client.patch(contact_url, json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    restored = client.patch(contact_url, json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


def test_no_delete_route_exists_for_client_contacts(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    contact = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts", json={"crm_contact_id": "ethan-1"}
    ).json()
    resp = client.delete(f"/client-crm/clients/{created_client['client_id']}/contacts/{contact['client_contact_id']}")
    assert resp.status_code in (404, 405)


def test_switching_primary_contact_demotes_the_other_via_api(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_ethan(crm_contact_store)
    _seed_crm_contact(crm_contact_store, crm_contact_id="tim-1", first_name="Tim", last_name="Lankau")
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    ethan_contact = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts",
        json={"crm_contact_id": "ethan-1", "is_primary_contact": True},
    ).json()
    tim_contact = client.post(
        f"/client-crm/clients/{created_client['client_id']}/contacts", json={"crm_contact_id": "tim-1"}
    ).json()

    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/contacts/{tim_contact['client_contact_id']}",
        json={"is_primary_contact": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_primary_contact"] is True

    contacts = client.get(f"/client-crm/clients/{created_client['client_id']}/contacts").json()
    by_id = {c["client_contact_id"]: c for c in contacts}
    assert by_id[ethan_contact["client_contact_id"]]["is_primary_contact"] is False


# =====================================================================
# Engagement -- Client CRM Stage 1E (2026-09-07)
# =====================================================================


def test_list_client_engagements_missing_client_is_404(test_client):
    client, _service = test_client
    resp = client.get("/client-crm/clients/does-not-exist/engagements")
    assert resp.status_code == 404


def test_create_engagement_missing_client_is_404(test_client):
    client, _service = test_client
    resp = client.post(
        "/client-crm/clients/does-not-exist/engagements",
        json={"title": "SF Investor Dinner", "engagement_type": "dinner"},
    )
    assert resp.status_code == 404


def test_create_engagement_missing_required_fields_is_422(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(f"/client-crm/clients/{created['client_id']}/engagements", json={})
    assert resp.status_code == 422


def test_create_engagement_blank_title_is_400(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/engagements",
        json={"title": "   ", "engagement_type": "dinner"},
    )
    assert resp.status_code == 400


def test_create_engagement_invalid_enum_is_422(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "not_a_real_type"},
    )
    assert resp.status_code == 422


def test_create_engagement_minimal(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/engagements",
        json={"title": "SF Investor Dinner", "engagement_type": "dinner"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "SF Investor Dinner"
    assert body["client_id"] == created["client_id"]
    assert body["status"] == "planned"
    assert body["dinner_type"] is None
    assert body["archived"] is False


def test_create_engagement_with_full_fields(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/engagements",
        json={
            "title": "SF Investor Dinner",
            "engagement_type": "dinner",
            "dinner_type": "investor_dinner",
            "engagement_date": "2026-09-22",
            "location": "The Battery, San Francisco",
            "status": "confirmed",
            "owner": "Chris",
            "fee": 5000.0,
            "contract_status": "signed",
            "contract_url": "https://drive.example.com/contract",
            "signed_date": "2026-09-01",
            "payment_status": "partial",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dinner_type"] == "investor_dinner"
    assert body["status"] == "confirmed"
    assert body["owner"] == "Chris"
    assert body["fee"] == 5000.0
    assert body["contract_status"] == "signed"
    assert body["payment_status"] == "partial"


def test_create_engagement_clears_dinner_type_for_sponsorship(test_client):
    client, _service = test_client
    created = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created['client_id']}/engagements",
        json={"title": "Fall Sponsorship", "engagement_type": "sponsorship", "dinner_type": "investor_dinner"},
    )
    assert resp.status_code == 200
    assert resp.json()["dinner_type"] is None


def test_get_engagement_existing(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()
    resp = client.get(f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "SF Dinner"


def test_get_engagement_missing_is_404(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.get(f"/client-crm/clients/{created_client['client_id']}/engagements/does-not-exist")
    assert resp.status_code == 404


def test_engagement_isolation_across_clients(test_client):
    """An Engagement created under Client A must 404 when requested or
    patched through Client B's URL."""
    client, _service = test_client
    client_a = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client_b = client.post("/client-crm/clients", json={"name": "Other Co"}).json()
    engagement = client.post(
        f"/client-crm/clients/{client_a['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()

    resp = client.get(f"/client-crm/clients/{client_b['client_id']}/engagements/{engagement['engagement_id']}")
    assert resp.status_code == 404

    resp = client.patch(
        f"/client-crm/clients/{client_b['client_id']}/engagements/{engagement['engagement_id']}",
        json={"title": "Hijacked"},
    )
    assert resp.status_code == 404


def test_list_client_engagements_after_create(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    )
    resp = client.get(f"/client-crm/clients/{created_client['client_id']}/engagements")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_update_engagement_partial_patch(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "owner": "Chris"},
    ).json()
    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}",
        json={"status": "confirmed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["owner"] == "Chris"
    assert body["title"] == "SF Dinner"


def test_update_engagement_missing_is_404(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/engagements/does-not-exist", json={"status": "confirmed"}
    )
    assert resp.status_code == 404


def test_archive_and_restore_engagement_via_patch(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()
    engagement_url = f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}"

    archived = client.patch(engagement_url, json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    restored = client.patch(engagement_url, json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


def test_no_delete_route_exists_for_engagements(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()
    resp = client.delete(f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}")
    assert resp.status_code in (404, 405)


# =====================================================================
# Engagement <-> Luma event link -- Client CRM Stage 1H-A (2026-09-09)
# =====================================================================


def test_create_engagement_with_luma_event_id_round_trips(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123")
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["luma_event_id"] == "luma-123"


def test_create_engagement_with_nonexistent_luma_event_id_is_400(luma_test_client):
    client, _service, _luma_event_store = luma_test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "does-not-exist"},
    )
    assert resp.status_code == 400


def test_create_engagement_with_already_linked_luma_event_id_is_409(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123")
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"},
    )
    resp = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "A Second Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"},
    )
    assert resp.status_code == 409


def test_update_engagement_links_and_unlinks_a_luma_event(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123")
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()
    engagement_url = f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}"

    linked = client.patch(engagement_url, json={"luma_event_id": "luma-123"})
    assert linked.status_code == 200
    assert linked.json()["luma_event_id"] == "luma-123"

    fetched = client.get(engagement_url)
    assert fetched.json()["luma_event_id"] == "luma-123"

    unlinked = client.patch(engagement_url, json={"luma_event_id": None})
    assert unlinked.status_code == 200
    assert unlinked.json()["luma_event_id"] is None


def test_update_engagement_with_nonexistent_luma_event_id_is_400(luma_test_client):
    client, _service, _luma_event_store = luma_test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    created = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner"},
    ).json()
    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/engagements/{created['engagement_id']}",
        json={"luma_event_id": "does-not-exist"},
    )
    assert resp.status_code == 400


def test_update_engagement_with_luma_event_already_linked_elsewhere_is_409(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123")
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"},
    )
    other = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "Other Dinner", "engagement_type": "dinner"},
    ).json()
    resp = client.patch(
        f"/client-crm/clients/{created_client['client_id']}/engagements/{other['engagement_id']}",
        json={"luma_event_id": "luma-123"},
    )
    assert resp.status_code == 409


def test_list_luma_events_returns_stored_events(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123", name="SF Investor Dinner")
    resp = client.get("/client-crm/luma-events")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["luma_event_id"] == "luma-123"
    assert body[0]["name"] == "SF Investor Dinner"
    assert "calendar_id" not in body[0]


def test_list_luma_events_filters_by_q(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "e1", name="SF Investor Dinner")
    _seed_luma_event(luma_event_store, "e2", name="NY Founder Breakfast")
    resp = client.get("/client-crm/luma-events", params={"q": "investor"})
    assert resp.status_code == 200
    body = resp.json()
    assert [e["luma_event_id"] for e in body] == ["e1"]


def test_get_luma_event_existing(luma_test_client):
    client, _service, luma_event_store = luma_test_client
    _seed_luma_event(luma_event_store, "luma-123", name="SF Investor Dinner")
    resp = client.get("/client-crm/luma-events/luma-123")
    assert resp.status_code == 200
    assert resp.json()["name"] == "SF Investor Dinner"


def test_get_luma_event_missing_is_404(luma_test_client):
    client, _service, _luma_event_store = luma_test_client
    resp = client.get("/client-crm/luma-events/does-not-exist")
    assert resp.status_code == 404


# =====================================================================
# EngagementCloseout -- Client CRM Stage 1F (2026-09-08)
# =====================================================================


def _create_client_and_engagement(client) -> tuple[dict, dict]:
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    engagement = client.post(
        f"/client-crm/clients/{created_client['client_id']}/engagements",
        json={"title": "SF Dinner", "engagement_type": "dinner", "dinner_type": "investor_dinner"},
    ).json()
    return created_client, engagement


def _closeout_url(client_id: str, engagement_id: str) -> str:
    return f"/client-crm/clients/{client_id}/engagements/{engagement_id}/closeout"


def test_get_engagement_closeout_missing_is_404(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.get(_closeout_url(created_client["client_id"], engagement["engagement_id"]))
    assert resp.status_code == 404


def test_get_engagement_closeout_missing_client_is_404(test_client):
    client, _service = test_client
    resp = client.get(_closeout_url("does-not-exist", "also-does-not-exist"))
    assert resp.status_code == 404


def test_create_engagement_closeout_minimal(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(_closeout_url(created_client["client_id"], engagement["engagement_id"]), json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["engagement_id"] == engagement["engagement_id"]
    assert body["client_id"] == created_client["client_id"]
    assert body["confirmed_guest_count"] is None
    assert body["archived"] is False


def test_create_engagement_closeout_with_full_fields(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(
        _closeout_url(created_client["client_id"], engagement["engagement_id"]),
        json={
            "confirmed_guest_count": 12,
            "attended_count": 10,
            "no_show_count": 2,
            "cancelled_count": 0,
            "unexpected_attendee_count": 1,
            "guest_quality": "Strong",
            "dinner_dynamics": "Energetic",
            "initial_client_experience": "Thrilled",
            "immediate_outcomes": "Two intros",
            "notable_signals": "Co-investing interest",
            "issues": "Ran late",
            "referrals": "One referral offered",
            "future_opportunities": "Possible Q1 follow-on",
            "internal_notes": "Reuse venue",
            "completed_by": "Chris",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed_guest_count"] == 12
    assert body["no_show_count"] == 2
    assert body["cancelled_count"] == 0  # explicit zero preserved
    assert body["guest_quality"] == "Strong"
    assert body["completed_by"] == "Chris"


def test_create_engagement_closeout_negative_count_is_422(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(
        _closeout_url(created_client["client_id"], engagement["engagement_id"]), json={"attended_count": -1}
    )
    assert resp.status_code == 422


def test_create_engagement_closeout_missing_engagement_is_404(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(_closeout_url(created_client["client_id"], "does-not-exist"), json={})
    assert resp.status_code == 404


def test_create_engagement_closeout_duplicate_is_409(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={})

    resp = client.post(url, json={})
    assert resp.status_code == 409


def test_get_engagement_closeout_after_create(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={"attended_count": 10})

    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.json()["attended_count"] == 10


def test_update_engagement_closeout_partial_patch(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={"attended_count": 10, "guest_quality": "Strong"})

    resp = client.patch(url, json={"attended_count": 11})
    assert resp.status_code == 200
    body = resp.json()
    assert body["attended_count"] == 11
    assert body["guest_quality"] == "Strong"


def test_update_engagement_closeout_negative_count_is_422(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={})

    resp = client.patch(url, json={"no_show_count": -1})
    assert resp.status_code == 422


def test_update_engagement_closeout_missing_is_404(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.patch(_closeout_url(created_client["client_id"], engagement["engagement_id"]), json={"attended_count": 5})
    assert resp.status_code == 404


def test_archive_and_restore_engagement_closeout_via_patch(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={})

    archived = client.patch(url, json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    restored = client.patch(url, json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


def test_no_delete_route_exists_for_engagement_closeout(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    url = _closeout_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={})

    resp = client.delete(url)
    assert resp.status_code in (404, 405)


def test_engagement_closeout_isolation_across_clients(test_client):
    client, _service = test_client
    created_client_a, engagement_a = _create_client_and_engagement(client)
    created_client_b = client.post("/client-crm/clients", json={"name": "Other Co"}).json()
    url_a = _closeout_url(created_client_a["client_id"], engagement_a["engagement_id"])
    client.post(url_a, json={"attended_count": 10})

    resp = client.get(_closeout_url(created_client_b["client_id"], engagement_a["engagement_id"]))
    assert resp.status_code == 404

    resp = client.patch(_closeout_url(created_client_b["client_id"], engagement_a["engagement_id"]), json={"attended_count": 1})
    assert resp.status_code == 404


# =====================================================================
# EngagementParticipant -- Client CRM Stage 1G (2026-09-08)
# =====================================================================


def _participants_url(client_id: str, engagement_id: str) -> str:
    return f"/client-crm/clients/{client_id}/engagements/{engagement_id}/participants"


def _participant_url(client_id: str, engagement_id: str, participant_id: str) -> str:
    return f"{_participants_url(client_id, engagement_id)}/{participant_id}"


def test_list_participants_empty_state(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.get(_participants_url(created_client["client_id"], engagement["engagement_id"]))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_participants_missing_engagement_is_404(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.get(_participants_url(created_client["client_id"], "does-not-exist"))
    assert resp.status_code == 404


def test_create_resolved_participant(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store)
    created_client, engagement = _create_client_and_engagement(client)

    resp = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"crm_contact_id": "ethan-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["crm_contact_id"] == "ethan-1"
    assert body["first_name"] == "Ethan"
    assert body["email"] == "ethan@hiveasmbld.example.com"
    assert body["role"] == "guest"
    assert body["source"] == "manual"


def test_create_resolved_participant_missing_contact_is_400(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"crm_contact_id": "does-not-exist"}
    )
    assert resp.status_code == 400


def test_create_unresolved_participant_with_name(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]),
        json={"first_name": "Jane", "last_name": "Doe", "role": "host"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["crm_contact_id"] is None
    assert body["first_name"] == "Jane"
    assert body["role"] == "host"


def test_create_unresolved_participant_with_no_identity_is_400(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(_participants_url(created_client["client_id"], engagement["engagement_id"]), json={})
    assert resp.status_code == 400


def test_create_participant_invalid_role_is_422(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]),
        json={"first_name": "Jane", "role": "not_a_real_role"},
    )
    assert resp.status_code == 422


def test_create_participant_missing_engagement_is_404(test_client):
    client, _service = test_client
    created_client = client.post("/client-crm/clients", json={"name": "Hive ASMBLD"}).json()
    resp = client.post(_participants_url(created_client["client_id"], "does-not-exist"), json={"first_name": "Jane"})
    assert resp.status_code == 404


def test_create_resolved_participant_duplicate_is_409(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store)
    created_client, engagement = _create_client_and_engagement(client)
    url = _participants_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={"crm_contact_id": "ethan-1"})

    resp = client.post(url, json={"crm_contact_id": "ethan-1"})
    assert resp.status_code == 409


def test_update_participant_partial_patch(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    created = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]),
        json={"first_name": "Jane", "role": "guest"},
    ).json()

    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], created["participant_id"]),
        json={"attendance_status": "attended", "is_walk_in": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["attendance_status"] == "attended"
    assert body["is_walk_in"] is True
    assert body["first_name"] == "Jane"  # untouched


def test_update_participant_missing_is_404(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], "does-not-exist"),
        json={"role": "host"},
    )
    assert resp.status_code == 404


def test_link_unresolved_participant_to_contact(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store)
    created_client, engagement = _create_client_and_engagement(client)
    unresolved = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]),
        json={"first_name": "Ethan", "email": "guessed@example.com"},
    ).json()

    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], unresolved["participant_id"]),
        json={"crm_contact_id": "ethan-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["crm_contact_id"] == "ethan-1"
    assert body["email"] == "ethan@hiveasmbld.example.com"  # refreshed from canonical


def test_linking_unresolved_participant_to_an_already_active_contact_is_409(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store)
    created_client, engagement = _create_client_and_engagement(client)
    url = _participants_url(created_client["client_id"], engagement["engagement_id"])
    client.post(url, json={"crm_contact_id": "ethan-1"})
    unresolved = client.post(url, json={"first_name": "Ethan", "last_name": "W."}).json()

    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], unresolved["participant_id"]),
        json={"crm_contact_id": "ethan-1"},
    )
    assert resp.status_code == 409


def test_clearing_crm_contact_id_on_a_resolved_participant_is_400(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store)
    created_client, engagement = _create_client_and_engagement(client)
    resolved = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"crm_contact_id": "ethan-1"}
    ).json()

    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], resolved["participant_id"]),
        json={"crm_contact_id": None},
    )
    assert resp.status_code == 400

    unchanged = client.get(_participants_url(created_client["client_id"], engagement["engagement_id"])).json()
    assert unchanged[0]["crm_contact_id"] == "ethan-1"


def test_resolved_participant_can_be_relinked_to_a_different_contact(contact_test_client):
    client, _service, crm_contact_store = contact_test_client
    _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan")
    _seed_crm_contact(crm_contact_store, crm_contact_id="priya-1", first_name="Priya", email="priya@hiveasmbld.example.com")
    created_client, engagement = _create_client_and_engagement(client)
    resolved = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"crm_contact_id": "ethan-1"}
    ).json()

    resp = client.patch(
        _participant_url(created_client["client_id"], engagement["engagement_id"], resolved["participant_id"]),
        json={"crm_contact_id": "priya-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["crm_contact_id"] == "priya-1"
    assert body["first_name"] == "Priya"


def test_archive_and_restore_participant_via_patch(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    created = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"first_name": "Jane"}
    ).json()
    url = _participant_url(created_client["client_id"], engagement["engagement_id"], created["participant_id"])

    archived = client.patch(url, json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    restored = client.patch(url, json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived"] is False


def test_no_delete_route_exists_for_participants(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    created = client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]), json={"first_name": "Jane"}
    ).json()
    resp = client.delete(_participant_url(created_client["client_id"], engagement["engagement_id"], created["participant_id"]))
    assert resp.status_code in (404, 405)


def test_participant_isolation_across_clients(test_client):
    client, _service = test_client
    created_client_a, engagement_a = _create_client_and_engagement(client)
    created_client_b = client.post("/client-crm/clients", json={"name": "Other Co"}).json()
    created = client.post(
        _participants_url(created_client_a["client_id"], engagement_a["engagement_id"]), json={"first_name": "Jane"}
    ).json()

    resp = client.patch(
        _participant_url(created_client_b["client_id"], engagement_a["engagement_id"], created["participant_id"]),
        json={"role": "host"},
    )
    assert resp.status_code == 404


def test_participant_creation_never_mutates_closeout(test_client):
    client, _service = test_client
    created_client, engagement = _create_client_and_engagement(client)
    closeout_url = f"/client-crm/clients/{created_client['client_id']}/engagements/{engagement['engagement_id']}/closeout"
    client.post(closeout_url, json={"confirmed_guest_count": 24, "attended_count": 24})

    client.post(
        _participants_url(created_client["client_id"], engagement["engagement_id"]),
        json={"first_name": "Jane", "attendance_status": "attended"},
    )

    resp = client.get(closeout_url)
    assert resp.json()["confirmed_guest_count"] == 24
    assert resp.json()["attended_count"] == 24
