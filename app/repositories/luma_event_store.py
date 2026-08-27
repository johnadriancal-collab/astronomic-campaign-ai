"""
Storage abstraction for LumaEvent -- upserted opportunistically whenever a
guest webhook/backfill page includes an embedded event, keyed by Luma's own
event id. No foreign-key constraint to luma_registrations (app-level
integrity only, matching this repo's existing convention -- see
crm_contact_list_members).
"""

from abc import ABC, abstractmethod

from app.models.luma import LumaEvent


class LumaEventStore(ABC):
    @abstractmethod
    async def save(self, event: LumaEvent) -> None:
        """Upsert, keyed on luma_event_id."""

    @abstractmethod
    async def get(self, luma_event_id: str) -> LumaEvent | None: ...

    @abstractmethod
    async def list(self) -> list[LumaEvent]: ...


class MemoryLumaEventStore(LumaEventStore):
    """Dict-backed, keyed by luma_event_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._events: dict[str, LumaEvent] = {}

    async def save(self, event: LumaEvent) -> None:
        self._events[event.luma_event_id] = event

    async def get(self, luma_event_id: str) -> LumaEvent | None:
        return self._events.get(luma_event_id)

    async def list(self) -> list[LumaEvent]:
        return list(self._events.values())
