"""
Storage abstraction for ActivityEvent -- append-only, no update/delete
surface at all (see ActivityLogService's docstring for why: no manual
editing of logs is in scope, and historical events must survive deletion of
the entities they describe). Filtering (category/search/date) and
pagination happen in the SERVICE layer over `list()`'s full result, the
same convention CrmService.list_contacts()/query_contacts() already use
over CrmContactStore.list() -- fine at this app's scale, and keeps this
store's own interface as small as CrmContactListStore's.
"""

from abc import ABC, abstractmethod

from app.models.activity import ActivityEvent


class ActivityEventStore(ABC):
    @abstractmethod
    async def create(self, event: ActivityEvent) -> None:
        """Persist a newly-recorded event. Raises ValueError if event_id already exists."""

    @abstractmethod
    async def list(self) -> list[ActivityEvent]:
        """Every stored event, newest first (created_at DESC)."""


class MemoryActivityEventStore(ActivityEventStore):
    """Dict-backed, keyed by event_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._events: dict[str, ActivityEvent] = {}

    async def create(self, event: ActivityEvent) -> None:
        if event.event_id in self._events:
            raise ValueError(f"ActivityEvent already exists: {event.event_id}")
        self._events[event.event_id] = event

    async def list(self) -> list[ActivityEvent]:
        return sorted(self._events.values(), key=lambda e: e.created_at, reverse=True)
