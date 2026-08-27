"""
AstroActivityTools tests -- Astro AI Phase 3 read-only Activity Log
surface. Exercised against a REAL ActivityLogService (in-memory store).
"""

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.activity import ActivityCategory, ActivityEvent, ActivitySource
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_activity_tools import (
    ACTIVITY_SEARCH_LIMIT,
    ASTRO_ACTIVITY_TOOL_DEFINITIONS,
    BUSINESS_TIMEZONE,
    AstroActivityTools,
)

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


# --- date-scoping fix (Phase 3 production verification finding) -----------
#
# Root cause recap: "what happened today" was calling search_activity with
# no date filter at all, fetching the 20 most-recent events, and having
# Claude reason over their timestamps itself -- unreliable once a day has
# more than 20 events. These tests prove the ACTUAL server-side date
# filtering (not Claude's own reasoning) is correct at the boundaries.


def _event_at(created_at: datetime, summary: str) -> ActivityEvent:
    return ActivityEvent(
        event_id=str(uuid.uuid4()),
        event_type="contact.created",
        category=ActivityCategory.CONTACTS,
        created_at=created_at,
        source=ActivitySource.MANUAL_CRM,
        summary=summary,
    )


@pytest.fixture
def business_noon():
    """Noon on August 20, 2026, America/Chicago -- a fixed anchor so
    boundary tests are deterministic regardless of when they actually run."""
    return datetime(2026, 8, 20, 12, 0, 0, tzinfo=BUSINESS_TIMEZONE)


async def test_date_from_only_excludes_earlier_events(tools, activity_log_service, business_noon):
    await activity_log_service.store.create(_event_at(business_noon.astimezone(timezone.utc) - timedelta(days=1), "before"))
    await activity_log_service.store.create(_event_at(business_noon.astimezone(timezone.utc) + timedelta(hours=1), "after"))

    result = await tools.dispatch("search_activity", {"date_from": business_noon.isoformat()})

    assert result["total"] == 1
    assert result["events"][0]["summary"] == "after"


async def test_date_to_only_excludes_later_events(tools, activity_log_service, business_noon):
    await activity_log_service.store.create(_event_at(business_noon.astimezone(timezone.utc) - timedelta(hours=1), "before"))
    await activity_log_service.store.create(_event_at(business_noon.astimezone(timezone.utc) + timedelta(days=1), "after"))

    result = await tools.dispatch("search_activity", {"date_to": business_noon.isoformat()})

    assert result["total"] == 1
    assert result["events"][0]["summary"] == "before"


async def test_date_from_and_date_to_together_bound_both_sides(tools, activity_log_service, business_noon):
    utc_noon = business_noon.astimezone(timezone.utc)
    await activity_log_service.store.create(_event_at(utc_noon - timedelta(days=2), "too early"))
    await activity_log_service.store.create(_event_at(utc_noon, "in range"))
    await activity_log_service.store.create(_event_at(utc_noon + timedelta(days=2), "too late"))

    result = await tools.dispatch(
        "search_activity",
        {
            "date_from": (business_noon - timedelta(days=1)).isoformat(),
            "date_to": (business_noon + timedelta(days=1)).isoformat(),
        },
    )

    assert result["total"] == 1
    assert result["events"][0]["summary"] == "in range"


async def test_explicit_timezone_offset_is_respected_not_reinterpreted(tools, activity_log_service):
    """An input that already carries its own UTC offset must be honored
    as-is -- never silently reinterpreted as America/Chicago."""
    # 2026-08-20T00:00:00-05:00 == 2026-08-20T05:00:00Z
    event_just_after = datetime(2026, 8, 20, 5, 0, 1, tzinfo=timezone.utc)
    event_just_before = datetime(2026, 8, 20, 4, 59, 59, tzinfo=timezone.utc)
    await activity_log_service.store.create(_event_at(event_just_after, "after"))
    await activity_log_service.store.create(_event_at(event_just_before, "before"))

    result = await tools.dispatch("search_activity", {"date_from": "2026-08-20T00:00:00-05:00"})

    assert result["total"] == 1
    assert result["events"][0]["summary"] == "after"


async def test_bare_date_covers_the_whole_business_day_in_america_chicago(tools, activity_log_service):
    """The exact 'what happened on August 20' / 'today' scenario: a bare
    date used for BOTH date_from and date_to must capture the entire
    America/Chicago day, not just its literal midnight instant."""
    start_of_day_chicago = datetime(2026, 8, 20, 0, 0, 0, tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)
    end_of_day_chicago = datetime(2026, 8, 20, 23, 59, 59, tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)
    mid_afternoon_chicago = datetime(2026, 8, 20, 15, 0, 0, tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)

    await activity_log_service.store.create(_event_at(start_of_day_chicago, "start of day"))
    await activity_log_service.store.create(_event_at(mid_afternoon_chicago, "mid afternoon"))
    await activity_log_service.store.create(_event_at(end_of_day_chicago, "end of day"))
    await activity_log_service.store.create(_event_at(start_of_day_chicago - timedelta(seconds=1), "just before midnight"))
    await activity_log_service.store.create(_event_at(end_of_day_chicago + timedelta(minutes=2), "just after end of day"))

    result = await tools.dispatch("search_activity", {"date_from": "2026-08-20", "date_to": "2026-08-20"})

    assert result["total"] == 3
    summaries = {e["summary"] for e in result["events"]}
    assert summaries == {"start of day", "mid afternoon", "end of day"}


async def test_boundary_events_immediately_before_inside_after_period(tools, activity_log_service, business_noon):
    """One second before, exactly at, and one second after the requested
    window -- proves inclusive boundaries are exactly where expected."""
    utc_noon = business_noon.astimezone(timezone.utc)
    await activity_log_service.store.create(_event_at(utc_noon - timedelta(seconds=1), "just before"))
    await activity_log_service.store.create(_event_at(utc_noon, "exactly at start"))
    await activity_log_service.store.create(_event_at(utc_noon + timedelta(hours=12) - timedelta(seconds=1), "exactly at end"))
    await activity_log_service.store.create(_event_at(utc_noon + timedelta(hours=12), "just after"))

    result = await tools.dispatch(
        "search_activity",
        {
            "date_from": business_noon.isoformat(),
            "date_to": (business_noon + timedelta(hours=12) - timedelta(seconds=1)).isoformat(),
        },
    )

    summaries = {e["summary"] for e in result["events"]}
    assert summaries == {"exactly at start", "exactly at end"}
    assert result["total"] == 2


async def test_date_filtered_search_still_respects_the_twenty_result_ceiling(tools, activity_log_service, business_noon):
    utc_noon = business_noon.astimezone(timezone.utc)
    for i in range(ACTIVITY_SEARCH_LIMIT + 5):
        await activity_log_service.store.create(_event_at(utc_noon + timedelta(minutes=i), f"event {i}"))

    result = await tools.dispatch(
        "search_activity",
        {"date_from": business_noon.isoformat(), "date_to": (business_noon + timedelta(days=1)).isoformat()},
    )

    assert result["total"] > ACTIVITY_SEARCH_LIMIT
    assert result["returned"] == ACTIVITY_SEARCH_LIMIT
    assert len(result["events"]) == ACTIVITY_SEARCH_LIMIT


def test_date_from_date_to_and_business_timezone_documented_in_tool_schema():
    """The schema description is what teaches Claude the America/Chicago
    convention and the start-of-day/end-of-day bare-date behavior."""
    tool = next(t for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS if t["name"] == "search_activity")
    date_from_desc = tool["input_schema"]["properties"]["date_from"]["description"]
    date_to_desc = tool["input_schema"]["properties"]["date_to"]["description"]
    assert "America/Chicago" in date_from_desc
    assert "America/Chicago" in date_to_desc
    assert "end" in date_to_desc.lower()


def test_tool_description_mandates_date_translation_for_relative_questions():
    tool = next(t for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS if t["name"] == "search_activity")
    text = tool["description"].lower()
    assert "must" in text
    assert "today" in text and "yesterday" in text
    assert "20 most-recent" in text or "20 most recent" in text
