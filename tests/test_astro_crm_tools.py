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

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.services.astro_crm_tools import (
    EXPORT_MAX_CONTACTS,
    SEARCH_RESULT_LIMIT,
    AstroCrmTools,
)
from app.services.astro_export_store import AstroExportStore
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
    john = make_contact(first_name="John", last_name="Smith", company="Acme", city="Austin")
    await service.contact_store.create(john)

    all_contacts = await service.contact_store.list()
    by_name = {f"{c.first_name} {c.last_name}": c for c in all_contacts}

    hotshot = await service.create_contact_list("Hotshot", description="Curated top prospects")
    await service.bulk_add_to_list(
        hotshot.list_id,
        [by_name["Alice Angel"].crm_contact_id, by_name["Bob Angel"].crm_contact_id, by_name["John Smith"].crm_contact_id],
    )

    # Two lists sharing the exact same name -- list names are NOT unique,
    # this is the deliberate ambiguity scenario.
    await service.create_contact_list("Austin Investors", description="2025 cohort")
    await service.create_contact_list("Austin Investors", description="2026 cohort")

    return service


@pytest.fixture
def tools(crm_service):
    return AstroCrmTools(crm_service)


@pytest.fixture
def export_tools(crm_service):
    return AstroCrmTools(crm_service, export_store=AstroExportStore(), activity_log_service=crm_service.activity_log)


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


# --- Phase 3: CRM Lists -----------------------------------------------------


async def test_list_crm_lists_returns_all_with_counts(tools):
    result = await tools.dispatch("list_crm_lists", {})
    names = {l["name"] for l in result["lists"]}
    assert "Hotshot" in names
    hotshot = next(l for l in result["lists"] if l["name"] == "Hotshot")
    assert hotshot["contact_count"] == 3


async def test_get_crm_list_found(tools):
    result = await tools.dispatch("get_crm_list", {"name": "Hotshot"})
    assert result["status"] == "found"
    assert result["list"]["contact_count"] == 3
    assert result["list"]["description"] == "Curated top prospects"


async def test_get_crm_list_not_found(tools):
    result = await tools.dispatch("get_crm_list", {"name": "Nonexistent List"})
    assert result == {"status": "not_found"}


async def test_get_crm_list_ambiguous_never_arbitrarily_picks_one(tools):
    """Two lists both named 'Austin Investors' -- list names are confirmed
    not unique; must never silently pick one."""
    result = await tools.dispatch("get_crm_list", {"name": "Austin Investors"})
    assert result["status"] == "ambiguous"
    assert result["total"] == 2
    descriptions = {c["description"] for c in result["candidates"]}
    assert descriptions == {"2025 cohort", "2026 cohort"}


async def test_get_crm_list_requires_a_name(tools):
    result = await tools.dispatch("get_crm_list", {})
    assert result["error"] == "invalid_filter"


async def test_get_crm_list_members_without_filter_returns_all(tools):
    result = await tools.dispatch("get_crm_list_members", {"list_name": "Hotshot"})
    assert result["status"] == "found"
    assert result["total"] == 3
    names = {c["name"] for c in result["contacts"]}
    assert names == {"Alice Angel", "Bob Angel", "John Smith"}


async def test_get_crm_list_members_unknown_list_is_not_found(tools):
    result = await tools.dispatch("get_crm_list_members", {"list_name": "Nonexistent List"})
    assert result == {"status": "not_found"}


async def test_get_crm_list_members_result_cannot_exceed_hard_limit(tools, crm_service):
    big_list = await crm_service.create_contact_list("Big List")
    all_contacts = await crm_service.contact_store.list()
    for i in range(SEARCH_RESULT_LIMIT + 5):
        c = make_contact(first_name=f"Bulk{i}", last_name="Contact")
        await crm_service.contact_store.create(c)
    all_contacts = await crm_service.contact_store.list()
    bulk_ids = [c.crm_contact_id for c in all_contacts if c.first_name and c.first_name.startswith("Bulk")]
    await crm_service.bulk_add_to_list(big_list.list_id, bulk_ids)

    result = await tools.dispatch("get_crm_list_members", {"list_name": "Big List", "limit": 999})

    assert result["total"] > SEARCH_RESULT_LIMIT
    assert result["returned"] == SEARCH_RESULT_LIMIT
    assert len(result["contacts"]) == SEARCH_RESULT_LIMIT


# --- Phase 3: the list + CRM-filter composite (the "Hotshot" example) ------


async def test_count_angel_investors_in_hotshot_list_uses_the_composite(tools):
    """The exact example from the approved architecture: 'how many angel
    investors are in the Hotshot list' resolves via count_crm_list_members
    with the SAME custom:investor_type filter count_crm_contacts uses --
    of Hotshot's 3 members (Alice Angel, Bob Angel, John Smith), only the
    2 Angels match."""
    result = await tools.dispatch(
        "count_crm_list_members",
        {
            "list_name": "Hotshot",
            "filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]}],
        },
    )
    assert result == {"status": "found", "list": {"list_id": result["list"]["list_id"], "name": "Hotshot"}, "total": 2}


async def test_count_crm_list_members_without_filter_reuses_precomputed_count(tools):
    result = await tools.dispatch("count_crm_list_members", {"list_name": "Hotshot"})
    assert result["total"] == 3


async def test_get_crm_list_members_with_filter_returns_only_matching_contacts(tools):
    result = await tools.dispatch(
        "get_crm_list_members",
        {
            "list_name": "Hotshot",
            "filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]}],
        },
    )
    assert result["total"] == 2
    names = {c["name"] for c in result["contacts"]}
    assert names == {"Alice Angel", "Bob Angel"}


async def test_count_crm_list_members_unknown_list_is_not_found(tools):
    result = await tools.dispatch("count_crm_list_members", {"list_name": "Nonexistent List", "filters": []})
    assert result == {"status": "not_found"}


async def test_count_crm_list_members_ambiguous_list_name(tools):
    result = await tools.dispatch("count_crm_list_members", {"list_name": "Austin Investors"})
    assert result["status"] == "ambiguous"


async def test_list_member_filter_rejects_unknown_field(tools):
    result = await tools.dispatch(
        "count_crm_list_members",
        {"list_name": "Hotshot", "filters": [{"field": "ssn", "operator": "eq", "value": "x"}]},
    )
    assert result["error"] == "invalid_filter"


async def test_list_member_filter_rejects_unknown_operator(tools):
    result = await tools.dispatch(
        "get_crm_list_members",
        {"list_name": "Hotshot", "filters": [{"field": "city", "operator": "sql_injection", "value": "x"}]},
    )
    assert result["error"] == "invalid_filter"


async def test_get_crm_list_members_requires_list_name(tools):
    result = await tools.dispatch("get_crm_list_members", {})
    assert result["error"] == "invalid_filter"


def test_list_tools_present_in_registry_no_write_tools():
    from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS

    names = {t["name"] for t in CRM_TOOL_DEFINITIONS}
    assert names == {
        "count_crm_contacts",
        "search_crm_contacts",
        "get_crm_contact",
        "list_crm_lists",
        "get_crm_list",
        "get_crm_list_members",
        "count_crm_list_members",
        "export_crm_contacts",
    }
    for forbidden in ["create", "update", "delete", "bulk_add", "bulk_remove", "remove"]:
        assert not any(forbidden in name.lower() for name in names)


# --- export_crm_contacts -----------------------------------------------------


class _ScaledCrmService:
    """Fakes a CRM whose match count is directly controllable, without
    actually storing that many contacts -- used only for the export
    ceiling's boundary tests (page_size=1 probe -> total; page_size=total
    -> `total` real-shaped CrmContact objects, only ever constructed for
    the ok-at-exactly-10000 case, never the over-the-limit case, which is
    rejected before any full fetch happens)."""

    def __init__(self, total: int):
        self.total = total

    async def query_contacts(self, query):
        from app.models.crm import CrmContactPage

        if query.page_size == 1:
            return CrmContactPage(items=[], total=self.total, page=1, page_size=1)
        contacts = [make_contact(first_name=f"Bulk{i}") for i in range(self.total)]
        return CrmContactPage(items=contacts, total=self.total, page=1, page_size=self.total)

    async def list_custom_fields(self, include_inactive=True):
        return []


async def test_export_all_contacts_no_filters_uses_generic_filename(export_tools):
    result = await export_tools.dispatch("export_crm_contacts", {"filters": []})
    assert result["status"] == "ready"
    assert result["contact_count"] == 4
    assert result["filename"] == "all-crm-contacts.csv"
    assert "export_id" in result


async def test_export_filtered_matches_the_conversational_example(export_tools):
    """The exact 'Angel + Austin' example from the approved spec: only
    Alice matches both criteria."""
    result = await export_tools.dispatch(
        "export_crm_contacts",
        {
            "filters": [
                {"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]},
                {"field": "city", "operator": "eq", "value": "Austin"},
            ],
            "logic": "AND",
            "label": "Austin Angel Investors",
        },
    )
    assert result == {
        "status": "ready",
        "export_id": result["export_id"],
        "filename": "austin-angel-investors.csv",
        "contact_count": 1,
    }


async def test_export_is_not_limited_to_the_20_contact_search_display_cap(export_tools, crm_service):
    """search_crm_contacts caps at SEARCH_RESULT_LIMIT (20) -- the export
    tool must not inherit that cap. 25 Austin contacts match; the export
    must contain all 25, never 20."""
    for i in range(25):
        await crm_service.contact_store.create(make_contact(first_name=f"Extra{i}", last_name="Austinite", city="Austin"))

    result = await export_tools.dispatch(
        "export_crm_contacts", {"filters": [{"field": "city", "operator": "eq", "value": "Austin"}]}
    )
    assert result["status"] == "ready"
    assert result["contact_count"] == 28  # 25 new + Alice, Carol, John, all already in Austin
    assert result["contact_count"] > SEARCH_RESULT_LIMIT

    export = export_tools.export_store.get(result["export_id"])
    csv_text = export.csv_bytes.decode("utf-8")
    # header row + one row per contact, no truncation.
    assert len(csv_text.strip("\r\n").split("\r\n")) == 28 + 1


async def test_export_csv_contains_full_matching_set_with_correct_columns(export_tools):
    result = await export_tools.dispatch(
        "export_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Family Office"]}]},
    )
    export = export_tools.export_store.get(result["export_id"])
    csv_text = export.csv_bytes.decode("utf-8")
    rows = csv_text.split("\r\n")
    header = rows[0].split(",")
    assert "First Name" in header
    assert "Investor Type" in header  # active custom field, same as the CRM UI's own export
    assert "Source Snapshot" not in header  # internal-only, excluded exactly like get_contact_export_fields()
    assert len(rows) == 2  # header + Carol only
    assert "Carol" in rows[1]


async def test_export_zero_matches_returns_no_matches_not_an_empty_file(export_tools):
    result = await export_tools.dispatch(
        "export_crm_contacts", {"filters": [{"field": "city", "operator": "eq", "value": "Nowhere"}]}
    )
    assert result == {"status": "no_matches"}


async def test_export_at_exactly_the_ceiling_succeeds():
    scaled_tools = AstroCrmTools(
        _ScaledCrmService(EXPORT_MAX_CONTACTS), export_store=AstroExportStore(), activity_log_service=None
    )
    result = await scaled_tools.dispatch("export_crm_contacts", {"filters": []})
    assert result["status"] == "ready"
    assert result["contact_count"] == EXPORT_MAX_CONTACTS


async def test_export_one_over_the_ceiling_is_rejected_never_partial():
    scaled_tools = AstroCrmTools(
        _ScaledCrmService(EXPORT_MAX_CONTACTS + 1), export_store=AstroExportStore(), activity_log_service=None
    )
    result = await scaled_tools.dispatch("export_crm_contacts", {"filters": []})
    assert result["error"] == "too_large"
    assert result["total"] == EXPORT_MAX_CONTACTS + 1
    assert result["limit"] == EXPORT_MAX_CONTACTS
    # Never a partial export -- nothing was stored.
    assert scaled_tools.export_store._pending == {}


async def test_export_records_exactly_one_activity_log_event_with_safe_metadata_only(export_tools, crm_service):
    result = await export_tools.dispatch(
        "export_crm_contacts",
        {
            "filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]}],
            "label": "Angel Investors",
        },
    )
    assert result["status"] == "ready"

    page = await crm_service.activity_log.list_events(category=ActivityCategory.EXPORTS)
    assert page.total == 1
    event = page.items[0]
    assert event.event_type == "contacts.exported"
    assert event.source == ActivitySource.ASTRO_AI
    assert event.metadata["contact_count"] == 2
    assert event.metadata["format"] == "csv"
    assert event.metadata["segment"] == "Angel Investors"
    # Never actual contact rows/PII in the audit event.
    assert "contacts" not in event.metadata
    assert "Alice" not in str(event.metadata)


async def test_export_without_activity_log_configured_still_succeeds():
    """activity_log_service is optional -- a caller/test that only cares
    about the export itself doesn't have to wire one up."""
    tools_no_audit = AstroCrmTools(CrmService(), export_store=AstroExportStore(), activity_log_service=None)
    await tools_no_audit.crm_service.contact_store.create(make_contact(first_name="Solo", last_name="Contact"))
    result = await tools_no_audit.dispatch("export_crm_contacts", {"filters": []})
    assert result["status"] == "ready"


async def test_export_without_export_store_configured_fails_cleanly(tools):
    """`tools` (the plain fixture) has no export_store -- matches every
    other existing test in this file that constructs AstroCrmTools with
    just a CrmService."""
    result = await tools.dispatch("export_crm_contacts", {"filters": []})
    assert result == {"error": "tool_failed", "message": "Export isn't available right now -- please try again."}


async def test_concurrent_exports_never_cross_contaminate(export_tools, crm_service):
    """Two different exports in the same store must never return each
    other's data via their export_id."""
    angel_result = await export_tools.dispatch(
        "export_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Angel Investor"]}]},
    )
    family_result = await export_tools.dispatch(
        "export_crm_contacts",
        {"filters": [{"field": "custom:investor_type", "operator": "contains_any", "value": ["Family Office"]}]},
    )
    assert angel_result["export_id"] != family_result["export_id"]

    angel_export = export_tools.export_store.get(angel_result["export_id"])
    family_export = export_tools.export_store.get(family_result["export_id"])
    assert b"Carol" not in angel_export.csv_bytes
    assert b"Alice" not in family_export.csv_bytes
    assert b"Bob" not in family_export.csv_bytes
