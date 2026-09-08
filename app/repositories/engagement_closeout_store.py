"""
Storage abstraction for EngagementCloseout (Client CRM Stage 1F, 2026-09-08).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore/EngagementStore (see their own docstrings); removal is always
"archived=True, then save()".

No uniqueness constraint at this layer for "at most one EngagementCloseout
per Engagement" -- exactly the same precedent as ClientContactStore's own
docstring for is_primary_contact: this store does not itself enforce it;
ClientCrmService's own create_engagement_closeout() owns that invariant by
checking get_for_engagement() before create().
"""

from abc import ABC, abstractmethod

from app.models.client_crm import EngagementCloseout


class EngagementCloseoutNotFoundError(Exception):
    def __init__(self, closeout_id: str):
        self.closeout_id = closeout_id
        super().__init__(f"EngagementCloseout not found: {closeout_id}")


class EngagementCloseoutStore(ABC):
    @abstractmethod
    async def create(self, closeout: EngagementCloseout) -> None:
        """Persist a newly-created EngagementCloseout. closeout_id is
        assumed unique (minted by the caller, e.g. uuid4) -- this store
        does not itself enforce at-most-one-per-engagement; see this
        module's own docstring."""

    @abstractmethod
    async def get(self, closeout_id: str) -> EngagementCloseout | None:
        """Returns the EngagementCloseout, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, closeout: EngagementCloseout) -> None:
        """Persist mutations to an existing EngagementCloseout, including
        archiving it. Raises EngagementCloseoutNotFoundError if
        closeout_id doesn't exist."""

    @abstractmethod
    async def get_for_engagement(self, engagement_id: str) -> EngagementCloseout | None:
        """The one EngagementCloseout for this Engagement (archived or
        not), or None if none was ever created. Used both to load "the"
        closeout for the Engagement detail page and, by the service
        layer, to enforce at-most-one-per-engagement before create()."""


class MemoryEngagementCloseoutStore(EngagementCloseoutStore):
    """Dict-backed, keyed by closeout_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, EngagementCloseout] = {}

    async def create(self, closeout: EngagementCloseout) -> None:
        self._rows[closeout.closeout_id] = closeout

    async def get(self, closeout_id: str) -> EngagementCloseout | None:
        return self._rows.get(closeout_id)

    async def save(self, closeout: EngagementCloseout) -> None:
        if closeout.closeout_id not in self._rows:
            raise EngagementCloseoutNotFoundError(closeout.closeout_id)
        self._rows[closeout.closeout_id] = closeout

    async def get_for_engagement(self, engagement_id: str) -> EngagementCloseout | None:
        for closeout in self._rows.values():
            if closeout.engagement_id == engagement_id:
                return closeout
        return None
