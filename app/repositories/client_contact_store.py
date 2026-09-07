"""
Storage abstraction for ClientContact (Client CRM Stage 1A, 2026-09-07).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore (see that module's own docstring); removal is always
"archived=True, then save()".
"""

from abc import ABC, abstractmethod

from app.models.client_crm import ClientContact


class ClientContactNotFoundError(Exception):
    def __init__(self, client_contact_id: str):
        self.client_contact_id = client_contact_id
        super().__init__(f"ClientContact not found: {client_contact_id}")


class ClientContactStore(ABC):
    @abstractmethod
    async def create(self, contact: ClientContact) -> None:
        """Persist a newly-created ClientContact. client_contact_id is
        assumed unique (minted by the caller, e.g. uuid4) -- this store
        does not itself enforce that a given crm_contact_id can only be
        linked to one ClientContact per Client (or at all); Stage 1D's
        own dedup/linking flow owns that decision, not this store."""

    @abstractmethod
    async def get(self, client_contact_id: str) -> ClientContact | None:
        """Returns the ClientContact, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, contact: ClientContact) -> None:
        """Persist mutations to an existing ClientContact, including
        archiving it. Raises ClientContactNotFoundError if
        client_contact_id doesn't exist."""

    @abstractmethod
    async def list_for_client(self, client_id: str) -> list[ClientContact]:
        """Every ClientContact for this Client (archived or not),
        ordered by created_at ascending -- the Client detail page's own
        Contacts tab load."""

    @abstractmethod
    async def list_for_crm_contact(self, crm_contact_id: str) -> list[ClientContact]:
        """Every ClientContact linked to this CrmContact, across every
        Client -- e.g. "has this person already been linked to a client
        relationship" (Stage 1D's own dedup flow) and a future "show this
        person's Client CRM relationships" panel on the existing CRM
        contact detail page. Usually zero or one result, but never
        assumed to be -- the same person could, in principle, be a
        ClientContact at more than one Client."""


class MemoryClientContactStore(ClientContactStore):
    """Dict-backed, keyed by client_contact_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, ClientContact] = {}

    async def create(self, contact: ClientContact) -> None:
        self._rows[contact.client_contact_id] = contact

    async def get(self, client_contact_id: str) -> ClientContact | None:
        return self._rows.get(client_contact_id)

    async def save(self, contact: ClientContact) -> None:
        if contact.client_contact_id not in self._rows:
            raise ClientContactNotFoundError(contact.client_contact_id)
        self._rows[contact.client_contact_id] = contact

    async def list_for_client(self, client_id: str) -> list[ClientContact]:
        rows = [c for c in self._rows.values() if c.client_id == client_id]
        return sorted(rows, key=lambda c: c.created_at)

    async def list_for_crm_contact(self, crm_contact_id: str) -> list[ClientContact]:
        rows = [c for c in self._rows.values() if c.crm_contact_id == crm_contact_id]
        return sorted(rows, key=lambda c: c.created_at)
