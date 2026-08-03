"""
Storage abstraction for EmailMessageEvent -- individual open/click (etc.)
events belonging to one EmailMessage. Discrete-column convention (like
EmailSequenceStep), since this is a narrow, stable-shape, append-only
event log, not an evolving single entity.
"""

from abc import ABC, abstractmethod

from app.models.email_message import EmailMessageEvent


class EmailMessageEventStore(ABC):
    @abstractmethod
    async def create(self, event: EmailMessageEvent) -> None:
        """Persist a newly-created event. Raises if email_message_event_id already exists."""

    @abstractmethod
    async def get_by_apollo_event_id(self, apollo_event_id: str) -> EmailMessageEvent | None:
        """The sync dedup lookup -- one EmailMessageEvent per Apollo event id, ever."""

    @abstractmethod
    async def list_for_message(self, email_message_id: str) -> list[EmailMessageEvent]:
        """Every event for this message, oldest first."""


class MemoryEmailMessageEventStore(EmailMessageEventStore):
    """Dict-backed, keyed by email_message_event_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._events: dict[str, EmailMessageEvent] = {}

    async def create(self, event: EmailMessageEvent) -> None:
        if event.email_message_event_id in self._events:
            raise ValueError(f"EmailMessageEvent already exists: {event.email_message_event_id}")
        self._events[event.email_message_event_id] = event

    async def get_by_apollo_event_id(self, apollo_event_id: str) -> EmailMessageEvent | None:
        for event in self._events.values():
            if event.apollo_event_id == apollo_event_id:
                return event
        return None

    async def list_for_message(self, email_message_id: str) -> list[EmailMessageEvent]:
        events = [e for e in self._events.values() if e.email_message_id == email_message_id]
        return sorted(events, key=lambda e: e.occurred_at)
