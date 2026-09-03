"""
Storage abstraction for MailEnrollmentBatch -- an append-only provenance
record per MailCampaignService.add_prospects() call (Phase 2, 2026-09-03).
Structurally mirrors mail_sequence_step_store.py's shape (an owned,
multi-row-per-campaign child entity with its own stable id and a `create`/
`list_for_campaign` surface) rather than activity_event_store.py's, since
callers need a single batch by id (GET .../batches/{id}, a later phase) as
well as the full campaign history -- but no `save`/`delete` at all: a batch
row is immutable and permanent once created, same "never edited, never
removed" discipline as ActivityEvent.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailEnrollmentBatch


class MailEnrollmentBatchStore(ABC):
    @abstractmethod
    async def create(self, batch: MailEnrollmentBatch) -> None:
        """Persist a newly-created batch. Raises ValueError if batch_id
        already exists."""

    @abstractmethod
    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        """Returns the batch, or None if it doesn't exist."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        """Every batch for this campaign, newest first (created_at DESC)
        -- matching ActivityEventStore.list()'s own ordering convention.
        Empty list for a campaign that has never had add_prospects()
        called against it (including every campaign that predates this
        feature -- see MailEnrollmentBatch's own docstring on why that's
        valid, permanent legacy state, not a gap)."""


class MemoryMailEnrollmentBatchStore(MailEnrollmentBatchStore):
    """Dict-backed, keyed by batch_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._batches: dict[str, MailEnrollmentBatch] = {}

    async def create(self, batch: MailEnrollmentBatch) -> None:
        if batch.batch_id in self._batches:
            raise ValueError(f"MailEnrollmentBatch already exists: {batch.batch_id}")
        self._batches[batch.batch_id] = batch

    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        return self._batches.get(batch_id)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        matching = [b for b in self._batches.values() if b.mail_campaign_id == mail_campaign_id]
        return sorted(matching, key=lambda b: b.created_at, reverse=True)
