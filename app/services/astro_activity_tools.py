"""
Astro AI Phase 3 -- read-only Activity Log tool.

A direct, thin wrap of ActivityLogService.list_events() -- no new
filtering/search logic is invented here; the category/query/date-range
filtering already lives in that one existing method. This file never
writes an event -- the record-a-new-event method on that same service is
never called here; see the static import-graph test proving no
write-capable service/store is reachable.

`actor` is always None on every ActivityEvent today (no authenticated-user
system exists in this app) -- the tool description and system-prompt
guidance must make Astro understand this explicitly, so it never invents
a "who" for an event.

Known, pre-existing technical debt (NOT introduced by this file, not
addressed in this phase per explicit scope): ActivityLogService.list_events()
loads the ENTIRE activity_events table on every call before filtering in
Python (see app/services/activity_log_service.py) -- acceptable at today's
volume (dozens-hundreds of rows), a real latency concern only at much
larger scale. No caching or store-level filtering is introduced here.
"""

from datetime import datetime, timezone

from loguru import logger

from app.models.activity import ActivityCategory, ActivityEvent
from app.services.activity_log_service import ActivityLogService

ACTIVITY_SEARCH_LIMIT = 20

_CATEGORY_VALUES = [c.value for c in ActivityCategory]

ASTRO_ACTIVITY_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_activity",
        "description": (
            "Search the Hub's Activity Log -- a record of meaningful CRM/contact/list/import/"
            "campaign/email-intake actions (never ordinary reads/views). Returns at most 20 "
            "events plus the true total match count. Omit all filters to get the most recent "
            "activity. IMPORTANT: every event's 'actor' is always unavailable (no user-identity "
            "system exists yet) -- never state or guess who performed an action, only what "
            "happened and when."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": _CATEGORY_VALUES,
                    "description": "Restrict to one category, e.g. 'contacts', 'lists', 'campaigns', 'mail', 'imports'.",
                },
                "q": {"type": "string", "description": "Case-insensitive text to match against the event summary/entity name."},
                "date_from": {"type": "string", "description": "ISO 8601 date/datetime lower bound, e.g. '2026-08-20'."},
                "date_to": {"type": "string", "description": "ISO 8601 date/datetime upper bound."},
                "limit": {"type": "integer", "description": "Max events to return. Capped at 20 regardless of what you request."},
            },
            "required": [],
        },
    },
]


def _project_event(event: ActivityEvent) -> dict:
    return {
        "event_type": event.event_type,
        "category": event.category.value,
        "created_at": event.created_at.isoformat(),
        "entity_type": event.entity_type,
        "entity_name": event.entity_name,
        "summary": event.summary,
        # `actor` deliberately omitted -- it is always None today; see
        # module docstring. Not worth sending a field that's never useful.
    }


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    # ActivityEvent.created_at is always UTC-aware; a bare date/datetime
    # string from Claude (e.g. "2026-08-20") parses as naive, which would
    # raise on comparison against an aware datetime -- assume UTC rather
    # than reject an otherwise-reasonable date string.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AstroActivityTools:
    """Read-only Activity Log tool surface. Only ever calls
    ActivityLogService.list_events() -- record() (the write path) is never
    called from this file."""

    def __init__(self, activity_log_service: ActivityLogService):
        self.activity_log_service = activity_log_service

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        try:
            return await handler(self, tool_input or {})
        except ValueError as e:
            # Includes: bad ActivityCategory value, unparseable date string.
            return {"error": "invalid_filter", "message": f"Malformed tool input: {e}"}
        except Exception as e:  # noqa: BLE001 -- must never crash the chat turn
            logger.error(f"Astro activity tool '{name}' failed: {type(e).__name__}")
            return {"error": "tool_failed", "message": "The activity search failed -- please try again."}

    async def _search_activity(self, tool_input: dict) -> dict:
        category_raw = tool_input.get("category")
        category = ActivityCategory(category_raw) if category_raw else None
        q = tool_input.get("q") or None
        date_from = _parse_datetime(tool_input.get("date_from"))
        date_to = _parse_datetime(tool_input.get("date_to"))
        requested_limit = int(tool_input.get("limit") or ACTIVITY_SEARCH_LIMIT)
        limit = max(1, min(requested_limit, ACTIVITY_SEARCH_LIMIT))

        page = await self.activity_log_service.list_events(
            category=category, q=q, date_from=date_from, date_to=date_to, page=1, page_size=limit
        )
        return {
            "total": page.total,
            "returned": len(page.items),
            "events": [_project_event(e) for e in page.items],
        }


_HANDLERS = {
    "search_activity": AstroActivityTools._search_activity,
}
