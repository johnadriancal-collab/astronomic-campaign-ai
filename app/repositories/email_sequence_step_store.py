"""
Storage abstraction for EmailSequenceStep -- the deployed-configuration
snapshot rows belonging to one EmailSequence.
"""

from abc import ABC, abstractmethod

from app.models.email_sequence import EmailSequenceStep


class EmailSequenceStepStore(ABC):
    @abstractmethod
    async def create(self, step: EmailSequenceStep) -> None:
        """Persist a newly-created step. Raises if (email_sequence_id, position) already exists."""

    @abstractmethod
    async def save(self, step: EmailSequenceStep) -> None:
        """Persist mutations to an existing step (e.g. a newly-confirmed apollo_step_id)."""

    @abstractmethod
    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailSequenceStep]:
        """Every step for this sequence, in position order."""


class MemoryEmailSequenceStepStore(EmailSequenceStepStore):
    """Dict-backed, keyed by (email_sequence_id, position) -- not persistent, for tests/local dev."""

    def __init__(self):
        self._steps: dict[tuple[str, int], EmailSequenceStep] = {}

    async def create(self, step: EmailSequenceStep) -> None:
        key = (step.email_sequence_id, step.position)
        if key in self._steps:
            raise ValueError(
                f"EmailSequenceStep already exists for sequence {step.email_sequence_id} position {step.position}"
            )
        self._steps[key] = step

    async def save(self, step: EmailSequenceStep) -> None:
        key = (step.email_sequence_id, step.position)
        if key not in self._steps:
            raise ValueError(f"EmailSequenceStep not found: {step.email_sequence_step_id}")
        self._steps[key] = step

    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailSequenceStep]:
        steps = [s for s in self._steps.values() if s.email_sequence_id == email_sequence_id]
        return sorted(steps, key=lambda s: s.position)
