"""
Storage abstraction for LumaCalendarEventCapture -- Stage 6A Capture
(2026-09-14), temporary schema-discovery storage ONLY. See
app/models/luma.py's LumaCalendarEventCapture docstring for the full
contract: at most ONE row per `event_type`, ever, by design.

Deliberately its own tiny abstraction (not folded into LumaEventStore or
LumaRegistrationStore) -- this is intentionally isolated from every
canonical Luma/CRM model so it can be deleted cleanly, in one place, once
its one job (letting a human see one real payload per event type) is
done.
"""

from abc import ABC, abstractmethod

from app.models.luma import LumaCalendarEventCapture


class LumaCalendarEventCaptureStore(ABC):
    @abstractmethod
    async def save_if_first_for_event_type(self, capture: LumaCalendarEventCapture) -> bool:
        """Inserts `capture` ONLY if no row exists yet for
        `capture.event_type` -- returns True if it was actually stored,
        False if a row for that event_type already existed (a genuine
        no-op, never an error, regardless of whether this is a retried
        delivery_id or simply a later, different delivery of the same
        event type). Callers must not rely on the return value for
        anything beyond diagnostics/tests -- the webhook route always
        returns the same 200 either way."""

    @abstractmethod
    async def get(self, event_type: str) -> LumaCalendarEventCapture | None: ...

    @abstractmethod
    async def list(self) -> list[LumaCalendarEventCapture]: ...


class MemoryLumaCalendarEventCaptureStore(LumaCalendarEventCaptureStore):
    """Dict-backed, keyed by event_type -- not persistent, for tests/local dev."""

    def __init__(self):
        self._captures: dict[str, LumaCalendarEventCapture] = {}

    async def save_if_first_for_event_type(self, capture: LumaCalendarEventCapture) -> bool:
        if capture.event_type in self._captures:
            return False
        self._captures[capture.event_type] = capture
        return True

    async def get(self, event_type: str) -> LumaCalendarEventCapture | None:
        return self._captures.get(event_type)

    async def list(self) -> list[LumaCalendarEventCapture]:
        return list(self._captures.values())
