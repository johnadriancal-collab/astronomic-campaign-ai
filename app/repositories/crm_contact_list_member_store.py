"""
Storage abstraction for the CrmContactList<->CrmContact membership join --
same shape and idempotency contract as CampaignLeadStore (see that module's
docstring): `add()` is a no-op on an existing (list_id, crm_contact_id) pair
rather than raising, since re-adding a contact that's already a member is
an expected, harmless action (not a bug to surface), and V1 explicitly
requires duplicate membership to never happen.

Only ever stores the reference (crm_contact_id) -- never a copy of the
contact itself. The full CrmContact is always re-fetched from
CrmContactStore by the caller (CrmService), so an edit to a contact is
visible everywhere it's listed with no extra step.
"""

from abc import ABC, abstractmethod

from app.models.crm import CrmContactListMembership


class CrmContactListMemberStore(ABC):
    @abstractmethod
    async def add(self, membership: CrmContactListMembership) -> bool:
        """Idempotent. Returns True if this was a new membership, False if
        (list_id, crm_contact_id) was already a member (no-op)."""

    @abstractmethod
    async def remove(self, list_id: str, crm_contact_id: str) -> bool:
        """Idempotent. Returns True if a membership was actually removed, False
        if the pair wasn't a member to begin with (no-op, not an error)."""

    @abstractmethod
    async def remove_all_for_list(self, list_id: str) -> None:
        """Cascade delete for CrmService.delete_contact_list() -- clears every
        membership row for this list. Never touches crm_contacts."""

    @abstractmethod
    async def list_contact_ids_for_list(self, list_id: str) -> list[str]:
        """Every crm_contact_id currently a member of this list."""

    @abstractmethod
    async def list_ids_for_contact(self, crm_contact_id: str) -> list[str]:
        """Every list_id this contact currently belongs to."""

    @abstractmethod
    async def count_by_list(self) -> dict[str, int]:
        """{list_id: member_count} for every list that has at least one member --
        one call for the whole /crm/lists index page, not one query per list."""


class MemoryCrmContactListMemberStore(CrmContactListMemberStore):
    """Dict-backed, keyed by (list_id, crm_contact_id) -- not persistent, for tests/local dev."""

    def __init__(self):
        self._memberships: dict[tuple[str, str], CrmContactListMembership] = {}

    async def add(self, membership: CrmContactListMembership) -> bool:
        key = (membership.list_id, membership.crm_contact_id)
        if key in self._memberships:
            return False
        self._memberships[key] = membership
        return True

    async def remove(self, list_id: str, crm_contact_id: str) -> bool:
        return self._memberships.pop((list_id, crm_contact_id), None) is not None

    async def remove_all_for_list(self, list_id: str) -> None:
        for key in [k for k in self._memberships if k[0] == list_id]:
            del self._memberships[key]

    async def list_contact_ids_for_list(self, list_id: str) -> list[str]:
        return [m.crm_contact_id for m in self._memberships.values() if m.list_id == list_id]

    async def list_ids_for_contact(self, crm_contact_id: str) -> list[str]:
        return [m.list_id for m in self._memberships.values() if m.crm_contact_id == crm_contact_id]

    async def count_by_list(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self._memberships.values():
            counts[m.list_id] = counts.get(m.list_id, 0) + 1
        return counts
