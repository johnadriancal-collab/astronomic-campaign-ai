"""
Storage abstraction for MailOpenEvent -- open tracking (2026-09-18).
`enrollment_step_id` IS the primary key -- exactly one row per step that
has ever recorded a pixel load, ever (see MailOpenEvent's own docstring
in app/models/mail.py). `record_open()` is the ONLY write method, an
upsert: the first call for a given step creates the row (a real "unique
open"); every later call for the SAME step only bumps `last_opened_at`/
`open_count` -- never a second row, never a decrease.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.mail import MailOpenEvent


class MailOpenEventStore(ABC):
    @abstractmethod
    async def record_open(
        self, *, enrollment_step_id: str, mail_campaign_id: str, enrollment_id: str, at: datetime
    ) -> bool:
        """Idempotent upsert. Returns True iff this call created the row
        (i.e. this is the FIRST recorded open for this step -- the
        "unique open" signal callers need); False means a row already
        existed and this call only bumped `last_opened_at`/`open_count`.
        `first_opened_at` is set once, at creation, and never touched by
        any later call."""

    @abstractmethod
    async def get(self, enrollment_step_id: str) -> MailOpenEvent | None:
        """Direct primary-key lookup. None means this step has never
        recorded a single open."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailOpenEvent]:
        """Every open-event row for one campaign -- the read path Open
        rate computation groups by `enrollment_id` over, for a real
        unique-opened-leads count (V1 pilot scale, same
        loop-and-aggregate-in-Python stance as every other read service
        this session)."""


class MemoryMailOpenEventStore(MailOpenEventStore):
    """Dict-backed, keyed by enrollment_step_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailOpenEvent] = {}

    async def record_open(
        self, *, enrollment_step_id: str, mail_campaign_id: str, enrollment_id: str, at: datetime
    ) -> bool:
        existing = self._rows.get(enrollment_step_id)
        if existing is None:
            self._rows[enrollment_step_id] = MailOpenEvent(
                enrollment_step_id=enrollment_step_id,
                mail_campaign_id=mail_campaign_id,
                enrollment_id=enrollment_id,
                first_opened_at=at,
                last_opened_at=at,
                open_count=1,
            )
            return True
        self._rows[enrollment_step_id] = existing.model_copy(
            update={"last_opened_at": at, "open_count": existing.open_count + 1}
        )
        return False

    async def get(self, enrollment_step_id: str) -> MailOpenEvent | None:
        return self._rows.get(enrollment_step_id)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailOpenEvent]:
        return [row for row in self._rows.values() if row.mail_campaign_id == mail_campaign_id]
