"""
Storage abstraction for LumaRegistration -- keyed by Luma's own guest id
(`luma_guest_id`), the real identity/idempotency key for a registration
(see app/services/luma_sync_service.py). `save()` is an upsert: reprocessing
the same guest id (a webhook retry, a legitimate guest.updated, or a
backfill rerun) always updates the SAME row, never creates a duplicate.
"""

from abc import ABC, abstractmethod

from app.models.luma import LumaRegistration


class LumaRegistrationStore(ABC):
    @abstractmethod
    async def save(self, registration: LumaRegistration) -> None:
        """Upsert, keyed on luma_guest_id."""

    @abstractmethod
    async def get(self, luma_guest_id: str) -> LumaRegistration | None: ...

    @abstractmethod
    async def list_for_event(self, luma_event_id: str) -> list[LumaRegistration]: ...

    @abstractmethod
    async def list_for_contact(self, crm_contact_id: str) -> list[LumaRegistration]: ...

    @abstractmethod
    async def list(self) -> list[LumaRegistration]: ...


class MemoryLumaRegistrationStore(LumaRegistrationStore):
    """Dict-backed, keyed by luma_guest_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._registrations: dict[str, LumaRegistration] = {}

    async def save(self, registration: LumaRegistration) -> None:
        self._registrations[registration.luma_guest_id] = registration

    async def get(self, luma_guest_id: str) -> LumaRegistration | None:
        return self._registrations.get(luma_guest_id)

    async def list_for_event(self, luma_event_id: str) -> list[LumaRegistration]:
        return [r for r in self._registrations.values() if r.luma_event_id == luma_event_id]

    async def list_for_contact(self, crm_contact_id: str) -> list[LumaRegistration]:
        return [r for r in self._registrations.values() if r.crm_contact_id == crm_contact_id]

    async def list(self) -> list[LumaRegistration]:
        return list(self._registrations.values())
