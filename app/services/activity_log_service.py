"""
ActivityLogService -- the one thing every other service (CrmService,
CrmImportService, ItfIngestionService, CampaignService,
CampaignSyncService) calls to record a meaningful action. Two rules govern
everything here:

1. Log only meaningful mutations/actions, never read-only activity. Nothing
   in this app's search/view/pagination/Astro-refinement code paths calls
   this service at all -- there is no filtering logic here to suppress
   reads, because reads never reach this service in the first place. Every
   real call site is listed in this module's `record()` docstring call
   sites below (grep callers of `record(` to see the exhaustive list).

2. `record()` is BEST EFFORT and must NEVER raise. A failure to write the
   log must never fail, roll back, or otherwise corrupt the primary action
   that's already succeeded by the time this is called (every call site
   invokes this AFTER its own store write, never before or wrapping it in a
   shared transaction -- there is no existing multi-store transaction
   pattern anywhere in this codebase to be consistent with, so this
   feature doesn't invent one just for its own sake). Any exception raised
   by the underlying store is caught, logged via loguru (this app's
   existing convention -- see app/api/sync.py), and swallowed. Callers
   never need their own try/except around a `record()` call.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.models.activity import ActivityCategory, ActivityEvent, ActivityEventPage, ActivitySource
from app.repositories.activity_event_store import ActivityEventStore


class ActivityLogService:
    def __init__(self, store: ActivityEventStore):
        self.store = store

    async def record(
        self,
        event_type: str,
        category: ActivityCategory,
        source: ActivitySource,
        summary: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        entity_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> ActivityEvent | None:
        """Returns the persisted event, or None if the write itself failed --
        callers should never branch on this return value for anything other
        than tests; the primary action this is called after has already
        succeeded regardless of what happens here.

        `actor` (Phase 2, 2026-09-03): WHO performed the action, distinct
        from `source` (WHERE it originated -- see ActivitySource's own
        docstring). Every existing call site omits this (stays None,
        byte-identical to before this parameter existed) -- it is
        populated only by the handful of Astronomic Mail campaign/CRM-list
        mutation routes reachable by the admin/service OPERATOR token (see
        app/session_auth_middleware.py), which pass `actor="claude_operator"`
        so those actions are visibly distinguishable from an ordinary Hub
        session's identical action in the Activity Log."""
        event = ActivityEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            category=category,
            created_at=datetime.now(timezone.utc),
            source=source,
            actor=actor,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            summary=summary,
            metadata=metadata or {},
        )
        try:
            await self.store.create(event)
            return event
        except Exception as e:
            logger.error(f"Activity log write failed for event_type={event_type}: {e}")
            return None

    async def list_events(
        self,
        category: ActivityCategory | None = None,
        q: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ActivityEventPage:
        """Category/search/date filtering plus pagination, all in this service
        layer over the store's full (already newest-first) result -- same
        convention as CrmService.list_contacts()/query_contacts() filtering
        in Python over CrmContactStore.list() rather than pushing WHERE
        clauses into the store. `q` matches against `summary` and
        `entity_name` (case-insensitive substring) -- not `metadata`, which
        stays structured data for the details view, not a search target."""
        events = await self.store.list()

        def matches(e: ActivityEvent) -> bool:
            if category and e.category != category:
                return False
            if date_from and e.created_at < date_from:
                return False
            if date_to and e.created_at > date_to:
                return False
            if q:
                haystack = f"{e.summary} {e.entity_name or ''}".lower()
                if q.lower() not in haystack:
                    return False
            return True

        filtered = [e for e in events if matches(e)]
        total = len(filtered)

        page = max(page, 1)
        page_size = max(page_size, 1)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]

        return ActivityEventPage(items=items, total=total, page=page, page_size=page_size)
