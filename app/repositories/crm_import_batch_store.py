"""
Storage abstraction for CrmImportBatch -- persisted immediately on upload
so the multi-step (upload -> preview -> commit) import flow never holds
parsed rows only in memory between requests, same "always persist, never
lose in-flight state" philosophy as the campaign_creation pipeline.
"""

from abc import ABC, abstractmethod

from app.models.crm import CrmImportBatch


class CrmImportBatchNotFoundError(Exception):
    def __init__(self, import_batch_id: str):
        self.import_batch_id = import_batch_id
        super().__init__(f"CrmImportBatch not found: {import_batch_id}")


class CrmImportBatchStore(ABC):
    @abstractmethod
    async def create(self, batch: CrmImportBatch) -> None:
        """Persist a newly-uploaded batch. Raises ValueError if import_batch_id already exists."""

    @abstractmethod
    async def get(self, import_batch_id: str) -> CrmImportBatch | None:
        """Returns the batch, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, batch: CrmImportBatch) -> None:
        """Persist mutations (mapping confirmed, preview computed, committed)."""


class MemoryCrmImportBatchStore(CrmImportBatchStore):
    """Dict-backed, keyed by import_batch_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._batches: dict[str, CrmImportBatch] = {}

    async def create(self, batch: CrmImportBatch) -> None:
        if batch.import_batch_id in self._batches:
            raise ValueError(f"CrmImportBatch already exists: {batch.import_batch_id}")
        self._batches[batch.import_batch_id] = batch

    async def get(self, import_batch_id: str) -> CrmImportBatch | None:
        return self._batches.get(import_batch_id)

    async def save(self, batch: CrmImportBatch) -> None:
        if batch.import_batch_id not in self._batches:
            raise CrmImportBatchNotFoundError(batch.import_batch_id)
        self._batches[batch.import_batch_id] = batch
