"""
Storage abstraction for MailLeadStartTrigger (Trigger feature, Stage 5A,
2026-09-04). Structurally mirrors mail_sequence_step_store.py -- an owned,
multi-row-per-campaign entity with its own stable id, individually
addressable -- rather than mail_send_window_store.py's whole-set-replace
shape, since a trigger is edited/deleted one at a time, not as a single
atomic "Save" of the campaign's entire trigger list.

Stage 5A implements persistence ONLY -- no service or endpoint calls any
of this yet.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailLeadStartTrigger


class MailLeadStartTriggerNotFoundError(Exception):
    def __init__(self, trigger_id: str):
        self.trigger_id = trigger_id
        super().__init__(f"MailLeadStartTrigger not found: {trigger_id}")


class MailLeadStartTriggerStore(ABC):
    @abstractmethod
    async def create(self, trigger: MailLeadStartTrigger) -> None:
        """Persist a newly-created trigger. trigger_id is assumed unique
        (minted by the caller, e.g. uuid4) -- not itself a uniqueness
        constraint this store enforces beyond the primary key."""

    @abstractmethod
    async def get(self, trigger_id: str) -> MailLeadStartTrigger | None:
        """Returns the trigger, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, trigger: MailLeadStartTrigger) -> None:
        """Persist mutations to an existing trigger. Raises
        MailLeadStartTriggerNotFoundError if trigger_id doesn't exist."""

    @abstractmethod
    async def delete(self, trigger_id: str) -> None:
        """Permanently deletes the trigger row. A no-op if it doesn't exist."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailLeadStartTrigger]:
        """Every trigger for this campaign (enabled or not), ordered by
        created_at ascending."""


class MemoryMailLeadStartTriggerStore(MailLeadStartTriggerStore):
    """Dict-backed, keyed by trigger_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, MailLeadStartTrigger] = {}

    async def create(self, trigger: MailLeadStartTrigger) -> None:
        self._rows[trigger.trigger_id] = trigger

    async def get(self, trigger_id: str) -> MailLeadStartTrigger | None:
        return self._rows.get(trigger_id)

    async def save(self, trigger: MailLeadStartTrigger) -> None:
        if trigger.trigger_id not in self._rows:
            raise MailLeadStartTriggerNotFoundError(trigger.trigger_id)
        self._rows[trigger.trigger_id] = trigger

    async def delete(self, trigger_id: str) -> None:
        self._rows.pop(trigger_id, None)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailLeadStartTrigger]:
        rows = [t for t in self._rows.values() if t.mail_campaign_id == mail_campaign_id]
        return sorted(rows, key=lambda t: t.created_at)
