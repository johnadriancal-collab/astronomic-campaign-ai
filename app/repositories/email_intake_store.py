"""
Storage abstraction for EmailIntakeItem. Unlike ActivityEventStore this is
NOT append-only -- approve()/reject() transition an item's status in
place, so `save()` is a genuine update. `create()` enforces uniqueness on
BOTH intake_id (primary key) and gmail_message_id (the idempotency key a
retried/duplicate webhook call is checked against) -- see
EmailIntakeService.ingest()'s docstring for exactly how that's used.

Filtering (status/search) and pagination happen in the SERVICE layer over
`list()`'s full result -- same convention as ActivityEventStore/
CrmContactStore already use at this app's scale.
"""

from abc import ABC, abstractmethod

from app.models.email_intake import EmailIntakeItem


class EmailIntakeDuplicateError(Exception):
    """Raised by create() when gmail_message_id already exists -- the
    caller (EmailIntakeService.ingest()) catches this to return the
    existing item as an idempotent "already_processed" response rather
    than letting it propagate as a real error."""


class EmailIntakeStore(ABC):
    @abstractmethod
    async def create(self, item: EmailIntakeItem) -> None:
        """Persist a newly-ingested item. Raises EmailIntakeDuplicateError
        if gmail_message_id already exists; raises ValueError if intake_id
        already exists (should never happen -- intake_id is a fresh uuid4
        per call)."""

    @abstractmethod
    async def save(self, item: EmailIntakeItem) -> None:
        """Upsert, keyed on intake_id -- used for status transitions
        (manual match, approve, reject) and for the stale-review refresh
        (see EmailIntakeService.approve())."""

    @abstractmethod
    async def get(self, intake_id: str) -> EmailIntakeItem | None:
        ...

    @abstractmethod
    async def get_by_gmail_message_id(self, gmail_message_id: str) -> EmailIntakeItem | None:
        ...

    @abstractmethod
    async def list(self) -> list[EmailIntakeItem]:
        """Every stored item, newest first (created_at DESC)."""


class MemoryEmailIntakeStore(EmailIntakeStore):
    """Dict-backed, keyed by intake_id, plus a secondary gmail_message_id
    index -- not persistent, for tests/local dev."""

    def __init__(self):
        self._items: dict[str, EmailIntakeItem] = {}
        self._by_gmail_message_id: dict[str, str] = {}  # gmail_message_id -> intake_id

    async def create(self, item: EmailIntakeItem) -> None:
        if item.gmail_message_id in self._by_gmail_message_id:
            raise EmailIntakeDuplicateError(
                f"EmailIntakeItem already exists for gmail_message_id={item.gmail_message_id}"
            )
        if item.intake_id in self._items:
            raise ValueError(f"EmailIntakeItem already exists: {item.intake_id}")
        self._items[item.intake_id] = item
        self._by_gmail_message_id[item.gmail_message_id] = item.intake_id

    async def save(self, item: EmailIntakeItem) -> None:
        self._items[item.intake_id] = item
        self._by_gmail_message_id[item.gmail_message_id] = item.intake_id

    async def get(self, intake_id: str) -> EmailIntakeItem | None:
        return self._items.get(intake_id)

    async def get_by_gmail_message_id(self, gmail_message_id: str) -> EmailIntakeItem | None:
        intake_id = self._by_gmail_message_id.get(gmail_message_id)
        return self._items.get(intake_id) if intake_id else None

    async def list(self) -> list[EmailIntakeItem]:
        return sorted(self._items.values(), key=lambda i: i.created_at, reverse=True)
