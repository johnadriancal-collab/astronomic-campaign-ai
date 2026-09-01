"""
Storage abstraction for MailboxSendPolicy -- see that model's own docstring
(app/models/mailbox.py) for why a MISSING row is a fully valid, expected
state (never an error, never something requiring a backfill write for an
already-connected mailbox). This store's `get()` simply returns None for a
mailbox with no policy row -- it is MailSendingService.
resolve_mailbox_send_policy()'s job, not this store's, to treat "no row"
and "a row whose override fields are both null" identically (system
defaults either way).
"""

from abc import ABC, abstractmethod

from app.models.mailbox import MailboxSendPolicy


class MailboxSendPolicyStore(ABC):
    @abstractmethod
    async def get(self, mailbox_id: str) -> MailboxSendPolicy | None:
        """None if this mailbox has no policy row -- a normal, expected
        state, not an error. Callers must resolve this the same as a row
        with null overrides (system defaults) -- see
        MailSendingService.resolve_mailbox_send_policy()."""

    @abstractmethod
    async def upsert(self, policy: MailboxSendPolicy) -> None:
        """Creates the row if none exists for this mailbox_id, otherwise
        overwrites it in place -- there is deliberately no separate
        create()/save() pair here (unlike most stores in this codebase):
        a send policy is a single, idempotent "set this mailbox's
        overrides to exactly this" operation, never an append-only or
        must-not-already-exist one."""


class MemoryMailboxSendPolicyStore(MailboxSendPolicyStore):
    """Dict-backed, keyed by mailbox_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailboxSendPolicy] = {}

    async def get(self, mailbox_id: str) -> MailboxSendPolicy | None:
        return self._rows.get(mailbox_id)

    async def upsert(self, policy: MailboxSendPolicy) -> None:
        self._rows[policy.mailbox_id] = policy
