"""
Storage abstraction for Engagement (Client CRM Stage 1A, 2026-09-07).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore (see that module's own docstring); removal is always
"archived=True, then save()".
"""

from abc import ABC, abstractmethod

from app.models.client_crm import Engagement


class EngagementNotFoundError(Exception):
    def __init__(self, engagement_id: str):
        self.engagement_id = engagement_id
        super().__init__(f"Engagement not found: {engagement_id}")


class EngagementStore(ABC):
    @abstractmethod
    async def create(self, engagement: Engagement) -> None:
        """Persist a newly-created Engagement. engagement_id is assumed
        unique (minted by the caller, e.g. uuid4)."""

    @abstractmethod
    async def get(self, engagement_id: str) -> Engagement | None:
        """Returns the Engagement, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, engagement: Engagement) -> None:
        """Persist mutations to an existing Engagement, including
        archiving it. Raises EngagementNotFoundError if engagement_id
        doesn't exist."""

    @abstractmethod
    async def list_for_client(self, client_id: str) -> list[Engagement]:
        """Every Engagement for this Client (archived or not), ordered by
        created_at ascending -- the Client detail page's own Dinners/
        Engagements tab load. Sorting by `engagement_date` (rather than created_at)
        for display, or filtering by `status`, is the caller's job (see
        this store's own module docstring for why no additional index
        exists yet for those -- no concrete cross-client query needs
        them today)."""


class MemoryEngagementStore(EngagementStore):
    """Dict-backed, keyed by engagement_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, Engagement] = {}

    async def create(self, engagement: Engagement) -> None:
        self._rows[engagement.engagement_id] = engagement

    async def get(self, engagement_id: str) -> Engagement | None:
        return self._rows.get(engagement_id)

    async def save(self, engagement: Engagement) -> None:
        if engagement.engagement_id not in self._rows:
            raise EngagementNotFoundError(engagement.engagement_id)
        self._rows[engagement.engagement_id] = engagement

    async def list_for_client(self, client_id: str) -> list[Engagement]:
        rows = [e for e in self._rows.values() if e.client_id == client_id]
        return sorted(rows, key=lambda e: e.created_at)
