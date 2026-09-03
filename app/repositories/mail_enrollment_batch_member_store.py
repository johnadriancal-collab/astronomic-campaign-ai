"""
Storage abstraction for MailEnrollmentBatchMember -- the durable, frozen
per-contact cohort behind one MailEnrollmentBatch (Stage 3, 2026-09-03).
See that model's own docstring for why members are written BEFORE the
owning batch row, and why this set is immutable once the batch row exists
(reconciliation only ever advances a member's own `state`, never adds or
removes a member).

`create()` is idempotent -- PRIMARY KEY (batch_id, crm_contact_id) --
matching the "service-layer-computes/store-layer-enforces" composite-key
precedent already used throughout this codebase (CrmContactListMembership,
MailSequenceStep, MailEnrollment). In practice the freeze loop writes each
candidate exactly once (already deduped before this store is ever called),
so this is defense in depth, not the primary correctness mechanism.

`list_distinct_batch_ids_created_before()` and `delete_for_batch()` exist
specifically for MailCampaignService.cleanup_orphan_batch_members() -- see
that method's own docstring for the narrow, conservative-age-threshold
cleanup this powers. Nothing else in this store needs them.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.mail import MailEnrollmentBatchMember


class MailEnrollmentBatchMemberStore(ABC):
    @abstractmethod
    async def create(self, member: MailEnrollmentBatchMember) -> bool:
        """Idempotent. Returns True if this was a new member row, False
        if (batch_id, crm_contact_id) already existed (no-op)."""

    @abstractmethod
    async def save(self, member: MailEnrollmentBatchMember) -> None:
        """Persists a state transition for an EXISTING member. Raises
        ValueError if (batch_id, crm_contact_id) doesn't exist."""

    @abstractmethod
    async def list_for_batch(self, batch_id: str) -> list[MailEnrollmentBatchMember]:
        """Every member of this batch's frozen cohort, in no particular
        guaranteed order -- callers that need a specific order (e.g. "only
        the still-CANDIDATE ones") filter/sort this themselves, matching
        this codebase's general "compute in the caller" convention for
        small, in-memory-sized result sets."""

    @abstractmethod
    async def list_distinct_batch_ids_created_before(self, cutoff: datetime) -> list[str]:
        """Every DISTINCT batch_id with at least one member row whose
        created_at is strictly before `cutoff` -- the candidate set
        cleanup_orphan_batch_members() then checks against the real
        MailEnrollmentBatchStore to find which ones are truly orphaned
        (no owning batch row at all)."""

    @abstractmethod
    async def delete_for_batch(self, batch_id: str) -> int:
        """Permanently deletes every member row for this batch_id. Returns
        the number of rows deleted. A no-op (returns 0) if none exist.
        ONLY ever called by cleanup_orphan_batch_members() against a
        batch_id it has already confirmed has no owning MailEnrollmentBatch
        row -- this store has no way to enforce that itself, by design
        (it doesn't know about MailEnrollmentBatchStore at all)."""


class MemoryMailEnrollmentBatchMemberStore(MailEnrollmentBatchMemberStore):
    """Dict-backed, keyed by (batch_id, crm_contact_id) -- not persistent,
    for tests/local dev."""

    def __init__(self):
        self._members: dict[tuple[str, str], MailEnrollmentBatchMember] = {}

    async def create(self, member: MailEnrollmentBatchMember) -> bool:
        key = (member.batch_id, member.crm_contact_id)
        if key in self._members:
            return False
        self._members[key] = member
        return True

    async def save(self, member: MailEnrollmentBatchMember) -> None:
        key = (member.batch_id, member.crm_contact_id)
        if key not in self._members:
            raise ValueError(f"MailEnrollmentBatchMember not found: {key}")
        self._members[key] = member

    async def list_for_batch(self, batch_id: str) -> list[MailEnrollmentBatchMember]:
        return [m for m in self._members.values() if m.batch_id == batch_id]

    async def list_distinct_batch_ids_created_before(self, cutoff: datetime) -> list[str]:
        return list({m.batch_id for m in self._members.values() if m.created_at < cutoff})

    async def delete_for_batch(self, batch_id: str) -> int:
        keys_to_delete = [key for key in self._members if key[0] == batch_id]
        for key in keys_to_delete:
            del self._members[key]
        return len(keys_to_delete)
