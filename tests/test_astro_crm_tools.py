"""
AstroCrmTools tests -- Astro AI Phase 2's read-only CRM tool surface.
Exercised against a REAL CrmService (in-memory stores), never a fake/mock
of the query engine itself, so these tests prove the actual
query_contacts/get_filterable_fields path behaves as claimed -- only
Claude itself is out of scope here (that's what test_astro_ai_service.py's
FakeClaudeClient-driven tool-loop tests cover).
"""

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.services.astro_crm_tools import (
    SEARCH_RESULT_LIMIT,
    AstroCrmTools,
)
from app.services.crm_service import CrmService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(
        crm_contact_id=str(uuid.uuid4()),
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return CrmContact(**defaults)


@pytest_asyncio.fixture
async def crm_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="investor_type",
            label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["Angel Investor", "Family Office", "Venture Capital"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    service = CrmService(custom_field_store=custom_field_store)

    await service.contact_store.create(
        make_contact(
            first_name="Alice",
            last_name="Angel",
            city="Austin",
            state="Texas",
            company="Angel Co",
            custom_fields={"investor_type": ["Angel Investor"]},
        )
    )
    await service.contact_store.create(
        make_contact(
            first_name="Bob",
            last_name="Angel",
            city="Denver",
            state="Colorado",
            company="Bob Ventures",
            custom_fields={"investor_type": ["Angel Investor"]},
        )
    )
    await service.contact_store.create(
        make_contact(
            first_name="Carol",
            last_name="Family",
            city="Austin",
            state="Texas",
            company="Family Office Co",
            custom_fields={"investor_type": ["Family Office"]},
        )
    )
    await service.contact_store.create(
        make_contact(first_name="John", last_name="Smith", company="Acme", city="Austin")
    )
    return service


@pytest.fixture
def tools(crm_service):
    return AstroCrmTools(crm_service)


# --- count_crm_contacts -----------------------------------------------------


async def test_count_all_contacts(tools):
    result = await tools.dispatch("count_crm_contacts", {"filters": []})
    assert result == {"total": 4}


async def test_count_angel_investors_uses_investor_type_field(tools):
    """The exact filter the approved architecture specifies for 'how many
    angel investors do we have' -- custom:investor_type contains_any
    ["Angel Investor"], never title-text matching."""
    result = await tools.dispatch(
        "count_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]}]},
    )
    assert result == {"total": 2}


async def test_count_angel_investors_in_austin_combines_filters(tools):
    """The 'how many of those are in Austin' follow-up -- both criteria
    together, AND logic."""
    result = await tools.dispatch(
        "count_crm_contacts",
        {
            "filters": [
                {"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]},
                {"field": "city", "operator": "eq", "value": "Austin"},
            ],
            "logic": "AND",
        },
    )
    assert result == {"total": 1}  # only Alice


async def test_count_returns_zero_for_no_matches_honestly(tools):
    result = await tools.dispatch(
        "count_crm_contacts",
        {"filters": [{"field": "city", "operator": "eq", "value": "Nowhere"}]},
    )
    assert result == {"total": 0}


# --- search_crm_contacts ----------------------------------------------------


async def test_search_returns_minimal_projection(tools):
    result = await tools.dispatch(
        "search_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Family Office"]}]},
    )
    assert result["total"] == 1
    assert result["returned"] == 1
    contact = result["contacts"][0]
    assert contact["name"] == "Carol Family"
    assert contact["city"] == "Austin"
    # Minimal projection -- no raw custom_fields dump, no source_snapshot, etc.
    assert set(contact.keys()) == {"name", "title", "company", "city", "state", "email"}


async def test_search_result_cannot_exceed_the_hard_limit(tools, crm_service):
    for i in range(SEARCH_RESULT_LIMIT + 5):
        await crm_service.contact_store.create(make_contact(first_name=f"Bulk{i}", last_name="Contact", city="Austin"))

    result = await tools.dispatch("search_crm_contacts", {"filters": [{"field": "city", "operator": "eq", "value": "Austin"}], "limit": 999})

    assert result["total"] > SEARCH_RESULT_LIMIT
    assert result["returned"] == SEARCH_RESULT_LIMIT
    assert len(result["contacts"]) == SEARCH_RESULT_LIMIT


async def test_search_empty_results_are_explicit(tools):
    result = await tools.dispatch("search_crm_contacts", {"filters": [{"field": "city", "operator": "eq", "value": "Nowhere"}]})
    assert result == {"total": 0, "returned": 0, "contacts": []}


# --- get_crm_contact ---------------------------------------------------------


async def test_get_contact_found_returns_fuller_projection(tools):
    result = await tools.dispatch("get_crm_contact", {"first_name": "John", "last_name": "Smith"})
    assert result["status"] == "found"
    assert result["contact"]["name"] == "John Smith"
    assert result["contact"]["company"] == "Acme"


async def test_get_contact_not_found_is_explicit(tools):
    result = await tools.dispatch("get_crm_contact", {"first_name": "Nobody", "last_name": "Nowhere"})
    assert result == {"status": "not_found"}


async def test_get_contact_ambiguous_never_arbitrarily_picks_one(tools):
    """Two 'Angel'-lastname contacts (Alice Angel, Bob Angel) -- a bare
    last-name lookup must report ambiguity, not silently return one."""
    result = await tools.dispatch("get_crm_contact", {"last_name": "Angel"})
    assert result["status"] == "ambiguous"
    assert result["total"] == 2
    names = {c["name"] for c in result["candidates"]}
    assert names == {"Alice Angel", "Bob Angel"}


async def test_get_contact_disambiguated_by_company(tools):
    result = await tools.dispatch("get_crm_contact", {"last_name": "Angel", "company": "Angel Co"})
    assert result["status"] == "found"
    assert result["contact"]["name"] == "Alice Angel"


async def test_get_contact_requires_some_identifying_detail(tools):
    result = await tools.dispatch("get_crm_contact", {})
    assert result["error"] == "invalid_filter"


# --- security / validation boundaries --------------------------------------


async def test_unknown_field_is_rejected_not_silently_ignored(tools):
    result = await tools.dispatch("count_crm_contacts", {"filters": [{"field": "ssn", "operator": "eq", "value": "x"}]})
    assert result["error"] == "invalid_filter"
    assert "ssn" in result["message"]


async def test_unknown_operator_is_rejected(tools):
    result = await tools.dispatch(
        "count_crm_contacts", {"filters": [{"field": "city", "operator": "sql_injection", "value": "x"}]}
    )
    assert result["error"] == "invalid_filter"


async def test_invalid_select_option_is_rejected(tools):
    result = await tools.dispatch(
        "count_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Not A Real Type"]}]},
    )
    assert result["error"] == "invalid_filter"


async def test_unknown_tool_name_is_rejected(tools):
    result = await tools.dispatch("delete_all_contacts", {})
    assert result == {"error": "unknown_tool", "message": "'delete_all_contacts' is not an available tool."}


async def test_apollo_and_campaign_tool_names_are_not_available(tools):
    for name in ["search_apollo", "create_campaign", "send_email", "update_contact", "delete_contact"]:
        result = await tools.dispatch(name, {})
        assert result["error"] == "unknown_tool"


async def test_dispatch_never_raises_on_bad_input(tools):
    """A malformed tool call must surface as a structured error the loop
    can feed back to Claude, never as an unhandled exception that would
    crash the whole chat turn."""
    result = await tools.dispatch("count_crm_contacts", {"filters": "not-a-list"})
    assert "error" in result


# --- field vocabulary (what Claude is told exists) --------------------------


async def test_describe_available_fields_includes_investor_type_with_its_real_options(tools):
    description = await tools.describe_available_fields()
    assert "custom:investor_type" in description
    assert "Angel Investor" in description
    assert "Family Office" in description


# --- genuine tool/database failure, distinct from bad input -----------------


class _BrokenCrmService:
    """Simulates a real infrastructure failure (e.g. the SQLite file being
    unreachable) -- distinct from a validation problem, which is a client
    (Claude) input error, not a database error."""

    async def get_filterable_fields(self):
        raise RuntimeError("database connection lost")

    async def query_contacts(self, query):
        raise RuntimeError("database connection lost")


async def test_genuine_database_failure_returns_tool_failed_not_a_crash():
    broken_tools = AstroCrmTools(_BrokenCrmService())
    result = await broken_tools.dispatch("count_crm_contacts", {"filters": []})
    assert result == {"error": "tool_failed", "message": "The CRM lookup failed -- please try again."}


# --- static import boundary: no write/Apollo/campaign/mailbox code reachable -


def test_astro_crm_tools_never_imports_write_apollo_campaign_or_mailbox_code():
    tree = ast.parse(Path("app/services/astro_crm_tools.py").read_text())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden = {
        "app.apollo",
        "app.apollo.client",
        "app.apollo.contacts",
        "app.apollo.people",
        "app.apollo.sequences",
        "app.apollo.messages",
        "app.services.campaign_service",
        "app.services.prospect_ranker",
        "app.services.mail_campaign_service",
        "app.services.mailbox_service",
        "app.agents.campaign_agent",
    }
    hit = forbidden & imported_modules
    assert not hit, f"astro_crm_tools.py imports forbidden module(s): {hit}"
