"""
Storage abstraction for MailboxHistoryCheckpoint -- bounce detection
(2026-09-18). Same shape/role as LumaBackfillCheckpointStore (see that
store's own docstring) -- a plain upsert of ONE row, keyed by
`mailbox_id` instead of a fixed "default" string (one real checkpoint
per connected mailbox, not one global checkpoint).
"""

from abc import ABC, abstractmethod

from app.models.mail import MailboxHistoryCheckpoint


class MailboxHistoryCheckpointStore(ABC):
    @abstractmethod
    async def save(self, checkpoint: MailboxHistoryCheckpoint) -> None:
        """Upsert, keyed on checkpoint.mailbox_id."""

    @abstractmethod
    async def get(self, mailbox_id: str) -> MailboxHistoryCheckpoint | None:
        """None means this mailbox has never been bootstrapped -- the
        NEXT poll cycle must establish a starting historyId rather than
        processing any history at all (see MailBounceDetectionService's
        own docstring for the exact bootstrap behavior)."""


class MemoryMailboxHistoryCheckpointStore(MailboxHistoryCheckpointStore):
    """Dict-backed, keyed by mailbox_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._checkpoints: dict[str, MailboxHistoryCheckpoint] = {}

    async def save(self, checkpoint: MailboxHistoryCheckpoint) -> None:
        self._checkpoints[checkpoint.mailbox_id] = checkpoint

    async def get(self, mailbox_id: str) -> MailboxHistoryCheckpoint | None:
        return self._checkpoints.get(mailbox_id)
