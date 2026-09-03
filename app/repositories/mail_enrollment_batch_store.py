"""
Storage abstraction for MailEnrollmentBatch -- a provenance + lifecycle
record per MailCampaignService.add_prospects() call (Phase 2, 2026-09-03;
extended for Stage 3's PREPARING/READY reconciliation lifecycle, same
date). Structurally mirrors mail_sequence_step_store.py's shape (an owned,
multi-row-per-campaign child entity with its own stable id) more than
activity_event_store.py's -- unlike an ActivityEvent, this row DOES mutate
exactly once (PREPARING -> READY, with the four count fields filled in
together at that same moment -- see MailEnrollmentBatch's own docstring
for why never partially) via `save()`, then never again.

UNIQUE(mail_campaign_id, idempotency_key) is the actual DB-level guarantee
behind "a retried POST /prospects with the same idempotency_key resolves
to the same batch, never creates a second one" -- see
DuplicateBatchIdempotencyKeyError and MailCampaignService.add_prospects()'s
own docstring for the full retry/race story, including what happens when
two concurrent submissions race for the same key.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchStatus


class MailEnrollmentBatchNotFoundError(Exception):
    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        super().__init__(f"MailEnrollmentBatch not found: {batch_id}")


class DuplicateBatchIdempotencyKeyError(Exception):
    """Raised by create() when (mail_campaign_id, idempotency_key) already
    belongs to a different batch_id -- the losing side of a genuine
    concurrent-submission race. Callers must NEVER treat this as a generic
    failure: it means a batch for this exact logical submission already
    exists (or is in the middle of being created by someone else) and
    should be looked up via get_by_idempotency_key() and reconciled/
    returned instead -- see MailCampaignService.add_prospects()."""

    def __init__(self, mail_campaign_id: str, idempotency_key: str):
        self.mail_campaign_id = mail_campaign_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"A MailEnrollmentBatch already exists for campaign {mail_campaign_id} with idempotency_key {idempotency_key!r}"
        )


class MailEnrollmentBatchStore(ABC):
    @abstractmethod
    async def create(self, batch: MailEnrollmentBatch) -> None:
        """Persist a newly-created batch. Raises ValueError if batch_id
        already exists; raises DuplicateBatchIdempotencyKeyError if
        (mail_campaign_id, idempotency_key) already belongs to a
        different batch_id -- see that exception's own docstring."""

    @abstractmethod
    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        """Returns the batch, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, batch: MailEnrollmentBatch) -> None:
        """Persists a mutation to an EXISTING batch (in practice: exactly
        one call per batch, PREPARING -> READY with all four counts filled
        in together -- see MailEnrollmentBatch's own docstring). Raises
        MailEnrollmentBatchNotFoundError if batch_id doesn't exist."""

    @abstractmethod
    async def get_by_idempotency_key(self, mail_campaign_id: str, idempotency_key: str) -> MailEnrollmentBatch | None:
        """The lookup that makes a retried add_prospects() call resolve to
        the same batch instead of starting a new one. None if no batch has
        ever been created for this exact (campaign, key) pair."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        """Every batch for this campaign, newest first (created_at DESC)
        -- matching ActivityEventStore.list()'s own ordering convention.
        Empty list for a campaign that has never had add_prospects()
        called against it (including every campaign that predates this
        feature -- see MailEnrollmentBatch's own docstring on why that's
        valid, permanent legacy state, not a gap)."""

    @abstractmethod
    async def list_by_status(self, status: MailEnrollmentBatchStatus) -> list[MailEnrollmentBatch]:
        """Every batch (across ALL campaigns) currently in this status --
        the query behind the startup/periodic reconciliation sweep's
        discovery of stuck PREPARING batches. Not campaign-scoped,
        unlike every other query on this store."""


class MemoryMailEnrollmentBatchStore(MailEnrollmentBatchStore):
    """Dict-backed, keyed by batch_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._batches: dict[str, MailEnrollmentBatch] = {}

    def _key_collides(self, batch: MailEnrollmentBatch) -> bool:
        return any(
            b.mail_campaign_id == batch.mail_campaign_id
            and b.idempotency_key == batch.idempotency_key
            and b.batch_id != batch.batch_id
            for b in self._batches.values()
        )

    async def create(self, batch: MailEnrollmentBatch) -> None:
        if batch.batch_id in self._batches:
            raise ValueError(f"MailEnrollmentBatch already exists: {batch.batch_id}")
        if self._key_collides(batch):
            raise DuplicateBatchIdempotencyKeyError(batch.mail_campaign_id, batch.idempotency_key)
        self._batches[batch.batch_id] = batch

    async def get(self, batch_id: str) -> MailEnrollmentBatch | None:
        return self._batches.get(batch_id)

    async def save(self, batch: MailEnrollmentBatch) -> None:
        if batch.batch_id not in self._batches:
            raise MailEnrollmentBatchNotFoundError(batch.batch_id)
        if self._key_collides(batch):
            raise DuplicateBatchIdempotencyKeyError(batch.mail_campaign_id, batch.idempotency_key)
        self._batches[batch.batch_id] = batch

    async def get_by_idempotency_key(self, mail_campaign_id: str, idempotency_key: str) -> MailEnrollmentBatch | None:
        for b in self._batches.values():
            if b.mail_campaign_id == mail_campaign_id and b.idempotency_key == idempotency_key:
                return b
        return None

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        matching = [b for b in self._batches.values() if b.mail_campaign_id == mail_campaign_id]
        return sorted(matching, key=lambda b: b.created_at, reverse=True)

    async def list_by_status(self, status: MailEnrollmentBatchStatus) -> list[MailEnrollmentBatch]:
        return [b for b in self._batches.values() if b.status == status]
