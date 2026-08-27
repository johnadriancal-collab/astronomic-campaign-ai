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

Timezone note (fixed after Phase 3 production verification found Astro
answering "what happened today" by fetching the 20 most-recent events with
no date filter and reasoning over their timestamps itself -- unreliable
once a day has more than 20 events): this app has no declared backend
"business timezone" anywhere (confirmed by inspection -- the only
timezone value in the whole codebase is the frontend's
DEFAULT_TIMEZONE="America/Chicago", scoped solely to per-campaign
mail-sending-schedule defaults, never anything to do with Activity Log or
"today"). America/Chicago was explicitly chosen for THIS purpose by the
user. `ActivityEvent.created_at` remains stored/compared in UTC exactly as
before; a date/datetime given here WITHOUT its own timezone is interpreted
as America/Chicago wall-clock time, then converted to UTC for comparison
-- never silently assumed to already be UTC.

Known, pre-existing technical debt (NOT introduced by this file, not
addressed in this phase per explicit scope): ActivityLogService.list_events()
loads the ENTIRE activity_events table on every call before filtering in
Python (see app/services/activity_log_service.py) -- acceptable at today's
volume (dozens-hundreds of rows), a real latency concern only at much
larger scale. No caching or store-level filtering is introduced here.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from app.models.activity import ActivityCategory, ActivityEvent
from app.services.activity_log_service import ActivityLogService

ACTIVITY_SEARCH_LIMIT = 20

# The Hub's chosen convention for interpreting a date/datetime that arrives
# with no timezone of its own -- see the module docstring above for why
# this (and not UTC, and not the caller's local time) was chosen.
BUSINESS_TIMEZONE = ZoneInfo("America/Chicago")

_CATEGORY_VALUES = [c.value for c in ActivityCategory]

ASTRO_ACTIVITY_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_activity",
        "description": (
            "Search the Hub's Activity Log -- a record of meaningful CRM/contact/list/import/"
            "campaign/email-intake actions (never ordinary reads/views). Returns at most 20 "
            "events plus the true total match count. IMPORTANT: whenever the question specifies "
            "or implies ANY date or time period -- explicit ('August 20', 'between August 20 and "
            "25') or relative ('today', 'yesterday', 'this week', 'last week') -- you MUST "
            "translate it into date_from/date_to and pass them here. Never call this with no "
            "date filter and then manually reason over the returned timestamps yourself: with no "
            "date filter this returns only the 20 most-recent events overall, which can silently "
            "miss earlier activity from the very period you were asked about. Only omit "
            "date_from/date_to when the question has no date/time scope at all (e.g. 'show me "
            "recent activity'). Also IMPORTANT: every event's 'actor' is always unavailable (no "
            "user-identity system exists yet) -- never state or guess who performed an action, "
            "only what happened and when."
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
                "date_from": {
                    "type": "string",
                    "description": (
                        "ISO 8601 date or datetime marking the START of the period, e.g. "
                        "'2026-08-20' or '2026-08-20T09:00:00-05:00'. A bare date with no time/"
                        "timezone (e.g. '2026-08-20') is interpreted as the start of that day in "
                        "America/Chicago (Central Time) -- the Hub's convention for dates with no "
                        "timezone of their own, given in your system instructions along with the "
                        "current date/time. Provide this whenever the question specifies or "
                        "implies a date/time period."
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": (
                        "ISO 8601 date or datetime marking the END of the period, inclusive, e.g. "
                        "'2026-08-25'. A bare date with no time/timezone is interpreted as the END "
                        "of that day (23:59:59) in America/Chicago (Central Time), so a single-day "
                        "question can use the SAME bare date for both date_from and date_to and "
                        "correctly cover the whole day."
                    ),
                },
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


def _parse_datetime(value: str | None, *, end_of_day: bool) -> datetime | None:
    """`end_of_day` distinguishes date_from (bare date -> start of that
    day) from date_to (bare date -> end of that day) so a single-day
    question can pass the same bare date for both and correctly cover the
    whole day -- see the date_to schema description above for why this
    matters (without it, a bare date_to would resolve to that day's
    midnight and exclude almost the entire day it names)."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # No timezone given -- interpret as America/Chicago (BUSINESS_TIMEZONE,
        # see module docstring for why), never silently assume UTC.
        is_bare_date = len(value.strip()) == 10  # exactly "YYYY-MM-DD", no time component
        if is_bare_date and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        parsed = parsed.replace(tzinfo=BUSINESS_TIMEZONE)
    # ActivityEvent.created_at is always UTC-aware; convert whatever
    # timezone this value ended up in (BUSINESS_TIMEZONE above, or
    # whatever explicit offset/Z the caller supplied) to UTC so the
    # comparison in ActivityLogService.list_events() is apples-to-apples.
    return parsed.astimezone(timezone.utc)


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
        date_from = _parse_datetime(tool_input.get("date_from"), end_of_day=False)
        date_to = _parse_datetime(tool_input.get("date_to"), end_of_day=True)
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
