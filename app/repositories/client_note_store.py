"""
Storage abstraction for ClientNote (Client CRM Stage 1A, 2026-09-07).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore (see that module's own docstring); removal is always
"archived=True, then save()". Notes are also meant to be historically
immutable in spirit once written (see ClientNote's own model docstring) --
`save()` still exists so a genuine correction/archive is possible, but
nothing in this stage rewrites a note's own content as part of any normal
flow.
"""

from abc import ABC, abstractmethod

from app.models.client_crm import ClientNote


class ClientNoteNotFoundError(Exception):
    def __init__(self, client_note_id: str):
        self.client_note_id = client_note_id
        super().__init__(f"ClientNote not found: {client_note_id}")


class ClientNoteStore(ABC):
    @abstractmethod
    async def create(self, note: ClientNote) -> None:
        """Persist a newly-created ClientNote. client_note_id is assumed
        unique (minted by the caller, e.g. uuid4)."""

    @abstractmethod
    async def get(self, client_note_id: str) -> ClientNote | None:
        """Returns the ClientNote, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, note: ClientNote) -> None:
        """Persist mutations to an existing ClientNote, including
        archiving it. Raises ClientNoteNotFoundError if client_note_id
        doesn't exist."""

    @abstractmethod
    async def list_for_client(self, client_id: str) -> list[ClientNote]:
        """Every ClientNote for this Client (archived or not, general or
        Engagement-linked), ordered by created_at ascending -- the Client
        detail page's own Activity tab load. Sorting for display by
        `occurred_at` (the note's own real-world time, which may differ
        from created_at for a backfilled note) is the caller's job -- see
        this store's own module docstring for why no separate index
        exists for that at this table's expected size."""

    @abstractmethod
    async def list_for_engagement(self, engagement_id: str) -> list[ClientNote]:
        """Every ClientNote linked to this specific Engagement, ordered
        by created_at ascending -- an Engagement detail/notes-for-this-
        dinner view."""


class MemoryClientNoteStore(ClientNoteStore):
    """Dict-backed, keyed by client_note_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, ClientNote] = {}

    async def create(self, note: ClientNote) -> None:
        self._rows[note.client_note_id] = note

    async def get(self, client_note_id: str) -> ClientNote | None:
        return self._rows.get(client_note_id)

    async def save(self, note: ClientNote) -> None:
        if note.client_note_id not in self._rows:
            raise ClientNoteNotFoundError(note.client_note_id)
        self._rows[note.client_note_id] = note

    async def list_for_client(self, client_id: str) -> list[ClientNote]:
        rows = [n for n in self._rows.values() if n.client_id == client_id]
        return sorted(rows, key=lambda n: n.created_at)

    async def list_for_engagement(self, engagement_id: str) -> list[ClientNote]:
        rows = [n for n in self._rows.values() if n.engagement_id == engagement_id]
        return sorted(rows, key=lambda n: n.created_at)
