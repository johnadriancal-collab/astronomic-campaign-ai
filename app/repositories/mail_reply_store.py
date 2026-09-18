"""
Storage abstraction for MailReply. Reply detection V1 (2026-09-15).
`enrollment_id` IS the primary key -- exactly one row per enrollment,
ever (see MailReply's own docstring in app/models/mail.py). `create()`
is the ONLY general write method -- every OTHER field is never mutated
after creation. `set_reply_preview_if_absent()` (2026-09-18) is a single,
deliberately narrow exception -- see its own docstring.
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

    @abstractmethod
    async def list_all(self) -> list[MailReply]:
        """Every MailReply row that exists, across every campaign, newest
        `detected_at` first -- the Inbox's (2026-09-17) only read path.
        Read-only, no pagination (V1 pilot scale); ordering happens here,
        not left to the caller, so every reader gets the same "newest
        reply first" guarantee this store's docstring promises."""

    @abstractmethod
    async def set_reply_preview_if_absent(self, enrollment_id: str, reply_preview: str) -> bool:
        """The ONE exception to "a MailReply row is never mutated after
        creation" -- and even this is narrower than a general update:
        writes `reply_preview` ONLY if the row currently has none (None
        or empty), and touches no other field. Exists specifically for
        the one-time startup backfill (app/services/
        mail_reply_preview_backfill.py) to fill in a preview for a reply
        that predates this field, WITHOUT ever overwriting a preview a
        future reply-detection run already set at creation time (that
        path sets it directly via MailReply's own constructor, never
        through this method). Returns False if no row exists for this
        enrollment_id, or if it already has a preview (both are no-ops,
        never an error) -- True iff this call is the one that actually
        wrote it, which is exactly the idempotency signal the backfill's
        "skip rows that already have reply_preview" requirement needs."""


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

    async def list_all(self) -> list[MailReply]:
        return sorted(self._rows.values(), key=lambda r: r.detected_at, reverse=True)

    async def set_reply_preview_if_absent(self, enrollment_id: str, reply_preview: str) -> bool:
        row = self._rows.get(enrollment_id)
        if row is None or row.reply_preview:
            return False
        self._rows[enrollment_id] = row.model_copy(update={"reply_preview": reply_preview})
        return True
