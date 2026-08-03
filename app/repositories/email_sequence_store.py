"""
Storage abstraction for EmailSequence -- mirrors CampaignStore/LeadStore
exactly: EmailSequenceSyncService depends only on this interface.
"""

from abc import ABC, abstractmethod

from app.models.email_sequence import EmailSequence


class EmailSequenceNotFoundError(Exception):
    def __init__(self, email_sequence_id: str):
        self.email_sequence_id = email_sequence_id
        super().__init__(f"EmailSequence not found: {email_sequence_id}")


class EmailSequenceStore(ABC):
    @abstractmethod
    async def create(self, sequence: EmailSequence) -> None:
        """Persist a newly-created sequence. Raises if the id or campaign_id already exists."""

    @abstractmethod
    async def get(self, email_sequence_id: str) -> EmailSequence | None:
        """Returns the sequence, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_campaign_id(self, campaign_id: str) -> EmailSequence | None:
        """The primary lookup -- EmailSequence is 1:1 with Campaign today."""

    @abstractmethod
    async def save(self, sequence: EmailSequence) -> None:
        """Persist mutations to an existing sequence."""


class MemoryEmailSequenceStore(EmailSequenceStore):
    """Dictionary-backed, keyed by email_sequence_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._sequences: dict[str, EmailSequence] = {}

    async def create(self, sequence: EmailSequence) -> None:
        if sequence.email_sequence_id in self._sequences:
            raise ValueError(f"EmailSequence already exists: {sequence.email_sequence_id}")
        if any(s.campaign_id == sequence.campaign_id for s in self._sequences.values()):
            raise ValueError(f"EmailSequence already exists for campaign: {sequence.campaign_id}")
        self._sequences[sequence.email_sequence_id] = sequence

    async def get(self, email_sequence_id: str) -> EmailSequence | None:
        return self._sequences.get(email_sequence_id)

    async def get_by_campaign_id(self, campaign_id: str) -> EmailSequence | None:
        for sequence in self._sequences.values():
            if sequence.campaign_id == campaign_id:
                return sequence
        return None

    async def save(self, sequence: EmailSequence) -> None:
        if sequence.email_sequence_id not in self._sequences:
            raise EmailSequenceNotFoundError(sequence.email_sequence_id)
        self._sequences[sequence.email_sequence_id] = sequence
