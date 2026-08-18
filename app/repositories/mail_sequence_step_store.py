"""
Storage abstraction for MailSequenceStep. `create()` must enforce
UNIQUE(mail_campaign_id, step_number) -- this is the actual backstop for
"prevent duplicate step numbers within one campaign" (the service layer
auto-assigns the next number in normal usage; this constraint is what
protects against a genuine race or bug, matching the same
service-layer-computes / store-layer-enforces pattern already used for
CrmContactListMembership's composite key).
"""

from abc import ABC, abstractmethod

from app.models.mail import MailSequenceStep


class MailSequenceStepNotFoundError(Exception):
    def __init__(self, step_id: str):
        self.step_id = step_id
        super().__init__(f"MailSequenceStep not found: {step_id}")


class DuplicateMailSequenceStepNumberError(Exception):
    def __init__(self, mail_campaign_id: str, step_number: int):
        self.mail_campaign_id = mail_campaign_id
        self.step_number = step_number
        super().__init__(f"Step number {step_number} already exists for campaign {mail_campaign_id}")


class MailSequenceStepStore(ABC):
    @abstractmethod
    async def create(self, step: MailSequenceStep) -> None:
        """Raises DuplicateMailSequenceStepNumberError if
        (mail_campaign_id, step_number) already exists."""

    @abstractmethod
    async def get(self, step_id: str) -> MailSequenceStep | None:
        """Returns the step, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, step: MailSequenceStep) -> None:
        """Persist mutations to an existing step (including a renumber)."""

    @abstractmethod
    async def delete(self, step_id: str) -> None:
        """Permanently deletes the step row. A no-op if it doesn't exist."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSequenceStep]:
        """Every step for this campaign, ordered by step_number ascending --
        the deterministic ordering guarantee callers rely on."""


class MemoryMailSequenceStepStore(MailSequenceStepStore):
    """Dict-backed, keyed by step_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._steps: dict[str, MailSequenceStep] = {}

    def _collides(self, step: MailSequenceStep) -> bool:
        return any(
            s.mail_campaign_id == step.mail_campaign_id
            and s.step_number == step.step_number
            and s.step_id != step.step_id
            for s in self._steps.values()
        )

    async def create(self, step: MailSequenceStep) -> None:
        if step.step_id in self._steps:
            raise ValueError(f"MailSequenceStep already exists: {step.step_id}")
        if self._collides(step):
            raise DuplicateMailSequenceStepNumberError(step.mail_campaign_id, step.step_number)
        self._steps[step.step_id] = step

    async def get(self, step_id: str) -> MailSequenceStep | None:
        return self._steps.get(step_id)

    async def save(self, step: MailSequenceStep) -> None:
        if step.step_id not in self._steps:
            raise MailSequenceStepNotFoundError(step.step_id)
        if self._collides(step):
            raise DuplicateMailSequenceStepNumberError(step.mail_campaign_id, step.step_number)
        self._steps[step.step_id] = step

    async def delete(self, step_id: str) -> None:
        self._steps.pop(step_id, None)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailSequenceStep]:
        matching = [s for s in self._steps.values() if s.mail_campaign_id == mail_campaign_id]
        return sorted(matching, key=lambda s: s.step_number)
