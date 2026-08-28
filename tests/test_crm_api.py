"""
Route-level tests for /crm -- exercises just the CRM router against a
fresh FastAPI app, isolated from app.main's real SQLite file.
"""

import asyncio
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crm import router as crm_router
from app.dependencies import get_crm_import_service, get_crm_service
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService


@pytest.fixture
def test_client():
    crm_service = CrmService(contact_store=MemoryCrmContactStore())
    import_service = CrmImportService(crm_service=crm_service, batch_store=MemoryCrmImportBatchStore())

    app = FastAPI()
    app.include_router(crm_router)
    app.dependency_overrides[get_crm_service] = lambda: crm_service
    app.dependency_overrides[get_crm_import_service] = lambda: import_service

    with TestClient(app) as client:
        yield client


def test_create_get_update_archive_contact(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Ada", "email": "ada@example.com"})
    assert resp.status_code == 200
    contact_id = resp.json()["crm_contact_id"]

    resp = test_client.get(f"/crm/contacts/{contact_id}")
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Ada"

    resp = test_client.patch(f"/crm/contacts/{contact_id}", json={"company": "Acme"})
    assert resp.status_code == 200
    assert resp.json()["company"] == "Acme"

    resp = test_client.delete(f"/crm/contacts/{contact_id}")
    assert resp.status_code == 200
    assert resp.json()["archived"] is True


def test_create_contact_duplicate_email_returns_409(test_client):
    test_client.post("/crm/contacts", json={"email": "dup@example.com"})
    resp = test_client.post("/crm/contacts", json={"email": "dup@example.com"})
    assert resp.status_code == 409


def test_get_missing_contact_returns_404(test_client):
    resp = test_client.get("/crm/contacts/does-not-exist")
    assert resp.status_code == 404


def test_list_contacts_filters_by_city(test_client):
    test_client.post("/crm/contacts", json={"first_name": "A", "city": "Austin"})
    test_client.post("/crm/contacts", json={"first_name": "B", "city": "Denver"})

    resp = test_client.get("/crm/contacts", params={"city": "Austin"})
    assert resp.status_code == 200
    body = resp.json()
    assert [c["first_name"] for c in body["items"]] == ["A"]
    assert body["total"] == 1


def test_list_contacts_pagination_params(test_client):
    for i in range(3):
        test_client.post("/crm/contacts", json={"first_name": f"C{i}"})

    resp = test_client.get("/crm/contacts", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2


def test_export_fields_route_returns_schema_metadata_not_contact_data(test_client):
    """The route must be reachable at a fixed path ahead of /contacts/{id} (i.e.
    "export-fields" is never swallowed as a contact id) and must return field
    metadata only -- no contact data, confirming it stays a schema-only endpoint."""
    resp = test_client.get("/crm/contacts/export-fields")
    assert resp.status_code == 200
    body = resp.json()
    keys = {f["key"] for f in body}
    assert "first_name" in keys
    assert "email" in keys
    assert "source_snapshot" not in keys
    assert "custom_fields" not in keys
    by_key = {f["key"]: f["kind"] for f in body}
    assert by_key["technologies"] == "list"
    assert by_key["archived"] == "boolean"
    assert by_key["email"] == "scalar"


def test_custom_field_create_and_list(test_client):
    resp = test_client.post(
        "/crm/custom-fields",
        json={"field_key": "fav_team", "label": "Favorite Team", "field_type": "text"},
    )
    assert resp.status_code == 200

    resp = test_client.get("/crm/custom-fields")
    assert resp.status_code == 200
    assert resp.json()[0]["field_key"] == "fav_team"


def test_custom_field_duplicate_key_returns_409(test_client):
    test_client.post("/crm/custom-fields", json={"field_key": "k", "label": "A", "field_type": "text"})
    resp = test_client.post("/crm/custom-fields", json={"field_key": "k", "label": "B", "field_type": "text"})
    assert resp.status_code == 409


def test_full_import_flow_upload_preview_commit(test_client):
    csv_content = b"Email,First Name\nnew@example.com,Ada\n"
    resp = test_client.post(
        "/crm/import/upload", files={"file": ("prospects.csv", io.BytesIO(csv_content), "text/csv")}
    )
    assert resp.status_code == 200
    batch = resp.json()
    assert batch["row_count"] == 1
    batch_id = batch["import_batch_id"]

    resp = test_client.post(
        f"/crm/import/{batch_id}/preview", json={"column_mapping": {"Email": "email", "First Name": "first_name"}}
    )
    assert resp.status_code == 200
    previewed = resp.json()
    assert previewed["new_count"] == 1

    resp = test_client.post(f"/crm/import/{batch_id}/commit", json={"decisions": {}})
    assert resp.status_code == 200
    report = resp.json()
    assert report["created"] == 1

    resp = test_client.get("/crm/contacts", params={"q": "ada"})
    assert resp.json()["items"][0]["email"] == "new@example.com"


def test_import_preview_on_missing_batch_returns_404(test_client):
    resp = test_client.post("/crm/import/does-not-exist/preview", json={"column_mapping": {}})
    assert resp.status_code == 404


def test_backup_export_returns_full_snapshot(test_client):
    test_client.post("/crm/contacts", json={"first_name": "Ada"})
    resp = test_client.get("/crm/backup/export")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["contacts"]) == 1
    assert "exported_at" in body


def test_reconcile_legacy_fields_route_seeds_and_migrates(test_client):
    test_client.post("/crm/contacts", json={"custom_fields": {"dietary_restrictions": "Vegan"}})
    resp = test_client.post("/crm/reconcile-legacy-fields")
    assert resp.status_code == 200
    report = resp.json()
    assert "gender" in report["created"]
    assert report["contacts_updated"] == 1

    contacts = test_client.get("/crm/contacts").json()["items"]
    assert contacts[0]["thesis_dietary_preferences"] == ["Vegan"]


def test_translate_legacy_values_route(test_client):
    resp = test_client.post(
        "/crm/import/upload",
        files={"file": ("p.csv", io.BytesIO(b'Deal Stage\n"Friends & family, Seed"\n'), "text/csv")},
    )
    batch_id = resp.json()["import_batch_id"]

    resp = test_client.post(f"/crm/import/{batch_id}/translate-legacy-values")
    assert resp.status_code == 200
    translated = resp.json()
    assert translated["rows"][0]["Deal Stage"] == (
        "Friends & Family (idea or concept stage, often pre-incorporation);"
        "Seed (product in market, early customers or pilots)"
    )


def test_translate_legacy_values_missing_batch_returns_404(test_client):
    resp = test_client.post("/crm/import/does-not-exist/translate-legacy-values")
    assert resp.status_code == 404


def test_repair_comma_delimited_custom_fields_route(test_client):
    resp = test_client.post(
        "/crm/contacts",
        json={
            "custom_fields": {"investor_type": ["Private Equity, Venture Capital"]},
            "source_snapshot": {"Investor type": "Private Equity, Venture Capital"},
        },
    )
    contact_id = resp.json()["crm_contact_id"]

    resp = test_client.post("/crm/repair-comma-delimited-custom-fields")
    assert resp.status_code == 200
    report = resp.json()
    assert report["contacts_touched"] == 1
    assert report["investor_type_repaired"] == 1

    fixed = test_client.get(f"/crm/contacts/{contact_id}").json()
    assert fixed["custom_fields"]["investor_type"] == ["Private Equity", "Venture Capital"]


# --- Lists ---


def test_list_crud_lifecycle(test_client):
    resp = test_client.post("/crm/lists", json={"name": "Austin Family Offices", "description": "Prospecting"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Austin Family Offices"
    assert body["contact_count"] == 0
    list_id = body["list_id"]

    resp = test_client.get(f"/crm/lists/{list_id}")
    assert resp.status_code == 200
    assert resp.json()["description"] == "Prospecting"

    resp = test_client.patch(f"/crm/lists/{list_id}", json={"name": "Renamed", "description": "New desc"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["description"] == "New desc"

    resp = test_client.get("/crm/lists")
    assert resp.status_code == 200
    assert any(l["list_id"] == list_id for l in resp.json())

    resp = test_client.delete(f"/crm/lists/{list_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"  # the pre-deletion summary

    resp = test_client.get(f"/crm/lists/{list_id}")
    assert resp.status_code == 404


def test_get_missing_list_returns_404(test_client):
    resp = test_client.get("/crm/lists/does-not-exist")
    assert resp.status_code == 404


def test_delete_missing_list_returns_404(test_client):
    resp = test_client.delete("/crm/lists/does-not-exist")
    assert resp.status_code == 404


def test_bulk_add_and_get_list_contacts(test_client):
    ids = []
    for name in ["Ada", "Grace"]:
        resp = test_client.post("/crm/contacts", json={"first_name": name})
        ids.append(resp.json()["crm_contact_id"])

    list_resp = test_client.post("/crm/lists", json={"name": "Test List"})
    list_id = list_resp.json()["list_id"]

    resp = test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": ids})
    assert resp.status_code == 200
    assert resp.json() == {"added": 2, "already_member": 0, "not_found": 0}

    resp = test_client.get(f"/crm/lists/{list_id}/contacts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {c["first_name"] for c in body["items"]} == {"Ada", "Grace"}

    resp = test_client.get(f"/crm/lists/{list_id}")
    assert resp.json()["contact_count"] == 2


def test_bulk_add_repeat_reports_already_member_not_duplicated(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Ada"})
    contact_id = resp.json()["crm_contact_id"]
    list_resp = test_client.post("/crm/lists", json={"name": "Test List"})
    list_id = list_resp.json()["list_id"]

    test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": [contact_id]})
    resp = test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": [contact_id]})

    assert resp.json() == {"added": 0, "already_member": 1, "not_found": 0}
    assert test_client.get(f"/crm/lists/{list_id}").json()["contact_count"] == 1


def test_bulk_add_to_missing_list_returns_404(test_client):
    resp = test_client.post("/crm/lists/does-not-exist/contacts/bulk-add", json={"contact_ids": []})
    assert resp.status_code == 404


def test_remove_one_contact_from_list(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Ada"})
    contact_id = resp.json()["crm_contact_id"]
    list_resp = test_client.post("/crm/lists", json={"name": "Test List"})
    list_id = list_resp.json()["list_id"]
    test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": [contact_id]})

    resp = test_client.delete(f"/crm/lists/{list_id}/contacts/{contact_id}")
    assert resp.status_code == 200
    assert resp.json()["contact_count"] == 0
    assert test_client.get(f"/crm/lists/{list_id}").json()["contact_count"] == 0

    # Underlying contact must still exist, unarchived.
    still_there = test_client.get(f"/crm/contacts/{contact_id}")
    assert still_there.status_code == 200
    assert still_there.json()["archived"] is False


def test_bulk_remove_from_list(test_client):
    ids = []
    for name in ["Ada", "Grace", "Marie"]:
        resp = test_client.post("/crm/contacts", json={"first_name": name})
        ids.append(resp.json()["crm_contact_id"])
    list_resp = test_client.post("/crm/lists", json={"name": "Test List"})
    list_id = list_resp.json()["list_id"]
    test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": ids})

    resp = test_client.post(f"/crm/lists/{list_id}/contacts/bulk-remove", json={"contact_ids": ids[:2]})
    assert resp.status_code == 200
    assert resp.json() == {"removed": 2}
    assert test_client.get(f"/crm/lists/{list_id}").json()["contact_count"] == 1


def test_delete_list_does_not_delete_contacts(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Ada"})
    contact_id = resp.json()["crm_contact_id"]
    list_resp = test_client.post("/crm/lists", json={"name": "Test List"})
    list_id = list_resp.json()["list_id"]
    test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": [contact_id]})

    resp = test_client.delete(f"/crm/lists/{list_id}")
    assert resp.status_code == 200

    still_there = test_client.get(f"/crm/contacts/{contact_id}")
    assert still_there.status_code == 200
    assert still_there.json()["archived"] is False


def test_same_contact_in_multiple_lists_via_api(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Jane"})
    contact_id = resp.json()["crm_contact_id"]
    list_a = test_client.post("/crm/lists", json={"name": "Austin Family Offices"}).json()["list_id"]
    list_b = test_client.post("/crm/lists", json={"name": "AI Investors"}).json()["list_id"]

    test_client.post(f"/crm/lists/{list_a}/contacts/bulk-add", json={"contact_ids": [contact_id]})
    test_client.post(f"/crm/lists/{list_b}/contacts/bulk-add", json={"contact_ids": [contact_id]})

    assert test_client.get(f"/crm/lists/{list_a}").json()["contact_count"] == 1
    assert test_client.get(f"/crm/lists/{list_b}").json()["contact_count"] == 1


def test_adding_to_list_does_not_change_contact_updated_at(test_client):
    resp = test_client.post("/crm/contacts", json={"first_name": "Ada"})
    contact_id = resp.json()["crm_contact_id"]
    before = test_client.get(f"/crm/contacts/{contact_id}").json()

    list_id = test_client.post("/crm/lists", json={"name": "Test List"}).json()["list_id"]
    test_client.post(f"/crm/lists/{list_id}/contacts/bulk-add", json={"contact_ids": [contact_id]})

    after = test_client.get(f"/crm/contacts/{contact_id}").json()
    assert after == before


# --- GET /crm/contacts/{id}/luma-registrations (contact detail Event History) --

from datetime import datetime, timezone  # noqa: E402

from app.dependencies import get_luma_sync_service  # noqa: E402
from app.models.luma import LumaApprovalStatus, LumaEvent, LumaMatchStatus, LumaRegistration  # noqa: E402
from app.repositories.luma_event_store import MemoryLumaEventStore  # noqa: E402
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore  # noqa: E402
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore  # noqa: E402
from app.services.luma_sync_service import LumaSyncService  # noqa: E402


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def _make_event(event_id: str, name: str) -> LumaEvent:
    return LumaEvent(luma_event_id=event_id, name=name, synced_at=_now(), updated_at=_now())


def _make_registration(**overrides) -> LumaRegistration:
    defaults = dict(
        luma_guest_id="gst-1", luma_event_id="evt-1", crm_contact_id="contact-1",
        match_status=LumaMatchStatus.MATCHED, approval_status=LumaApprovalStatus.APPROVED,
        synced_at=_now(), updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


@pytest.fixture
def luma_test_setup():
    crm_service = CrmService(contact_store=MemoryCrmContactStore())
    event_store = MemoryLumaEventStore()
    registration_store = MemoryLumaRegistrationStore()
    luma_service = LumaSyncService(
        crm_service=crm_service, event_store=event_store, registration_store=registration_store,
        mapping_store=MemoryLumaQuestionMappingStore(), activity_log=crm_service.activity_log,
    )

    app = FastAPI()
    app.include_router(crm_router)
    app.dependency_overrides[get_crm_service] = lambda: crm_service
    app.dependency_overrides[get_luma_sync_service] = lambda: luma_service

    with TestClient(app) as client:
        yield client, crm_service, event_store, registration_store


async def _create_contact(crm_service, **overrides) -> str:
    contact = await crm_service.create_contact_from_import({"first_name": "Ada", **overrides})
    return contact.crm_contact_id


def test_luma_registrations_route_returns_404_for_unknown_contact(luma_test_setup):
    client, _, _, _ = luma_test_setup
    resp = client.get("/crm/contacts/does-not-exist/luma-registrations")
    assert resp.status_code == 404


def test_luma_registrations_route_returns_empty_list_for_contact_with_none(luma_test_setup):
    client, crm_service, _, _ = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))

    resp = client.get(f"/crm/contacts/{contact_id}/luma-registrations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_luma_registrations_route_joins_event_name_and_omits_raw_answers(luma_test_setup):
    client, crm_service, event_store, registration_store = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    asyncio.run(event_store.save(_make_event("evt-1", "Hot Shot Investor Dinner ATX")))
    asyncio.run(
        registration_store.save(
            _make_registration(
                crm_contact_id=contact_id, registered_at=_now(),
                registration_answers=[{"label": "Investor Type", "question_type": "multi-select", "value": ["Angel Investor"]}],
            )
        )
    )

    resp = client.get(f"/crm/contacts/{contact_id}/luma-registrations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["event_name"] == "Hot Shot Investor Dinner ATX"
    assert body[0]["luma_event_id"] == "evt-1"
    assert body[0]["approval_status"] == "approved"
    assert "registration_answers" not in body[0]
    assert "luma_guest_id" not in body[0]


def test_luma_registrations_route_reflects_checked_in_at_when_present(luma_test_setup):
    client, crm_service, event_store, registration_store = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    asyncio.run(event_store.save(_make_event("evt-1", "Hot Shot Investor Dinner ATX")))
    asyncio.run(
        registration_store.save(
            _make_registration(crm_contact_id=contact_id, registered_at=_now(), checked_in_at=_now())
        )
    )

    body = client.get(f"/crm/contacts/{contact_id}/luma-registrations").json()
    assert body[0]["checked_in_at"] is not None


def test_luma_registrations_route_never_reports_checked_in_for_approved_only(luma_test_setup):
    client, crm_service, event_store, registration_store = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    asyncio.run(event_store.save(_make_event("evt-1", "Hot Shot Investor Dinner ATX")))
    asyncio.run(
        registration_store.save(
            _make_registration(crm_contact_id=contact_id, approval_status=LumaApprovalStatus.APPROVED, registered_at=_now())
        )
    )

    body = client.get(f"/crm/contacts/{contact_id}/luma-registrations").json()
    assert body[0]["approval_status"] == "approved"
    assert body[0]["checked_in_at"] is None


def test_luma_registrations_route_orders_multiple_events_newest_first(luma_test_setup):
    client, crm_service, event_store, registration_store = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    asyncio.run(event_store.save(_make_event("evt-old", "Older Dinner")))
    asyncio.run(event_store.save(_make_event("evt-new", "Newer Dinner")))
    asyncio.run(
        registration_store.save(
            _make_registration(
                luma_guest_id="gst-old", luma_event_id="evt-old", crm_contact_id=contact_id,
                registered_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
    )
    asyncio.run(
        registration_store.save(
            _make_registration(
                luma_guest_id="gst-new", luma_event_id="evt-new", crm_contact_id=contact_id,
                registered_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        )
    )

    body = client.get(f"/crm/contacts/{contact_id}/luma-registrations").json()
    assert [e["event_name"] for e in body] == ["Newer Dinner", "Older Dinner"]


def test_luma_registrations_route_reflects_declined_status(luma_test_setup):
    client, crm_service, event_store, registration_store = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    asyncio.run(event_store.save(_make_event("evt-1", "Hot Shot Investor Dinner ATX")))
    asyncio.run(
        registration_store.save(
            _make_registration(crm_contact_id=contact_id, approval_status=LumaApprovalStatus.DECLINED, registered_at=_now())
        )
    )

    body = client.get(f"/crm/contacts/{contact_id}/luma-registrations").json()
    assert body[0]["approval_status"] == "declined"


def test_get_contact_route_itself_is_unaffected_by_luma_route_addition(luma_test_setup):
    """Opening a contact (GET /crm/contacts/{id}) must never write anything --
    confirms the new route lives entirely alongside the existing one without
    changing its behavior."""
    client, crm_service, _, _ = luma_test_setup
    contact_id = asyncio.run(_create_contact(crm_service))
    before = client.get(f"/crm/contacts/{contact_id}").json()
    client.get(f"/crm/contacts/{contact_id}/luma-registrations")
    after = client.get(f"/crm/contacts/{contact_id}").json()
    assert before == after
