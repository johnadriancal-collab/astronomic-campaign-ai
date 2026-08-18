"""
Storage abstraction for MailEnrollment. `create()` is idempotent by
contract (same shape as CrmContactListMemberStore.add()) --
UNIQUE(mail_campaign_id, crm_contact_id) is enforced at the DB layer (see
sqlite_mail_enrollment_store.py), and this ABC's `create()` signature
mirrors that: it returns whether a NEW row was actually inserted, never
raises for a repeat, so mark_ready()'s snapshot loop can safely be called
more than once (e.g. after fixing a validation error) without ever
duplicating an enrollment.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailEnrollment


class MailEnrollmentStore(ABC):
    @abstractmethod
    async def create(self, enrollment: MailEnrollment) -> bool:
        """Idempotent. Returns True if this was a new enrollment, False if
        (mail_campaign_id, crm_contact_id) was already enrolled (no-op)."""

    @abstractmethod
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

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollment]:
        return [e for e in self._enrollments.values() if e.mail_campaign_id == mail_campaign_id]

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        for key in [k for k in self._enrollments if k[0] == mail_campaign_id]:
            del self._enrollments[key]

    async def count_for_campaign(self, mail_campaign_id: str) -> int:
        return len(await self.list_for_campaign(mail_campaign_id))
