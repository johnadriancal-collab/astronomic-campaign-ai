"""
Storage abstraction for MailEnrollment. `create()` is idempotent by
contract (same shape as CrmContactListMemberStore.add()) --
UNIQUE(mail_campaign_id, crm_contact_id) is enforced at the DB layer (see
sqlite_mail_enrollment_store.py), and this ABC's `create()` signature
mirrors that: it returns whether a NEW row was actually inserted, never
raises for a repeat, so mark_ready()'s snapshot loop can safely be called
more than once (e.g. after fixing a validation error) without ever
duplicating an enrollment.

`save()` (Phase A addition) is the update path Phase 1 never needed --
enrollments were create-then-read-only until activate_campaign()/
MailSendingService needed to mutate `status` (PENDING -> ACTIVE -> ...).
Unconditional overwrite, matching every other store's `save()` convention
in this codebase (e.g. MailboxStore.save()) -- correct for every caller
EXCEPT mailbox assignment, which needs the real compare-and-swap primitive
below instead (see try_assign_mailbox()'s own docstring for exactly why
"deterministic selection converges to the same value" is not, by itself,
the same claim as "the write is race-safe").
"""

from abc import ABC, abstractmethod

from app.models.mail import MailEnrollment


class MailEnrollmentNotFoundError(Exception):
    def __init__(self, enrollment_id: str):
        self.enrollment_id = enrollment_id
        super().__init__(f"MailEnrollment not found: {enrollment_id}")


class MailEnrollmentStore(ABC):
    @abstractmethod
    async def create(self, enrollment: MailEnrollment) -> bool:
        """Idempotent. Returns True if this was a new enrollment, False if
        (mail_campaign_id, crm_contact_id) was already enrolled (no-op)."""

    @abstractmethod
    async def save(self, enrollment: MailEnrollment) -> None:
        """Unconditional overwrite of an existing row. Raises
        MailEnrollmentNotFoundError if it doesn't exist."""

    @abstractmethod
    async def get(self, enrollment_id: str) -> MailEnrollment | None:
        """Direct lookup by enrollment_id (Phase A addition -- Phase 1 only
        ever needed campaign-scoped listing). Returns None if it doesn't
        exist. Used by MailSendingService.process_one_due_step(), which
        only has a MailEnrollmentStep row's enrollment_id to work from."""

    @abstractmethod
    async def try_assign_mailbox(self, enrollment_id: str, updated: MailEnrollment) -> bool:
        """Atomically applies `updated` (the full row, with
        assigned_mailbox_id already set to the chosen value) IFF the row's
        CURRENT on-disk assigned_mailbox_id is still None. Returns True iff
        this call's write actually applied; False means it was already
        assigned (a lost race, or a repeat call on an already-assigned
        enrollment).

        This exists because deterministic hash-based mailbox selection
        (see MailSendingService._pick_mailbox_deterministic()) guarantees
        two concurrent callers COMPUTE the same value, but that is a
        property of the SELECTION, not the PERSISTENCE -- it does not, by
        itself, make a blind `save()` safe: `save()` overwrites the ENTIRE
        row from whatever the caller last read, so two concurrent
        read-modify-write cycles could otherwise silently clobber an
        unrelated concurrent field change (e.g. a suppression cascade
        flipping `status` in between). try_assign_mailbox() is the one
        real compare-and-swap for this specific field, exactly analogous
        to MailEnrollmentStepStore.try_transition() for step status."""
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollment]:
        """Every enrollment for this campaign."""

    @abstractmethod
    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        """Clears every enrollment row for this campaign -- used by
        unlock_campaign() when a READY campaign's stale snapshot must be
        discarded before it can be edited again. Never touches crm_contacts."""

    @abstractmethod
    async def count_for_campaign(self, mail_campaign_id: str) -> int:
        """Fast count, without materializing every row -- used by the
        campaign list view."""


class MemoryMailEnrollmentStore(MailEnrollmentStore):
    """Dict-backed, keyed by (mail_campaign_id, crm_contact_id) -- not
    persistent, for tests/local dev."""

    def __init__(self):
        self._enrollments: dict[tuple[str, str], MailEnrollment] = {}

    async def create(self, enrollment: MailEnrollment) -> bool:
        key = (enrollment.mail_campaign_id, enrollment.crm_contact_id)
        if key in self._enrollments:
            return False
        self._enrollments[key] = enrollment
        return True

    async def save(self, enrollment: MailEnrollment) -> None:
        key = (enrollment.mail_campaign_id, enrollment.crm_contact_id)
        if key not in self._enrollments:
            raise MailEnrollmentNotFoundError(enrollment.enrollment_id)
        self._enrollments[key] = enrollment

    async def get(self, enrollment_id: str) -> MailEnrollment | None:
        for enrollment in self._enrollments.values():
            if enrollment.enrollment_id == enrollment_id:
                return enrollment
        return None

    async def try_assign_mailbox(self, enrollment_id: str, updated: MailEnrollment) -> bool:
        current = await self.get(enrollment_id)
        if current is None or current.assigned_mailbox_id is not None:
            return False
        key = (current.mail_campaign_id, current.crm_contact_id)
        self._enrollments[key] = updated
        return True

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollment]:
        return [e for e in self._enrollments.values() if e.mail_campaign_id == mail_campaign_id]

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        for key in [k for k in self._enrollments if k[0] == mail_campaign_id]:
            del self._enrollments[key]

    async def count_for_campaign(self, mail_campaign_id: str) -> int:
        return len(await self.list_for_campaign(mail_campaign_id))
