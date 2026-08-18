"""
Storage abstraction for MailSuppression. `email_normalized` IS the primary
key -- exactly one row per address, ever (see MailSuppression's docstring
in app/models/mail.py for why re-suppression/unsuppression mutate that one
row in place rather than creating new rows). `get()` by normalized email is
a direct primary-key lookup at every layer, satisfying "suppression lookup
must be fast" without any extra index.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailSuppression


class MailSuppressionStore(ABC):
    @abstractmethod
    async def get(self, email_normalized: str) -> MailSuppression | None:
        """Direct primary-key lookup. Returns None if this email has never
        been suppressed at all."""

    @abstractmethod
    async def upsert(self, suppression: MailSuppression) -> None:
        """Creates the row if it doesn't exist, otherwise overwrites it in
        place (same row, by email_normalized) -- this is how both a fresh
        suppression and a reactivation/deactivation are persisted."""

    @abstractmethod
    async def list(self) -> list[MailSuppression]:
        """Every suppression row, active or not -- callers filter by
        `active` themselves (see MailSuppressionService)."""


class MemoryMailSuppressionStore(MailSuppressionStore):
    """Dict-backed, keyed by email_normalized -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailSuppression] = {}

    async def get(self, email_normalized: str) -> MailSuppression | None:
        return self._rows.get(email_normalized)

    async def upsert(self, suppression: MailSuppression) -> None:
        self._rows[suppression.email_normalized] = suppression

    async def list(self) -> list[MailSuppression]:
        return list(self._rows.values())
