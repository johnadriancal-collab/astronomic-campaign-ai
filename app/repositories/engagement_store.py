"""
Storage abstraction for Engagement (Client CRM Stage 1A, 2026-09-07),
extended in Stage 1H-A (2026-09-09) with get_by_luma_event_id() and the
at-most-one-Engagement-per-luma_event_id invariant (the approved product
decision for the Luma <-> Engagement link: one Luma event links to at most
one Engagement).

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


class EngagementLumaEventAlreadyLinkedError(Exception):
    """Raised by create()/save() when the write would leave two Engagements
    linked to the same non-null luma_event_id -- Stage 1H-A's approved
    product decision, enforced by a real SQLite partial unique index (see
    sqlite_engagement_store.py), not only a service-layer pre-check, so it
    holds even under concurrent requests -- same pattern as
    EngagementParticipantDuplicateError."""

    def __init__(self, luma_event_id: str):
        self.luma_event_id = luma_event_id
        super().__init__(f"luma_event_id {luma_event_id} is already linked to another Engagement.")


class EngagementStore(ABC):
    @abstractmethod
    async def create(self, engagement: Engagement) -> None:
        """Persist a newly-created Engagement. engagement_id is assumed
        unique (minted by the caller, e.g. uuid4). Raises
        EngagementLumaEventAlreadyLinkedError if `engagement.luma_event_id`
        is not None and already linked to a DIFFERENT Engagement."""

    @abstractmethod
    async def get(self, engagement_id: str) -> Engagement | None:
        """Returns the Engagement, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, engagement: Engagement) -> None:
        """Persist mutations to an existing Engagement, including
        archiving it or linking/unlinking its luma_event_id. Raises
        EngagementNotFoundError if engagement_id doesn't exist, or
        EngagementLumaEventAlreadyLinkedError if the new luma_event_id is
        already linked to a DIFFERENT Engagement."""

    @abstractmethod
    async def list_for_client(self, client_id: str) -> list[Engagement]:
        """Every Engagement for this Client (archived or not), ordered by
        created_at ascending -- the Client detail page's own Dinners/
        Engagements tab load. Sorting by `engagement_date` (rather than created_at)
        for display, or filtering by `status`, is the caller's job (see
        this store's own module docstring for why no additional index
        exists yet for those -- no concrete cross-client query needs
        them today)."""

    @abstractmethod
    async def get_by_luma_event_id(self, luma_event_id: str) -> Engagement | None:
        """Returns the Engagement currently linked to this Luma event, or
        None if none is -- Stage 1H-A's own lookup direction (Luma event ->
        Engagement), the one a future registration-sync stage will need."""

    @abstractmethod
    async def list_for_clients(self, client_ids: list[str]) -> list[Engagement]:
        """Every Engagement (archived or not, any status) belonging to ANY
        of these Client IDs, in one bulk call -- Stage 2C's own Next
        Dinner derivation for the Master Client CRM table, added
        specifically so ClientCrmService.list_clients() can compute a
        derived summary column for a whole page of Clients with exactly
        one query, never one list_for_client() call per Client (that
        would be the exact N+1 shape this method exists to avoid). Order
        is unspecified -- grouping by client_id and any further
        filtering/sorting (by engagement_date, status, etc.) is the
        caller's job, same "store returns everything relevant, caller
        decides what's shown" convention as list_for_client(). Returns an
        empty list for an empty `client_ids` (never a malformed query)."""

    @abstractmethod
    async def list_by_ids(self, engagement_ids: list[str]) -> list[Engagement]:
        """Every Engagement (archived or not, any status) whose
        engagement_id is in `engagement_ids`, in one bulk call --
        Contacts CRM Stage 3A's own Event History derivation, added so
        ClientCrmService.list_contact_event_history() can resolve every
        Engagement a Contact's EngagementParticipant rows reference with
        exactly one query, never one get() call per participant. An id
        with no matching Engagement is simply absent from the result
        (never an error) -- the caller treats that as a missing/malformed
        reference and skips it, same "fail safely, never crash the whole
        read" convention as this stage's own approved design. Returns an
        empty list for an empty `engagement_ids` (never a malformed
        query)."""


class MemoryEngagementStore(EngagementStore):
    """Dict-backed, keyed by engagement_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, Engagement] = {}

    def _linked_elsewhere(self, luma_event_id: str | None, exclude_engagement_id: str | None) -> bool:
        if luma_event_id is None:
            return False
        return any(
            e.luma_event_id == luma_event_id and e.engagement_id != exclude_engagement_id for e in self._rows.values()
        )

    async def create(self, engagement: Engagement) -> None:
        if self._linked_elsewhere(engagement.luma_event_id, exclude_engagement_id=None):
            raise EngagementLumaEventAlreadyLinkedError(engagement.luma_event_id)
        self._rows[engagement.engagement_id] = engagement

    async def get(self, engagement_id: str) -> Engagement | None:
        return self._rows.get(engagement_id)

    async def save(self, engagement: Engagement) -> None:
        if engagement.engagement_id not in self._rows:
            raise EngagementNotFoundError(engagement.engagement_id)
        if self._linked_elsewhere(engagement.luma_event_id, exclude_engagement_id=engagement.engagement_id):
            raise EngagementLumaEventAlreadyLinkedError(engagement.luma_event_id)
        self._rows[engagement.engagement_id] = engagement

    async def list_for_client(self, client_id: str) -> list[Engagement]:
        rows = [e for e in self._rows.values() if e.client_id == client_id]
        return sorted(rows, key=lambda e: e.created_at)

    async def get_by_luma_event_id(self, luma_event_id: str) -> Engagement | None:
        for e in self._rows.values():
            if e.luma_event_id == luma_event_id:
                return e
        return None

    async def list_for_clients(self, client_ids: list[str]) -> list[Engagement]:
        if not client_ids:
            return []
        ids = set(client_ids)
        return [e for e in self._rows.values() if e.client_id in ids]

    async def list_by_ids(self, engagement_ids: list[str]) -> list[Engagement]:
        if not engagement_ids:
            return []
        return [self._rows[eid] for eid in set(engagement_ids) if eid in self._rows]
