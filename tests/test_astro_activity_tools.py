"""
AstroActivityTools tests -- Astro AI Phase 3 read-only Activity Log
surface. Exercised against a REAL ActivityLogService (in-memory store).
"""

import ast
from pathlib import Path

import pytest

from app.models.activity import ActivityCategory, ActivitySource
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_activity_tools import ACTIVITY_SEARCH_LIMIT, ASTRO_ACTIVITY_TOOL_DEFINITIONS, AstroActivityTools

pytestmark = pytest.mark.asyncio


@pytest.fixture
def activity_log_service():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def tools(activity_log_service):
    return AstroActivityTools(activity_log_service)


async def _seed(service, n=1, category=ActivityCategory.CONTACTS, source=ActivitySource.MANUAL_CRM, summary="Something happened."):
    for i in range(n):
        await service.record(
            event_type="contact.created",
            category=category,
            source=source,
            summary=f"{summary} #{i}",
            entity_type="contact",
            entity_name=f"Person {i}",
        )


async def test_search_with_no_filters_returns_most_recent(tools, activity_log_service):
    await _seed(activity_log_service, n=3)

    result = await tools.dispatch("search_activity", {})

    assert result["total"] == 3
    assert result["returned"] == 3
    assert len(result["events"]) == 3


async def test_search_filters_by_category(tools, activity_log_service):
    await _seed(activity_log_service, n=2, category=ActivityCategory.CONTACTS)
    await _seed(activity_log_service, n=1, category=ActivityCategory.LISTS, source=ActivitySource.LISTS, summary="List thing.")

    result = await tools.dispatch("search_activity", {"category": "lists"})

    assert result["total"] == 1
    assert result["events"][0]["category"] == "lists"


async def test_search_result_cannot_exceed_hard_limit(tools, activity_log_service):
    await _seed(activity_log_service, n=ACTIVITY_SEARCH_LIMIT + 5)

    result = await tools.dispatch("search_activity", {"limit": 999})

    assert result["total"] > ACTIVITY_SEARCH_LIMIT
    assert result["returned"] == ACTIVITY_SEARCH_LIMIT
    assert len(result["events"]) == ACTIVITY_SEARCH_LIMIT


async def test_search_by_text_query(tools, activity_log_service):
    await activity_log_service.record(
        event_type="contact.created", category=ActivityCategory.CONTACTS, source=ActivitySource.MANUAL_CRM,
        summary="John Smith was manually created in the CRM.", entity_type="contact", entity_name="John Smith",
    )
    await activity_log_service.record(
        event_type="contact.created", category=ActivityCategory.CONTACTS, source=ActivitySource.MANUAL_CRM,
        summary="Jane Doe was manually created in the CRM.", entity_type="contact", entity_name="Jane Doe",
    )

    result = await tools.dispatch("search_activity", {"q": "John Smith"})

    assert result["total"] == 1
    assert "John Smith" in result["events"][0]["summary"]


async def test_search_by_date_range(tools, activity_log_service):
    await _seed(activity_log_service, n=1)
    result_future = await tools.dispatch("search_activity", {"date_from": "2099-01-01"})
    result_all = await tools.dispatch("search_activity", {})

    assert result_future["total"] == 0
    assert result_all["total"] == 1


async def test_empty_results_are_explicit(tools):
    result = await tools.dispatch("search_activity", {"q": "nothing matches this"})
    assert result == {"total": 0, "returned": 0, "events": []}


async def test_invalid_category_is_rejected_not_silently_ignored(tools):
    result = await tools.dispatch("search_activity", {"category": "not_a_real_category"})
    assert result["error"] == "invalid_filter"


async def test_malformed_date_is_rejected(tools):
    result = await tools.dispatch("search_activity", {"date_from": "not-a-date"})
    assert result["error"] == "invalid_filter"


async def test_projected_event_never_includes_actor(tools, activity_log_service):
    """actor is always None today -- the tool must not even surface the
    field, so Claude can't accidentally treat an absent/null actor as
    meaningful signal."""
    await _seed(activity_log_service, n=1)
    result = await tools.dispatch("search_activity", {})
    assert "actor" not in result["events"][0]


async def test_projected_event_never_includes_raw_metadata(tools, activity_log_service):
    await activity_log_service.record(
        event_type="import.completed", category=ActivityCategory.IMPORTS, source=ActivitySource.CSV_IMPORT,
        summary="Import completed.", entity_type="import_batch", metadata={"internal_detail": "should not leak"},
    )
    result = await tools.dispatch("search_activity", {})
    assert "metadata" not in result["events"][0]
    assert "internal_detail" not in str(result)


async def test_unknown_tool_name_is_rejected(tools):
    result = await tools.dispatch("delete_activity_event", {})
    assert result == {"error": "unknown_tool", "message": "'delete_activity_event' is not an available tool."}


def test_no_write_tool_exists_in_the_activity_tool_registry():
    names = {t["name"] for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS}
    assert names == {"search_activity"}


def test_activity_tool_description_warns_actor_is_unavailable():
    full_text = " ".join(t["description"].lower() for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS)
    assert "actor" in full_text and "unavailable" in full_text


def test_astro_activity_tools_never_calls_record():
    """record() is the write path -- this file must only ever call
    list_events()."""
    source = Path("app/services/astro_activity_tools.py").read_text()
    assert ".record(" not in source


def test_astro_activity_tools_never_imports_write_capable_modules():
    tree = ast.parse(Path("app/services/astro_activity_tools.py").read_text())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden = {
        "app.repositories.mailbox_credential_store",
        "app.services.mailbox_service",
        "app.apollo",
        "app.services.crm_service",
        "app.services.campaign_service",
        "app.services.mail_campaign_service",
    }
    hit = forbidden & imported_modules
    assert not hit, f"astro_activity_tools.py imports forbidden module(s): {hit}"
