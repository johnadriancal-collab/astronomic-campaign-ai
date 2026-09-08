"""
Storage abstraction for EngagementParticipant (Client CRM Stage 1G, 2026-09-08).

No `delete()` -- same approved archive/soft-delete-only design as every
other Client CRM entity; removal is always "archived=True, then save()".

Unlike ClientContactStore's own "is_primary_contact is not enforced here"
precedent, THIS store DOES enforce its own invariant -- at most one
EngagementParticipant per (engagement_id, crm_contact_id) -- via a real
SQLite partial unique index in the SQLite implementation (see
sqlite_engagement_participant_store.py), not just an application-level
check, so it holds even under concurrent requests. `create()` and
`save()` both raise EngagementParticipantDuplicateError on a conflict
(create-time duplicate, or a PATCH that links/relinks an unresolved
participant's crm_contact_id to one already used in the same Engagement)
rather than letting a raw SQLite IntegrityError escape.
"""

from abc import ABC, abstractmethod

from app.models.client_crm import EngagementParticipant


class EngagementParticipantNotFoundError(Exception):
    def __init__(self, participant_id: str):
        self.participant_id = participant_id
        super().__init__(f"EngagementParticipant not found: {participant_id}")


class EngagementParticipantDuplicateError(Exception):
    """Raised by create()/save() when the write would leave two ACTIVE
    EngagementParticipant rows for the same (engagement_id, crm_contact_id)
    -- either a fresh duplicate create, or a PATCH that links/relinks an
    unresolved participant to a crm_contact_id already active on this
    Engagement."""

    def __init__(self, engagement_id: str, crm_contact_id: str):
        self.engagement_id = engagement_id
        self.crm_contact_id = crm_contact_id
        super().__init__(f"crm_contact_id {crm_contact_id} is already an active participant of Engagement {engagement_id}.")


class EngagementParticipantStore(ABC):
    @abstractmethod
    async def create(self, participant: EngagementParticipant) -> None:
        """Persist a newly-created EngagementParticipant. participant_id
        is assumed unique (minted by the caller, e.g. uuid4). Raises
        EngagementParticipantDuplicateError if `participant.crm_contact_id`
        is not None and already active for `participant.engagement_id`."""

    @abstractmethod
    async def get(self, participant_id: str) -> EngagementParticipant | None:
        """Returns the EngagementParticipant, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, participant: EngagementParticipant) -> None:
        """Persist mutations to an existing EngagementParticipant,
        including archiving it or linking/relinking its crm_contact_id.
        Raises EngagementParticipantNotFoundError if participant_id
        doesn't exist, or EngagementParticipantDuplicateError if the new
        crm_contact_id is already active for this Engagement on a
        DIFFERENT participant row."""

    @abstractmethod
    async def list_for_engagement(self, engagement_id: str) -> list[EngagementParticipant]:
        """Every EngagementParticipant for this Engagement (archived or
        not), ordered by created_at ascending -- the Engagement detail
        page's own Participants section load."""


class MemoryEngagementParticipantStore(EngagementParticipantStore):
    """Dict-backed, keyed by participant_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._rows: dict[str, EngagementParticipant] = {}

    def _active_duplicate(self, engagement_id: str, crm_contact_id: str | None, exclude_participant_id: str | None) -> bool:
        if crm_contact_id is None:
            return False
        return any(
            p.engagement_id == engagement_id
            and p.crm_contact_id == crm_contact_id
            and p.participant_id != exclude_participant_id
            for p in self._rows.values()
        )

    async def create(self, participant: EngagementParticipant) -> None:
        if self._active_duplicate(participant.engagement_id, participant.crm_contact_id, exclude_participant_id=None):
            raise EngagementParticipantDuplicateError(participant.engagement_id, participant.crm_contact_id)
        self._rows[participant.participant_id] = participant

    async def get(self, participant_id: str) -> EngagementParticipant | None:
        return self._rows.get(participant_id)

    async def save(self, participant: EngagementParticipant) -> None:
        if participant.participant_id not in self._rows:
            raise EngagementParticipantNotFoundError(participant.participant_id)
        if self._active_duplicate(
            participant.engagement_id, participant.crm_contact_id, exclude_participant_id=participant.participant_id
        ):
            raise EngagementParticipantDuplicateError(participant.engagement_id, participant.crm_contact_id)
        self._rows[participant.participant_id] = participant

    async def list_for_engagement(self, engagement_id: str) -> list[EngagementParticipant]:
        rows = [p for p in self._rows.values() if p.engagement_id == engagement_id]
        return sorted(rows, key=lambda p: p.created_at)
