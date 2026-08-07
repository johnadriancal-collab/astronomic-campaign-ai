"""
Route-level tests for More Filters (GET /crm/filterable-fields, POST
/crm/contacts/query) -- same fresh-FastAPI-app pattern as test_crm_api.py,
exercising the real CrmService end to end (real custom field definitions
created through the actual API, not hand-built fixtures) so these tests
also stand in for "combining core + thesis + custom fields."
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.crm import router as crm_router
from app.dependencies import get_crm_service
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.crm_service import CrmService


@pytest.fixture
def client():
    crm_service = CrmService(contact_store=MemoryCrmContactStore())
    app = FastAPI()
    app.include_router(crm_router)
    app.dependency_overrides[get_crm_service] = lambda: crm_service
    with TestClient(app) as c:
        yield c


def _create_custom_field(client, field_key, field_type, options=None):
    resp = client.post(
        "/crm/custom-fields",
        json={"field_key": field_key, "label": field_key, "field_type": field_type, "options": options or []},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_contact(client, **fields):
    resp = client.post("/crm/contacts", json=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _query(client, **body):
    resp = client.post("/crm/contacts/query", json=body)
    return resp


# --- GET /crm/filterable-fields ---


def test_filterable_fields_includes_core_and_custom(client):
    _create_custom_field(client, "investor_type", "multi_select", ["Angel Investor", "Family Office", "Venture Capital"])
    resp = client.get("/crm/filterable-fields")
    assert resp.status_code == 200
    keys = {f["key"] for f in resp.json()}
    assert "city" in keys
    assert "thesis_investor_mode" in keys
    assert "custom:investor_type" in keys


def test_filterable_fields_excludes_inactive_custom_field(client):
    field = _create_custom_field(client, "legacy_field", "text")
    client.patch(f"/crm/custom-fields/{field['crm_custom_field_id']}", json={"active": False})
    resp = client.get("/crm/filterable-fields")
    keys = {f["key"] for f in resp.json()}
    assert "custom:legacy_field" not in keys


# --- POST /crm/contacts/query: security/validation ---


def test_query_rejects_unknown_field(client):
    resp = _query(client, filters=[{"field": "not_a_real_field", "operator": "eq", "value": "x"}])
    assert resp.status_code == 400
    assert "Unknown filterable field" in resp.json()["detail"]


def test_query_rejects_disallowed_operator(client):
    resp = _query(client, filters=[{"field": "city", "operator": "gte", "value": "Austin"}])
    assert resp.status_code == 400


def test_query_rejects_invalid_value_for_closed_option_field(client):
    _create_custom_field(client, "investor_type", "multi_select", ["Angel Investor", "Family Office"])
    resp = _query(client, filters=[{"field": "custom:investor_type", "operator": "contains_any", "value": ["Not Real"]}])
    assert resp.status_code == 400


# --- Core field filtering ---


def test_query_filters_by_state(client):
    _create_contact(client, first_name="Ada", state="Texas")
    _create_contact(client, first_name="Grace", state="California")
    resp = _query(client, filters=[{"field": "state", "operator": "eq", "value": "Texas"}])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["first_name"] == "Ada"


def test_query_or_across_multi_value_single_field(client):
    _create_contact(client, first_name="Ada", state="Texas")
    _create_contact(client, first_name="Grace", state="California")
    _create_contact(client, first_name="Alan", state="Oregon")
    resp = _query(client, filters=[{"field": "state", "operator": "eq", "value": ["Texas", "California"]}])
    body = resp.json()
    assert body["total"] == 2
    assert {c["first_name"] for c in body["items"]} == {"Ada", "Grace"}


# --- Core + thesis + custom field combined ---


def test_query_combines_core_thesis_and_custom_fields(client):
    _create_custom_field(client, "investor_type", "multi_select", ["Family Office", "Venture Capital"])
    match = _create_contact(
        client, first_name="Match", state="Texas", thesis_investor_mode="Institutionally",
        custom_fields={"investor_type": ["Family Office"]},
    )
    _create_contact(
        client, first_name="WrongState", state="California", thesis_investor_mode="Institutionally",
        custom_fields={"investor_type": ["Family Office"]},
    )
    _create_contact(
        client, first_name="WrongCustom", state="Texas", thesis_investor_mode="Institutionally",
        custom_fields={"investor_type": ["Venture Capital"]},
    )
    resp = _query(
        client,
        filters=[
            {"field": "state", "operator": "eq", "value": "Texas"},
            {"field": "thesis_investor_mode", "operator": "eq", "value": "Institutionally"},
            {"field": "custom:investor_type", "operator": "contains_any", "value": ["Family Office"]},
        ],
        logic="AND",
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["crm_contact_id"] == match["crm_contact_id"]


# --- Ordinal filtering through the real registry (Check Size / Age Range) ---


def test_check_size_gte_through_real_registry(client):
    _create_custom_field(
        client, "check_size_personal", "multi_select",
        ["$1k - $10k", "$1M - $2M", "$5M - $10M", "$10M+", "Other:"],
    )
    small = _create_contact(client, first_name="Small", custom_fields={"check_size_personal": ["$1k - $10k"]})
    big = _create_contact(client, first_name="Big", custom_fields={"check_size_personal": ["$5M - $10M"]})
    other_only = _create_contact(client, first_name="OtherOnly", custom_fields={"check_size_personal": ["Other:"]})

    resp = _query(client, filters=[{"field": "custom:check_size_personal", "operator": "gte", "value": "$1M - $2M"}])
    body = resp.json()
    ids = {c["crm_contact_id"] for c in body["items"]}
    assert big["crm_contact_id"] in ids
    assert small["crm_contact_id"] not in ids
    assert other_only["crm_contact_id"] not in ids


def test_check_size_gte_other_rejected_through_real_registry(client):
    _create_custom_field(client, "check_size_personal", "multi_select", ["$1M - $2M", "Other:"])
    resp = _query(client, filters=[{"field": "custom:check_size_personal", "operator": "gte", "value": "Other:"}])
    assert resp.status_code == 400


# --- Pagination / sorting ---


def test_query_pagination(client):
    for i in range(5):
        _create_contact(client, first_name=f"Contact{i}", state="Texas")
    resp = _query(client, filters=[{"field": "state", "operator": "eq", "value": "Texas"}], page=2, page_size=2)
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 2


def test_query_sort_by_last_name(client):
    _create_contact(client, first_name="Z", last_name="Zephyr")
    _create_contact(client, first_name="A", last_name="Adams")
    resp = _query(client, sort={"field": "last_name", "direction": "asc"})
    body = resp.json()
    assert [c["first_name"] for c in body["items"]] == ["A", "Z"]


# --- Zero results ---


def test_query_zero_results(client):
    _create_contact(client, first_name="Ada", state="Texas")
    resp = _query(client, filters=[{"field": "state", "operator": "eq", "value": "Nowhere"}])
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


# --- Existing Contacts search (GET /crm/contacts) untouched ---


def test_existing_contacts_search_still_works(client):
    """More Filters is additive -- the original keyword/city/investor_mode search
    on GET /crm/contacts must behave exactly as before."""
    _create_contact(client, first_name="Ada", city="Austin", thesis_investor_mode="Privately")
    _create_contact(client, first_name="Grace", city="Dallas", thesis_investor_mode="Institutionally")
    resp = client.get("/crm/contacts", params={"city": "Austin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["first_name"] == "Ada"
