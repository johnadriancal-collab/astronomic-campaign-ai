"""
Storage abstraction for ClientTouchpoint (Client CRM Stage 2A, 2026-09-11).

No `delete()` -- same approved archive/soft-delete-only design as
ClientStore (see that module's own docstring); removal is always
"archived=True, then save()".
"""

from abc import ABC, abstractmethod

from app.models.client_crm import ClientTouchpoint


class ClientTouchpointNotFoundError(Exception):
    def __init__(self, touchpoint_id: str):
        self.touchpoint_id = touchpoint_id
        super().__init__(f"ClientTouchpoint not found: {touchpoint_id}")


class ClientTouchpointStore(ABC):
    @abstractmethod
    async def create(self, touchpoint: ClientTouchpoint) -> None:
        """Persist a newly-created ClientTouchpoint. touchpoint_id is
        assumed unique (minted by the caller, e.g. uuid4)."""

    @abstractmethod
    async def get(self, touchpoint_id: str) -> ClientTouchpoint | None:
        """Returns the ClientTouchpoint, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, touchpoint: ClientTouchpoint) -> None:
        """Persist mutations to an existing ClientTouchpoint, including
        archiving/restoring it. Raises ClientTouchpointNotFoundError if
        touchpoint_id doesn't exist."""

    @abstractmethod
    async def list_for_client(self, client_id: str) -> list[ClientTouchpoint]:
        """Every ClientTouchpoint for this Client (archived or not),
        ordered newest-first: occurred_at DESC, created_at DESC,
        touchpoint_id DESC -- a fully deterministic tie-break chain (Stage
        2A's own approved ordering), never left to whatever order the
        underlying store happens to return rows in. Filtering archived
        rows out (the API's default view) is the caller's job -- same
        "store returns everything, caller/service decides what's shown"
        convention as every other Client CRM list_for_x()."""


def touchpoint_sort_key(touchpoint: ClientTouchpoint) -> tuple:
    return (touchpoint.occurred_at, touchpoint.created_at, touchpoint.touchpoint_id)


class MemoryClientTouchpointStore(ClientTouchpointStore):
    """Dict-backed, keyed by touchpoint_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, ClientTouchpoint] = {}

    async def create(self, touchpoint: ClientTouchpoint) -> None:
        self._rows[touchpoint.touchpoint_id] = touchpoint

    async def get(self, touchpoint_id: str) -> ClientTouchpoint | None:
        return self._rows.get(touchpoint_id)

    async def save(self, touchpoint: ClientTouchpoint) -> None:
        if touchpoint.touchpoint_id not in self._rows:
            raise ClientTouchpointNotFoundError(touchpoint.touchpoint_id)
        self._rows[touchpoint.touchpoint_id] = touchpoint

    async def list_for_client(self, client_id: str) -> list[ClientTouchpoint]:
        rows = [t for t in self._rows.values() if t.client_id == client_id]
        return sorted(rows, key=touchpoint_sort_key, reverse=True)
