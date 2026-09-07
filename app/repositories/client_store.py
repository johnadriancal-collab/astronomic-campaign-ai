"""
Storage abstraction for Client (Client CRM Stage 1A, 2026-09-07).

No `list_for_x` scoping method here -- unlike ClientContact/Engagement/
ClientNote, a Client has no owning parent; `list()` returns every Client.
No `delete()` -- Client CRM's approved archive/soft-delete-only design
(see this stage's own STOP report) means removal is always "set
archived=True, then save()", never a real row deletion; adding a true
delete() method here would invite a destructive-delete code path this
design deliberately does not want to exist.
"""

from abc import ABC, abstractmethod

from app.models.client_crm import Client


class ClientNotFoundError(Exception):
    def __init__(self, client_id: str):
        self.client_id = client_id
        super().__init__(f"Client not found: {client_id}")


class ClientStore(ABC):
    @abstractmethod
    async def create(self, client: Client) -> None:
        """Persist a newly-created Client. client_id is assumed unique
        (minted by the caller, e.g. uuid4)."""

    @abstractmethod
    async def get(self, client_id: str) -> Client | None:
        """Returns the Client, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, client: Client) -> None:
        """Persist mutations to an existing Client, including archiving
        it (archived=True). Raises ClientNotFoundError if client_id
        doesn't exist."""

    @abstractmethod
    async def list(self) -> list[Client]:
        """Every Client (archived or not), ordered by created_at
        ascending. Filtering/sorting for display (by status,
        relationship_classification, name, excluding archived, etc.) is
        the caller's job -- matching this codebase's existing convention
        of filtering in Python over a store's own full list() rather than
        pushing WHERE clauses into the store for a table this small (see
        ActivityLogService.list_events() and CrmService's own contact
        listing for the same convention)."""


class MemoryClientStore(ClientStore):
    """Dict-backed, keyed by client_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, Client] = {}

    async def create(self, client: Client) -> None:
        self._rows[client.client_id] = client

    async def get(self, client_id: str) -> Client | None:
        return self._rows.get(client_id)

    async def save(self, client: Client) -> None:
        if client.client_id not in self._rows:
            raise ClientNotFoundError(client.client_id)
        self._rows[client.client_id] = client

    async def list(self) -> list[Client]:
        return sorted(self._rows.values(), key=lambda c: c.created_at)
