"""
Route-level tests for /crm -- exercises just the CRM router against a
fresh FastAPI app, isolated from app.main's real SQLite file.
"""

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
    assert contacts[0]["thesis_dietary_preferences"] == "Vegan"


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
