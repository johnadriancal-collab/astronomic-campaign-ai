"""
Storage abstraction for the ITF ingestion idempotency ledger -- see
ItfIngestionLogEntry's docstring for the exact skip/retry/reprocess rule
this backs. `save()` is an upsert keyed on row_number (a row is logged once
per poll it's actually processed in, and a retry overwrites its own prior
entry) -- never called during a dry run, so a dry run never advances this
ledger.
"""

from abc import ABC, abstractmethod

from app.models.itf import ItfIngestionLogEntry


class ItfIngestionLogStore(ABC):
    @abstractmethod
    async def save(self, entry: ItfIngestionLogEntry) -> None:
        """Upsert, keyed on row_number."""

    @abstractmethod
    async def get(self, row_number: int) -> ItfIngestionLogEntry | None:
        ...

    @abstractmethod
    async def get_all(self) -> dict[int, ItfIngestionLogEntry]:
        """Every logged row, keyed by row_number -- fetched once per sync run
        so the row loop can check idempotency in memory rather than one query
        per row."""


class MemoryItfIngestionLogStore(ItfIngestionLogStore):
    """Dict-backed, keyed by row_number -- not persistent, for tests/local dev."""

    def __init__(self):
        self._entries: dict[int, ItfIngestionLogEntry] = {}

    async def save(self, entry: ItfIngestionLogEntry) -> None:
        self._entries[entry.row_number] = entry

    async def get(self, row_number: int) -> ItfIngestionLogEntry | None:
        return self._entries.get(row_number)

    async def get_all(self) -> dict[int, ItfIngestionLogEntry]:
        return dict(self._entries)
