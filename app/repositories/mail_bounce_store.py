"""
Storage abstraction for MailBounce -- bounce detection (2026-09-18).
`gmail_message_id` IS the primary key -- exactly one row per detected,
attributed DSN, ever (see MailBounce's own docstring in app/models/mail.py
for why THAT is the dedup key, not enrollment_step_id). `create()` is the
ONLY write method -- every field is set once, at detection time, never
mutated after creation.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailBounce


class MailBounceStore(ABC):
    @abstractmethod
    async def create(self, bounce: MailBounce) -> bool:
        """Creates the row if one doesn't already exist for this
        gmail_message_id. Returns True if created, False if a row
        already existed (a pure no-op in that case, never an error,
        never overwritten) -- THIS is the actual duplicate-DSN-
        processing guard; callers must never assume idempotency lives
        anywhere else."""

    @abstractmethod
    async def get(self, gmail_message_id: str) -> MailBounce | None:
        """Direct primary-key lookup. None means this exact DSN message
        has never been recorded."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailBounce]:
        """Every bounce row for one campaign -- the read path Bounce
        rate computation groups by `enrollment_id` over, for a real
        unique-bounced-leads count (V1 pilot scale, same
        loop-and-aggregate-in-Python stance as every other read service
        this session)."""


class MemoryMailBounceStore(MailBounceStore):
    """Dict-backed, keyed by gmail_message_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailBounce] = {}

    async def create(self, bounce: MailBounce) -> bool:
        if bounce.gmail_message_id in self._rows:
            return False
        self._rows[bounce.gmail_message_id] = bounce
        return True

    async def get(self, gmail_message_id: str) -> MailBounce | None:
        return self._rows.get(gmail_message_id)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailBounce]:
        return [row for row in self._rows.values() if row.mail_campaign_id == mail_campaign_id]
