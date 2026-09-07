"""
Storage abstraction for ClientContact (Client CRM Stage 1A, 2026-09-07).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore (see that module's own docstring); removal is always
"archived=True, then save()".
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

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
    async def create_as_primary(self, contact: ClientContact) -> None:
        """Client CRM Stage 1D: persist a newly-created ClientContact
        (contact.is_primary_contact is assumed True) AND, in the same
        atomic operation, clear is_primary_contact on every OTHER
        non-archived ClientContact for contact.client_id. This is the
        only way a new row is inserted with is_primary_contact=True --
        it exists so "at most one active Primary Contact per Client" is
        never violated even momentarily (no window where two rows are
        simultaneously primary), which a separate create() + a separate
        clear-the-old-one call could not guarantee."""

    @abstractmethod
    async def set_primary(self, client_id: str, client_contact_id: str) -> ClientContact:
        """Client CRM Stage 1D: atomically set is_primary_contact=True on
        the given EXISTING ClientContact and clear it on every OTHER
        non-archived ClientContact for the same client_id, in one
        operation -- same invariant as create_as_primary, for the
        "switch which existing contact is primary" case. Raises
        ClientContactNotFoundError if client_contact_id doesn't exist or
        does not belong to client_id. Does not itself reject an archived
        target -- the service layer owns that validation (Client CRM
        Stage 1D's own explicit rule: an archived ClientContact must
        never remain/become Primary)."""

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

    async def create_as_primary(self, contact: ClientContact) -> None:
        for other in self._rows.values():
            if other.client_id == contact.client_id and not other.archived and other.is_primary_contact:
                self._rows[other.client_contact_id] = other.model_copy(update={"is_primary_contact": False})
        self._rows[contact.client_contact_id] = contact

    async def set_primary(self, client_id: str, client_contact_id: str) -> ClientContact:
        target = self._rows.get(client_contact_id)
        if target is None or target.client_id != client_id:
            raise ClientContactNotFoundError(client_contact_id)
        for other in self._rows.values():
            if other.client_id == client_id and not other.archived and other.is_primary_contact and other.client_contact_id != client_contact_id:
                self._rows[other.client_contact_id] = other.model_copy(update={"is_primary_contact": False})
        updated = target.model_copy(update={"is_primary_contact": True, "updated_at": datetime.now(timezone.utc)})
        self._rows[client_contact_id] = updated
        return updated
