"""
Storage abstraction for CrmContactList -- small, low-volume records (like
CrmCustomFieldDefinition), stored whole via the same JSON-blob convention as
everything else here. No uniqueness constraint on `name`: nothing in the V1
scope calls for unique list names.

Deleting a list is a real, permanent delete (unlike every other entity in
this codebase, which is either immutable or soft-deleted via `archived`) --
a list is a lightweight named container, not a record of real-world data,
so losing it loses nothing about the contacts it referenced. Deleting a
list's ROW here is only half of CrmService.delete_contact_list(): the
membership rows in CrmContactListMemberStore must also be cleared, which is
this store's caller's job, not this store's.
"""

from abc import ABC, abstractmethod

from app.models.crm import CrmContactList


class CrmContactListNotFoundError(Exception):
    def __init__(self, list_id: str):
        self.list_id = list_id
        super().__init__(f"CrmContactList not found: {list_id}")


class CrmContactListStore(ABC):
    @abstractmethod
    async def create(self, contact_list: CrmContactList) -> None:
        """Persist a newly-created list. Raises ValueError if list_id already exists."""

    @abstractmethod
    async def get(self, list_id: str) -> CrmContactList | None:
        """Returns the list, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, contact_list: CrmContactList) -> None:
        """Persist mutations (rename, description edit) to an existing list."""

    @abstractmethod
    async def delete(self, list_id: str) -> None:
        """Permanently deletes the list row. A no-op if it doesn't exist -- the
        caller (CrmService) is responsible for also clearing its memberships."""

    @abstractmethod
    async def list(self) -> list[CrmContactList]:
        """Every stored list -- contact counts are computed separately, by the
        caller, from CrmContactListMemberStore."""


class MemoryCrmContactListStore(CrmContactListStore):
    """Dict-backed, keyed by list_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._lists: dict[str, CrmContactList] = {}

    async def create(self, contact_list: CrmContactList) -> None:
        if contact_list.list_id in self._lists:
            raise ValueError(f"CrmContactList already exists: {contact_list.list_id}")
        self._lists[contact_list.list_id] = contact_list

    async def get(self, list_id: str) -> CrmContactList | None:
        return self._lists.get(list_id)

    async def save(self, contact_list: CrmContactList) -> None:
        if contact_list.list_id not in self._lists:
            raise CrmContactListNotFoundError(contact_list.list_id)
        self._lists[contact_list.list_id] = contact_list

    async def delete(self, list_id: str) -> None:
        self._lists.pop(list_id, None)

    async def list(self) -> list[CrmContactList]:
        return list(self._lists.values())
