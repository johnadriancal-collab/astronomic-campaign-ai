"""
Route-level tests for POST /astro/command. Exercises the real astro router
against a fresh FastAPI app + in-memory CrmService (same isolation pattern
as test_campaign_lifecycle_endpoints.py) -- no real SQLite file, no other
test's store state.

Also proves (test_astro_never_touches_claude_client) that this endpoint's
entire call graph never instantiates ClaudeClient, and
(test_astro_module_never_imports_claude_or_httpx) that the astro modules
don't even import anything Claude/network-related in the first place --
two independent, complementary proofs of the "no Claude/Anthropic call"
requirement.
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.astro import router as astro_router
from app.dependencies import get_crm_service
from app.models.crm import CustomFieldType, FilterCondition, FilterQuery
from app.services.crm_service import CrmService


@pytest.fixture
def seeded_service():
    service = CrmService()
    return service


async def _seed(service: CrmService):
    await service.create_custom_field(
        field_key="investor_type",
        label="Investor Type",
        field_type=CustomFieldType.MULTI_SELECT,
        options=[
            "Angel Investor", "Family Office", "Fund LP", "I sponsor deals that I find",
            "Institutional Investor", "Invest with group of Angels",
            "Participate in syndicated investments", "Private Equity", "Private Investor",
            "Venture Capital",
        ],
    )
    await service.create_custom_field(
        field_key="check_size_personal",
        label="Check Size (Personal)",
        field_type=CustomFieldType.MULTI_SELECT,
        options=[
            "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
            "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+", "Other:",
        ],
    )
    await service.create_custom_field(
        field_key="investment_industry", label="Investment Industry", field_type=CustomFieldType.MULTI_SELECT,
    )

    # Ada: Austin-based Family Office, $100k-$250k personal check size, interested in AI.
    ada = await service.create_contact(
        {
            "first_name": "Ada", "last_name": "Lovelace", "city": "Austin", "state": "Texas",
            # thesis_investor_mode_manual_override=True with no thesis_investor_mode set
            # deliberately prevents create_contact()'s own auto-derivation (Family Office is
            # in INSTITUTIONAL_INVESTOR_TYPES, so without this override Ada would silently
            # pick up thesis_investor_mode="Institutionally" and pollute the investor-mode
            # tests below with a contact that has nothing to do with what they're isolating).
            "thesis_investor_mode_manual_override": True,
            "custom_fields": {
                "investor_type": ["Family Office"],
                "check_size_personal": ["$100k - $250k"],
                "investment_industry": ["Artificial Intelligence / Machine Learning"],
            },
        }
    )
    # Grace: Austin-based Angel Investor, smaller check size, interested in Aerospace & Defense.
    grace = await service.create_contact(
        {
            "first_name": "Grace", "last_name": "Hopper", "city": "Austin", "state": "Texas",
            "thesis_investor_mode_manual_override": True,
            "custom_fields": {
                "investor_type": ["Angel Investor"],
                "check_size_personal": ["$10k - $25k"],
                "investment_industry": ["Aerospace & Defense"],
            },
        }
    )
    # Marie: Dallas-based Family Office, large check size, no industry interest recorded.
    marie = await service.create_contact(
        {
            "first_name": "Marie", "last_name": "Curie", "city": "Dallas", "state": "Texas",
            "thesis_investor_mode_manual_override": True,
            "custom_fields": {"investor_type": ["Family Office"], "check_size_personal": ["$1M - $2M"]},
        }
    )
    # Rosalind: San Francisco-based Institutional Investor TAG, but a human has explicitly
    # corrected her actual thesis_investor_mode to "Privately" -- a real way these two fields
    # diverge (the tag says one thing, a human's manual override says another). Proves
    # "Show institutional investors" (the tag) and "invests institutionally" (the mode) are
    # genuinely independent, not just aliases of each other.
    rosalind = await service.create_contact(
        {
            "first_name": "Rosalind", "last_name": "Franklin", "city": "San Francisco", "state": "California",
            "thesis_investor_mode_manual_override": True,
            "thesis_investor_mode": "Privately",
            "custom_fields": {"investor_type": ["Institutional Investor"]},
        }
    )
    # Katherine: her investor_type tag is Angel Investor (a PRIVATE-classified type -- auto-
    # derivation would say "Privately"), but a human has manually corrected her actual
    # thesis_investor_mode to "Institutionally". The inverse divergence from Rosalind's.
    katherine = await service.create_contact(
        {
            "first_name": "Katherine", "last_name": "Johnson", "city": "Austin", "state": "Texas",
            "thesis_investor_mode": "Institutionally",
            "thesis_investor_mode_manual_override": True,
            "custom_fields": {"investor_type": ["Angel Investor"]},
        }
    )
    return {"ada": ada, "grace": grace, "marie": marie, "rosalind": rosalind, "katherine": katherine}


@pytest.fixture
def test_client(seeded_service):
    app = FastAPI()
    app.include_router(astro_router)
    app.dependency_overrides[get_crm_service] = lambda: seeded_service
    with TestClient(app) as client:
        yield client, seeded_service


@pytest.mark.asyncio
async def test_search_family_offices_in_austin(test_client):
    client, service = test_client
    contacts = await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find family offices in Austin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "search_contacts"
    assert body["total"] == 1
    assert [c["crm_contact_id"] for c in body["contacts"]] == [contacts["ada"].crm_contact_id]


@pytest.mark.asyncio
async def test_count_family_offices_in_austin_omits_contacts(test_client):
    client, service = test_client
    await _seed(service)

    resp = client.post("/astro/command", json={"text": "How many family offices are in Austin?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "count_contacts"
    assert body["total"] == 1
    assert body["contacts"] is None


@pytest.mark.asyncio
async def test_search_investors_interested_in_ai_alias(test_client):
    client, service = test_client
    contacts = await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find investors interested in AI"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [c["crm_contact_id"] for c in body["contacts"]] == [contacts["ada"].crm_contact_id]


@pytest.mark.asyncio
async def test_search_investors_with_100k_plus_check_size(test_client):
    """Ada ($100k-$250k) and Marie ($1M-$2M) both qualify; Grace ($10k-$25k) does not."""
    client, service = test_client
    contacts = await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find investors with $100k+ check sizes"})
    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {c["crm_contact_id"] for c in body["contacts"]}
    assert returned_ids == {contacts["ada"].crm_contact_id, contacts["marie"].crm_contact_id}


@pytest.mark.asyncio
async def test_institutional_investors_matches_investor_type_tag_only(test_client):
    """Rosalind (investor_type tag) matches; Katherine (thesis_investor_mode=Institutionally,
    investor_type=Angel Investor) must NOT match -- proves the two fields stay separate."""
    client, service = test_client
    contacts = await _seed(service)

    resp = client.post("/astro/command", json={"text": "Show institutional investors"})
    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {c["crm_contact_id"] for c in body["contacts"]}
    assert returned_ids == {contacts["rosalind"].crm_contact_id}


@pytest.mark.asyncio
async def test_invests_institutionally_matches_investor_mode_only(test_client):
    """The inverse of the test above: Katherine (thesis_investor_mode) matches;
    Rosalind (investor_type tag only, no thesis_investor_mode set) does not."""
    client, service = test_client
    contacts = await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find investors who invest institutionally"})
    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {c["crm_contact_id"] for c in body["contacts"]}
    assert returned_ids == {contacts["katherine"].crm_contact_id}


@pytest.mark.asyncio
async def test_unresolved_phrase_returns_200_with_clarification_not_a_guess(test_client):
    client, service = test_client
    await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find high quality investors in Austin"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "unresolved"
    assert body["understood"] == {"City": "Austin"}
    assert "high quality" in body["unresolved_phrase"]
    assert body.get("contacts") is None
    assert body.get("total") is None


# --- prove Astro produces the SAME results as an equivalent direct CRM query ---


@pytest.mark.asyncio
async def test_astro_result_matches_equivalent_direct_crm_query(test_client):
    """No divergent filtering logic exists -- Astro's parsed FilterQuery, run directly
    through CrmService.query_contacts() (bypassing the route entirely), must return
    the exact same contact set as the route itself returns for the same command."""
    client, service = test_client
    await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find family offices in Austin"})
    astro_ids = {c["crm_contact_id"] for c in resp.json()["contacts"]}

    hand_built_query = FilterQuery(
        filters=[
            FilterCondition(field="custom:investor_type", operator="contains_any", value=["Family Office"]),
            FilterCondition(field="city", operator="eq", value="Austin"),
        ],
        logic="AND",
    )
    direct_page = await service.query_contacts(hand_built_query)
    direct_ids = {c.crm_contact_id for c in direct_page.items}

    assert astro_ids == direct_ids
    assert len(astro_ids) > 0  # sanity: not vacuously equal because both are empty


@pytest.mark.asyncio
async def test_astro_echoes_back_the_exact_filterquery_it_executed(test_client):
    """The 'query' field in the response is the literal FilterQuery Astro built and
    ran -- callers can inspect exactly what happened, not just trust 'understood_as'."""
    client, service = test_client
    await _seed(service)

    resp = client.post("/astro/command", json={"text": "Find family offices in Austin"})
    body = resp.json()
    returned_query = FilterQuery(**body["query"])
    direct_page = await service.query_contacts(returned_query)
    assert {c.crm_contact_id for c in direct_page.items} == {c["crm_contact_id"] for c in body["contacts"]}


# --- prove no Claude/Anthropic call occurs anywhere in this feature ---


@pytest.mark.asyncio
async def test_astro_never_touches_claude_client(test_client, monkeypatch):
    """Patches ClaudeClient.__init__ to fail the test if it's EVER instantiated, then
    exercises several real commands through the full route -- proves no code path in
    this feature reaches Claude at runtime, not just that it isn't imported."""
    import app.claude.client as client_module

    def _fail_if_constructed(*args, **kwargs):
        raise AssertionError("ClaudeClient must never be instantiated by the Astro Core command path")

    monkeypatch.setattr(client_module.ClaudeClient, "__init__", _fail_if_constructed)

    client, service = test_client
    await _seed(service)

    for text in [
        "Find family offices in Austin",
        "How many family offices are in Austin?",
        "Find investors interested in AI",
        "Find investors with $100k+ check sizes",
        "Show institutional investors",
        "Find high quality investors in Austin",  # unresolved path too
    ]:
        resp = client.post("/astro/command", json={"text": text})
        assert resp.status_code == 200


def test_astro_module_never_imports_claude_or_httpx():
    """Static structural check, independent of the runtime test above: neither astro
    module's import statements reference anything Claude/Anthropic/network-related."""
    for path in [
        Path("app/services/astro_parser.py"),
        Path("app/api/astro.py"),
        Path("app/models/astro.py"),
    ]:
        tree = ast.parse(path.read_text())
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        forbidden = {"httpx", "app.claude.client", "app.agents.campaign_agent", "app.services.prospect_ranker"}
        hit = forbidden & imported_modules
        assert not hit, f"{path} imports forbidden module(s): {hit}"
