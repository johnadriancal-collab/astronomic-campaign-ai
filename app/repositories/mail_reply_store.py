"""
Storage abstraction for MailReply. Reply detection V1 (2026-09-15).
`enrollment_id` IS the primary key -- exactly one row per enrollment,
ever (see MailReply's own docstring in app/models/mail.py). `create()`
is the ONLY write method -- there is no update/upsert, matching that a
MailReply row is never mutated after creation.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailReply


class MailReplyStore(ABC):
    @abstractmethod
    async def get(self, enrollment_id: str) -> MailReply | None:
        """Direct primary-key lookup. Returns None if this enrollment has
        no recorded reply -- the exact signal both the send-path's final
        gate and MailReplyDetectionService's candidate query depend on."""

    @abstractmethod
    async def create(self, reply: MailReply) -> bool:
        """Creates the row if one doesn't already exist for this
        enrollment_id. Returns True if created, False if a row already
        existed (a pure no-op in that case, never an error, never
        overwritten) -- THIS is the actual duplicate-detection-processing
        guard; callers must never assume idempotency lives anywhere
        else."""


class MemoryMailReplyStore(MailReplyStore):
    """Dict-backed, keyed by enrollment_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailReply] = {}

    async def get(self, enrollment_id: str) -> MailReply | None:
        return self._rows.get(enrollment_id)

    async def create(self, reply: MailReply) -> bool:
        if reply.enrollment_id in self._rows:
            return False
        self._rows[reply.enrollment_id] = reply
        return True
