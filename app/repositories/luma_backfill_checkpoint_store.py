"""
Storage abstraction for LumaBackfillCheckpoint -- durable resume state for
the one-time historical backfill (app/services/luma_sync_service.py).
Single-calendar scope, so there is only ever ONE checkpoint row
("default") -- `save()` is a plain upsert of that one row.
"""

from abc import ABC, abstractmethod

from app.models.luma import LumaBackfillCheckpoint

DEFAULT_CHECKPOINT_ID = "default"


class LumaBackfillCheckpointStore(ABC):
    @abstractmethod
    async def save(self, checkpoint: LumaBackfillCheckpoint) -> None:
        """Upsert, keyed on checkpoint.checkpoint_id (always "default" today)."""

    @abstractmethod
    async def get(self, checkpoint_id: str = DEFAULT_CHECKPOINT_ID) -> LumaBackfillCheckpoint | None: ...


class MemoryLumaBackfillCheckpointStore(LumaBackfillCheckpointStore):
    """Dict-backed -- not persistent, for tests/local dev."""

    def __init__(self):
        self._checkpoints: dict[str, LumaBackfillCheckpoint] = {}

    async def save(self, checkpoint: LumaBackfillCheckpoint) -> None:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint

    async def get(self, checkpoint_id: str = DEFAULT_CHECKPOINT_ID) -> LumaBackfillCheckpoint | None:
        return self._checkpoints.get(checkpoint_id)
