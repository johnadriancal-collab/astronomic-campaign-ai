"""
Full multi-turn conversational tests through the REAL POST /astro/command
route -- not just individual parser functions. Each test simulates exactly
what the frontend is expected to do: capture `query` + `intent` from a
resolved response and resend them as `context` on the next call. No backend
session state exists anywhere in this call graph -- see
app/services/astro_parser.py's Phase 1.1 module docstring.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.astro import router as astro_router
from app.dependencies import get_crm_service
from app.models.crm import CustomFieldType
from app.services.crm_service import CrmService


@pytest.fixture
def seeded_service():
    return CrmService()


async def _seed(service: CrmService):
    await service.create_custom_field(
        field_key="investor_type", label="Investor Type", field_type=CustomFieldType.MULTI_SELECT,
        options=[
            "Angel Investor", "Family Office", "Fund LP", "I sponsor deals that I find",
            "Institutional Investor", "Invest with group of Angels",
            "Participate in syndicated investments", "Private Equity", "Private Investor",
            "Venture Capital",
        ],
    )
    await service.create_custom_field(
        field_key="check_size_personal", label="Check Size (Personal)", field_type=CustomFieldType.MULTI_SELECT,
        options=[
            "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
            "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+", "Other:",
        ],
    )
    await service.create_custom_field(
        field_key="investment_industry", label="Investment Industry", field_type=CustomFieldType.MULTI_SELECT,
    )

    contacts = {}
    contacts["ada"] = await service.create_contact({
        "first_name": "Ada", "last_name": "Lovelace", "city": "Austin", "state": "Texas",
        "thesis_investor_mode_manual_override": True,
        "custom_fields": {
            "investor_type": ["Family Office"],
            "check_size_personal": ["$100k - $250k"],
            "investment_industry": ["Artificial Intelligence / Machine Learning"],
        },
    })
    contacts["emmy"] = await service.create_contact({
        "first_name": "Emmy", "last_name": "Noether", "city": "Houston", "state": "Texas",
        "thesis_investor_mode_manual_override": True,
        "custom_fields": {
            "investor_type": ["Family Office"],
            "check_size_personal": ["$50k - $100k"],
            "investment_industry": ["Artificial Intelligence / Machine Learning"],
        },
    })
    contacts["grace"] = await service.create_contact({
        "first_name": "Grace", "last_name": "Hopper", "city": "Austin", "state": "Texas",
        "thesis_investor_mode_manual_override": True,
        "custom_fields": {
            "investor_type": ["Angel Investor"],
            "check_size_personal": ["$10k - $25k"],
            "investment_industry": ["Aerospace & Defense"],
        },
    })
    contacts["marie"] = await service.create_contact({
        "first_name": "Marie", "last_name": "Curie", "city": "Dallas", "state": "Texas",
        "thesis_investor_mode_manual_override": True,
        "custom_fields": {"investor_type": ["Family Office"], "check_size_personal": ["$1M - $2M"]},
    })
    contacts["rosalind"] = await service.create_contact({
        "first_name": "Rosalind", "last_name": "Franklin", "city": "San Francisco", "state": "California",
        "thesis_investor_mode_manual_override": True, "thesis_investor_mode": "Privately",
        "custom_fields": {"investor_type": ["Institutional Investor"]},
    })
    return contacts


@pytest.fixture
def client(seeded_service):
    app = FastAPI()
    app.include_router(astro_router)
    app.dependency_overrides[get_crm_service] = lambda: seeded_service
    with TestClient(app) as c:
        yield c, seeded_service


def send(client, text, context=None):
    body = {"text": text}
    if context is not None:
        body["context"] = context
    resp = client.post("/astro/command", json=body)
    assert resp.status_code == 200
    return resp.json()


def next_context(body):
    """Exactly what the frontend is expected to carry forward -- see
    app/models/astro.py's AstroCommandContext."""
    return {"query": body["query"], "intent": body["intent"]}


# --- Example A: the exact scenario from the Phase 1.1 design doc ---


@pytest.mark.asyncio
async def test_conversation_example_a_full_flow(client):
    c, service = client
    ids = await _seed(service)

    # Turn 1: standalone -- Texas family offices interested in AI (Ada + Emmy)
    r1 = send(c, "Find family offices in Texas interested in AI")
    assert r1["intent"] == "search_contacts"
    assert r1["total"] == 2
    assert {ct["crm_contact_id"] for ct in r1["contacts"]} == {ids["ada"].crm_contact_id, ids["emmy"].crm_contact_id}
    assert r1["operation"] is None  # standalone, not a refinement

    # Turn 2: "Only Austin" -- narrows to Ada (Emmy is in Houston), Investor
    # Type + Industry filters from turn 1 must still be active.
    r2 = send(c, "Only Austin", context=next_context(r1))
    assert r2["intent"] == "search_contacts"
    assert r2["operation"] == "replace"
    assert r2["changed_field"] == "city"
    assert r2["total"] == 1
    assert [ct["crm_contact_id"] for ct in r2["contacts"]] == [ids["ada"].crm_contact_id]
    assert r2["message"] == "Showing 1 contact in Austin. Your other filters are unchanged."
    fields_in_query = {f["field"] for f in r2["query"]["filters"]}
    assert fields_in_query == {"custom:investor_type", "custom:investment_industry", "state", "city"}

    # Turn 3: "Only $100k+" -- Ada's check size ($100k-$250k) still qualifies.
    r3 = send(c, "Only $100k+", context=next_context(r2))
    assert r3["operation"] == "replace"
    assert r3["changed_field"] == "custom:check_size_personal"
    assert r3["total"] == 1
    assert r3["message"] == "Added a $100k+ check-size filter. 1 contact match."

    # Turn 4: "How many are left?" -- same filters, intent flips to count.
    r4 = send(c, "How many are left?", context=next_context(r3))
    assert r4["intent"] == "count_contacts"
    assert r4["operation"] == "change_intent"
    assert r4["total"] == 1
    assert r4.get("contacts") is None
    assert r4["message"] == "1 contact match your current filters."
    assert r4["query"]["filters"] == r3["query"]["filters"]  # filters genuinely unchanged

    # Turn 5: "Show them again" -- back to search, same filters, same result.
    r5 = send(c, "Show them again", context=next_context(r4))
    assert r5["intent"] == "search_contacts"
    assert r5["operation"] == "change_intent"
    assert [ct["crm_contact_id"] for ct in r5["contacts"]] == [ids["ada"].crm_contact_id]


# --- Example B: ADD / REMOVE(no-op) / RESET ---


@pytest.mark.asyncio
async def test_conversation_example_b_add_remove_reset(client):
    c, service = client
    ids = await _seed(service)

    r1 = send(c, "Show institutional investors")
    assert r1["total"] == 1
    assert [ct["crm_contact_id"] for ct in r1["contacts"]] == [ids["rosalind"].crm_contact_id]

    # "Include family offices too" -- unions into investor_type, now matches
    # Rosalind (Institutional Investor) + Ada/Emmy/Marie (Family Office) = 4.
    r2 = send(c, "Include family offices too", context=next_context(r1))
    assert r2["operation"] == "add"
    assert r2["total"] == 4
    returned_ids = {ct["crm_contact_id"] for ct in r2["contacts"]}
    assert returned_ids == {ids["rosalind"].crm_contact_id, ids["ada"].crm_contact_id, ids["emmy"].crm_contact_id, ids["marie"].crm_contact_id}
    investor_type_condition = next(f for f in r2["query"]["filters"] if f["field"] == "custom:investor_type")
    assert investor_type_condition["value"] == ["Institutional Investor", "Family Office"]
    assert r2["message"] == "Added Family Office to your Investor Type filter. 4 contacts match."

    # "Remove the check size filter" -- none exists yet; harmless no-op, same 4.
    r3 = send(c, "Remove the check size filter", context=next_context(r2))
    assert r3["operation"] == "remove"
    assert r3["total"] == 4
    assert r3["query"]["filters"] == r2["query"]["filters"]

    # "Start over" -- clears everything, shows all 5 seeded contacts.
    r4 = send(c, "Start over", context=next_context(r3))
    assert r4["operation"] == "reset"
    assert r4["query"]["filters"] == []
    assert r4["total"] == 5


# --- Example C: ambiguous refinement leaves context (and results) completely unchanged ---


@pytest.mark.asyncio
async def test_conversation_example_c_ambiguous_refinement_is_a_true_no_op(client):
    c, service = client
    ids = await _seed(service)

    r1 = send(c, "Find family offices in Austin")
    assert r1["total"] == 1
    assert [ct["crm_contact_id"] for ct in r1["contacts"]] == [ids["ada"].crm_contact_id]

    r2 = send(c, "Only good ones", context=next_context(r1))
    assert r2["intent"] == "unresolved"
    assert r2["operation"] is None
    assert r2["changed_field"] is None
    assert r2["contacts"] is None  # unresolved never dumps a contact list
    # The PROOF: the exact FilterQuery from turn 1 comes back byte-for-byte unchanged.
    assert r2["query"] == r1["query"]
    # And re-running that unchanged query still gives the same, correct total.
    assert r2["total"] == r1["total"] == 1
    assert "good ones" in r2["unresolved_phrase"]

    # A THIRD turn, refining against the SAME context the frontend already
    # had before the ambiguous turn (correct frontend behavior: an unresolved
    # response's own intent is "unresolved", which isn't a valid context
    # intent -- the frontend must never store it; r1's context is still the
    # right thing to carry forward). Proves the unchanged context is still
    # fully usable, not just equal in isolation.
    r3 = send(c, "Only $100k+", context=next_context(r1))
    assert r3["intent"] == "search_contacts"
    assert r3["operation"] == "replace"
    assert r3["total"] == 1
    fields_in_query = {f["field"] for f in r3["query"]["filters"]}
    assert fields_in_query == {"custom:investor_type", "city", "custom:check_size_personal"}


# --- Standalone commands remain unaffected by the presence of context (requirement) ---


@pytest.mark.asyncio
async def test_standalone_command_with_context_present_starts_a_brand_new_query(client):
    """A fully-formed standalone sentence always wins over any prior context --
    "Find X" with context present must behave IDENTICALLY to Phase 1 with no
    context at all, discarding whatever was there before."""
    c, service = client
    ids = await _seed(service)

    r1 = send(c, "Find family offices in Austin")
    assert r1["total"] == 1

    # A brand-new, fully standalone-parseable command, WITH context attached.
    r2 = send(c, "Show institutional investors", context=next_context(r1))
    assert r2["operation"] is None  # NOT a refinement -- resolved entirely by the standalone parser
    assert r2["total"] == 1
    assert [ct["crm_contact_id"] for ct in r2["contacts"]] == [ids["rosalind"].crm_contact_id]
    # The new query has ONLY the institutional-investor filter -- turn 1's
    # Family Office/Austin filters were fully discarded, not merged.
    assert len(r2["query"]["filters"]) == 1
