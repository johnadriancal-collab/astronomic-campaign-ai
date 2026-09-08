"""
ClientCrmService -- Client CRM Stage 1B (2026-09-07), extended for
ClientContact (Stage 1D) and Engagement (Stage 1E, 2026-09-07).
ClientNote remains completely inert: its Stage 1A store exists, but
nothing constructs or wires it into live app state yet, and this service
never touches it.

Engagement (Stage 1E) is historical/commercial delivery data ONLY --
creating, updating, or archiving/restoring an Engagement NEVER writes to
Client (relationship_classification/next_action/owner), ClientContact,
CrmContact, or anything Luma-related. The only side effect of any
Engagement write is its own Activity Log event, same as every other
Client CRM entity. This is a deliberate boundary, not an oversight: see
this stage's own STOP report for the "historical vs. current Client
state" architecture this preserves for a future ClientFollowUpRecord.

Archive/restore is deliberately NOT a separate method -- both are just
update_client(id, {"archived": True/False}) through the same partial-PATCH
path, mirroring CrmService.archive_contact()/update_contact() exactly
(see _record_update_activity's own docstring for why diffing the archived
flag across the save is what distinguishes the three resulting Activity
Log event types, with no dedicated archive/restore method needed). This
was chosen over MailCampaignService's dedicated action-endpoint style
(POST .../activate, .../pause, etc.) because Client's `archived` is a
simple boolean flag structurally identical to CrmContact.archived, not a
rich multi-state lifecycle like MailCampaign.status -- the simpler
existing precedent is the correct fit here, not the more elaborate one.

Duplicate-name handling: Stage 1B does NOT check for or block duplicate
Client names in any way (see this stage's own investigation/STOP report).
Astronomic has no company/account dedup engine yet, and inventing one
here -- even a "just warn" version -- would mean designing a new response
shape (a plain Client create can't also carry a non-blocking warning
without one) before Client/company dedup has been properly designed as
its own concern. `name` is NOT unique at the store/DB level. The
Clients-list search this service already provides (see list_clients())
is the intended way a human checks for an existing Client by name before
creating a new one -- a frontend UX concern for a later stage, not a
backend rule enforced here.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.client_crm import (
    Client,
    ClientContact,
    ClientPage,
    ClientRelationshipClassification,
    ClientStatus,
    Engagement,
    EngagementCloseout,
    EngagementParticipant,
    ParticipantSource,
)
from app.repositories.client_contact_store import ClientContactStore
from app.repositories.client_store import ClientStore
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.engagement_closeout_store import EngagementCloseoutStore
from app.repositories.engagement_participant_store import (
    EngagementParticipantDuplicateError,
    EngagementParticipantStore,
)
from app.repositories.engagement_store import EngagementStore
from app.services.activity_log_service import ActivityLogService

SORTABLE_CLIENT_FIELDS = frozenset({"name", "created_at", "updated_at", "next_action_due"})

# Server-owned fields -- stripped from any incoming create/update payload
# BEFORE it's applied, regardless of what the caller sent. The API
# layer's own ClientCreateRequest/ClientUpdateRequest already have no
# fields for these (so a real HTTP caller can never send them at all),
# but this is enforced again here, defensively, so "the server owns
# these fields" is actually true of this service's own contract -- not
# merely true by accident of which Pydantic model happens to sit in
# front of it today.
_SERVER_OWNED_FIELDS = frozenset({"client_id", "created_at", "updated_at"})


def _strip_server_owned_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in _SERVER_OWNED_FIELDS}


class ClientNotFound(Exception):
    def __init__(self, client_id: str):
        self.client_id = client_id
        super().__init__(f"Client not found: {client_id}")


class ClientContactNotFound(Exception):
    """Raised for a client_contact_id that doesn't exist, OR that exists
    but belongs to a different client_id than the one in the URL -- the
    two cases are deliberately indistinguishable to the caller (same 404),
    so this never leaks whether a given client_contact_id exists at all
    under some OTHER Client."""

    def __init__(self, client_contact_id: str):
        self.client_contact_id = client_contact_id
        super().__init__(f"ClientContact not found: {client_contact_id}")


class EngagementNotFound(Exception):
    """Raised for an engagement_id that doesn't exist, OR that exists but
    belongs to a different client_id than the one in the URL -- same
    deliberately-indistinguishable-404 rule as ClientContactNotFound, so
    an Engagement requested through Client A can never expose (or be
    mutated through) Client B's URL."""

    def __init__(self, engagement_id: str):
        self.engagement_id = engagement_id
        super().__init__(f"Engagement not found: {engagement_id}")


class EngagementCloseoutNotFound(Exception):
    """Raised when an Engagement has no EngagementCloseout yet -- a
    normal, expected state (not an error the caller did anything wrong
    to reach), mapped to a plain 404 at the API layer so the frontend can
    render its own "no closeout recorded yet" empty state rather than a
    generic error."""

    def __init__(self, engagement_id: str):
        self.engagement_id = engagement_id
        super().__init__(f"EngagementCloseout not found for Engagement: {engagement_id}")


class EngagementCloseoutAlreadyExists(Exception):
    """Raised by create_engagement_closeout() when this Engagement
    already has one (archived or not) -- at most ONE EngagementCloseout
    is ever created per Engagement (see EngagementCloseout's own
    docstring); callers must PATCH the existing one instead."""

    def __init__(self, engagement_id: str):
        self.engagement_id = engagement_id
        super().__init__(f"Engagement {engagement_id} already has a Closeout.")


class EngagementParticipantNotFound(Exception):
    """Raised for a participant_id that doesn't exist, OR that exists but
    belongs to a different engagement_id than the one in the URL -- same
    deliberately-indistinguishable-404 rule as EngagementNotFound."""

    def __init__(self, participant_id: str):
        self.participant_id = participant_id
        super().__init__(f"EngagementParticipant not found: {participant_id}")


class EngagementParticipantDuplicate(Exception):
    """Raised when a create or link/relink would leave two ACTIVE
    EngagementParticipant rows for the same (engagement_id,
    crm_contact_id) -- mapped to a clean 409 at the API layer rather than
    a raw SQLite integrity error ever reaching a caller."""

    def __init__(self, engagement_id: str, crm_contact_id: str):
        self.engagement_id = engagement_id
        self.crm_contact_id = crm_contact_id
        super().__init__(f"crm_contact_id {crm_contact_id} is already an active participant of Engagement {engagement_id}.")


class ClientCrmService:
    def __init__(
        self,
        *,
        client_store: ClientStore,
        activity_log: ActivityLogService,
        client_contact_store: ClientContactStore,
        crm_contact_store: CrmContactStore,
        engagement_store: EngagementStore,
        engagement_closeout_store: EngagementCloseoutStore,
        engagement_participant_store: EngagementParticipantStore,
    ):
        self.client_store = client_store
        self.activity_log = activity_log
        self.client_contact_store = client_contact_store
        self.crm_contact_store = crm_contact_store
        self.engagement_store = engagement_store
        self.engagement_closeout_store = engagement_closeout_store
        self.engagement_participant_store = engagement_participant_store

    async def _require_client(self, client_id: str) -> Client:
        client = await self.client_store.get(client_id)
        if client is None:
            raise ClientNotFound(client_id)
        return client

    async def _require_client_contact(self, client_id: str, client_contact_id: str) -> ClientContact:
        contact = await self.client_contact_store.get(client_contact_id)
        if contact is None or contact.client_id != client_id:
            raise ClientContactNotFound(client_contact_id)
        return contact

    async def _require_engagement(self, client_id: str, engagement_id: str) -> Engagement:
        engagement = await self.engagement_store.get(engagement_id)
        if engagement is None or engagement.client_id != client_id:
            raise EngagementNotFound(engagement_id)
        return engagement

    async def _require_engagement_participant(self, engagement_id: str, participant_id: str) -> EngagementParticipant:
        participant = await self.engagement_participant_store.get(participant_id)
        if participant is None or participant.engagement_id != engagement_id:
            raise EngagementParticipantNotFound(participant_id)
        return participant

    async def create_client(self, fields: dict[str, Any]) -> Client:
        """`fields` is assumed already validated/shaped by the API layer's
        own ClientCreateRequest (enum values, field types) -- this method
        only re-validates the one thing a typed request model can't fully
        enforce by itself: that `name`, after stripping whitespace, is
        actually non-blank. `client_id`/`created_at`/`updated_at` are
        always stripped from `fields` first (see _strip_server_owned_fields)
        and set explicitly below -- a caller can never smuggle its own
        value in for any of them, even by bypassing the API layer."""
        fields = _strip_server_owned_fields(fields)
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValueError("Client name is required.")
        now = datetime.now(timezone.utc)
        client = Client(client_id=str(uuid.uuid4()), created_at=now, updated_at=now, **{**fields, "name": name})
        await self.client_store.create(client)
        await self.activity_log.record(
            event_type="client.created",
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'Client "{client.name}" was created.',
            entity_type="client",
            entity_id=client.client_id,
            entity_name=client.name,
        )
        return client

    async def get_client(self, client_id: str) -> Client:
        return await self._require_client(client_id)

    async def update_client(self, client_id: str, patch: dict[str, Any]) -> Client:
        """Direct partial update -- every remaining key in `patch` is set
        as given (matching CrmService.update_contact()'s own "no merge
        rule" base case; Client has no dict-shaped field like
        custom_fields that would need shallow-merge protection).
        `client_id`/`created_at` are stripped from `patch` first (see
        _strip_server_owned_fields) -- the API layer's ClientUpdateRequest
        already has no fields for them, but this is enforced again here
        so it holds even for a direct service-layer call; `updated_at` is
        always server-set to now, ignoring anything the caller sent."""
        client = await self._require_client(client_id)
        patch = _strip_server_owned_fields(patch)
        if "name" in patch:
            stripped = (patch["name"] or "").strip()
            if not stripped:
                raise ValueError("Client name cannot be blank.")
            patch = {**patch, "name": stripped}
        updated = client.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        await self.client_store.save(updated)
        await self._record_update_activity(client, updated)
        return updated

    async def _record_update_activity(self, before: Client, after: Client) -> None:
        """Distinguishes archive/restore from a plain edit by diffing the
        `archived` flag across the save -- there is no separate
        archive_client()/restore_client() method, so this is the one place
        that transition is detected, exactly mirroring
        CrmService._record_contact_update_activity()."""
        if before.archived == after.archived:
            event_type, verb = "client.updated", "updated"
        elif after.archived:
            event_type, verb = "client.archived", "archived"
        else:
            event_type, verb = "client.restored", "restored"
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'Client "{after.name}" was {verb}.',
            entity_type="client",
            entity_id=after.client_id,
            entity_name=after.name,
        )

    async def list_clients(
        self,
        q: str | None = None,
        status: ClientStatus | None = None,
        relationship_classification: ClientRelationshipClassification | None = None,
        owner: str | None = None,
        include_archived: bool = False,
        sort_by: str = "name",
        sort_dir: str = "asc",
        page: int = 1,
        page_size: int = 50,
    ) -> ClientPage:
        """Filtering, sorting, AND pagination all happen here, in the
        service layer -- the caller only ever receives the one page it
        asked for plus a total count. Filtering/sorting is a Python scan
        over store.list() (same convention as CrmService.list_contacts()/
        ActivityLogService.list_events() -- fine at the scale Client CRM
        is expected to run at; would need real SQL WHERE/ORDER BY if this
        ever grows to tens of thousands of rows, a ceiling independent of
        pagination itself). This intentionally does NOT reproduce the
        CRM's generic FilterQuery/FilterCondition engine -- Client's field
        set is small and stable (unlike CrmContact's core+thesis+custom-
        fields shape that engine exists to handle generically), so a
        simple, explicitly-typed parameter set is a better fit and a much
        smaller surface for Stage 1C's Clients table to build against."""
        if sort_by not in SORTABLE_CLIENT_FIELDS:
            raise ValueError(f"sort_by must be one of {sorted(SORTABLE_CLIENT_FIELDS)}.")
        if sort_dir not in ("asc", "desc"):
            raise ValueError("sort_dir must be 'asc' or 'desc'.")

        clients = await self.client_store.list()
        if not include_archived:
            clients = [c for c in clients if not c.archived]

        def matches(c: Client) -> bool:
            if status is not None and c.status != status:
                return False
            if relationship_classification is not None and c.relationship_classification != relationship_classification:
                return False
            if owner and (c.owner or "").lower() != owner.lower():
                return False
            if q and q.lower() not in c.name.lower():
                return False
            return True

        filtered = [c for c in clients if matches(c)]

        # None-valued sort keys (e.g. next_action_due unset) always sort
        # LAST, regardless of asc/desc -- reversing a (is_none, value)
        # tuple as a whole would instead put them FIRST on sort_dir="desc",
        # which reads as "these need attention most" when it should mean
        # "nothing scheduled at all". Splitting the two groups keeps that
        # correct in both directions.
        with_value = [c for c in filtered if getattr(c, sort_by) is not None]
        without_value = [c for c in filtered if getattr(c, sort_by) is None]
        with_value.sort(key=lambda c: getattr(c, sort_by), reverse=(sort_dir == "desc"))
        filtered = with_value + without_value

        total = len(filtered)
        page = max(page, 1)
        page_size = max(page_size, 1)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]
        return ClientPage(items=items, total=total, page=page, page_size=page_size)

    # =====================================================================
    # ClientContact -- Client CRM Stage 1D (2026-09-07). See this stage's
    # own STOP report for the full investigation this design comes from.
    # =====================================================================

    async def list_client_contacts(self, client_id: str) -> list[ClientContact]:
        await self._require_client(client_id)
        return await self.client_contact_store.list_for_client(client_id)

    async def create_client_contact(self, client_id: str, fields: dict[str, Any]) -> ClientContact:
        """V1 requires `crm_contact_id` -- there is no free-text-only
        person-creation path here (see this stage's own STOP report,
        item 8: reusing the existing Contact-creation form inline was
        investigated and deliberately deferred). Snapshot fields
        (first_name/last_name/email/phone) are populated FROM the
        canonical CrmContact at creation time, using only whatever data
        it actually has -- never fabricated -- and are never written back
        to CrmContact (no two-way sync in Stage 1D; see ClientContact's
        own model docstring for why the snapshot exists at all).
        `title`/`is_decision_maker`/`role_notes` are relationship-specific
        and come from the request only -- CrmContact.title is a different
        concept (this person's general job title) and is never copied
        into ClientContact.title (this person's role AT THIS CLIENT)."""
        await self._require_client(client_id)

        crm_contact_id = (fields.get("crm_contact_id") or "").strip()
        if not crm_contact_id:
            raise ValueError("crm_contact_id is required to link a Primary/additional Contact.")
        crm_contact = await self.crm_contact_store.get(crm_contact_id)
        if crm_contact is None:
            raise ValueError("crm_contact_id does not refer to an existing Contact.")
        if crm_contact.archived:
            raise ValueError("Cannot link an archived Contact.")

        now = datetime.now(timezone.utc)
        contact = ClientContact(
            client_contact_id=str(uuid.uuid4()),
            client_id=client_id,
            crm_contact_id=crm_contact_id,
            first_name=crm_contact.first_name,
            last_name=crm_contact.last_name,
            email=crm_contact.email,
            phone=crm_contact.phone,
            title=fields.get("title"),
            is_primary_contact=bool(fields.get("is_primary_contact", False)),
            is_decision_maker=bool(fields.get("is_decision_maker", False)),
            role_notes=fields.get("role_notes"),
            created_at=now,
            updated_at=now,
        )
        if contact.is_primary_contact:
            await self.client_contact_store.create_as_primary(contact)
        else:
            await self.client_contact_store.create(contact)

        display_name = " ".join(part for part in (contact.first_name, contact.last_name) if part) or "Unnamed contact"
        await self.activity_log.record(
            event_type="client_contact.created",
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'"{display_name}" was linked as a Contact.',
            entity_type="client_contact",
            entity_id=contact.client_contact_id,
            entity_name=display_name,
            metadata={"client_id": client_id, "crm_contact_id": crm_contact_id},
        )
        return contact

    async def update_client_contact(self, client_id: str, client_contact_id: str, patch: dict[str, Any]) -> ClientContact:
        """Genuine partial update. crm_contact_id/first_name/last_name/
        email/phone have no fields in ClientContactUpdateRequest at all
        (re-linking to a different person isn't supported in V1 -- archive
        and re-add instead), so `patch` only ever carries title/
        is_primary_contact/is_decision_maker/role_notes/archived.

        Ordering matters here: is_primary_contact=True is always applied
        via the store's atomic set_primary() (which also clears every
        other active primary for this Client in the same transaction),
        never via a plain field assignment -- see ClientContactStore.
        set_primary()'s own docstring for why. An archived ClientContact
        can never remain or become Primary (this stage's own explicit
        rule): archiving a currently-primary contact clears
        is_primary_contact in the same save(); requesting
        is_primary_contact=True together with archived=True (or against
        an already-archived contact) is rejected outright."""
        existing = await self._require_client_contact(client_id, client_contact_id)

        wants_primary = patch.get("is_primary_contact") is True
        archiving = patch.get("archived") is True and not existing.archived
        if wants_primary and (existing.archived or archiving):
            raise ValueError("Cannot make an archived Contact relationship Primary.")

        # is_primary_contact=True always routes through the atomic
        # set_primary() path below instead, so it's excluded here. An
        # explicit is_primary_contact=False (voluntary un-primary, or the
        # archiving-clears-primary case below) is a plain field like any
        # other and stays in other_fields.
        other_fields = {k: v for k, v in patch.items() if not (k == "is_primary_contact" and v is True)}
        now = datetime.now(timezone.utc)

        if archiving and existing.is_primary_contact:
            other_fields = {**other_fields, "is_primary_contact": False}

        merged = existing.model_copy(update={**other_fields, "updated_at": now})

        if wants_primary and not archiving:
            # Atomically flips THIS row to primary and clears every other
            # active primary for the Client -- must happen before (or
            # instead of) a plain save() of the other field changes, so
            # there is never a moment with two primaries.
            merged = await self.client_contact_store.set_primary(client_id, client_contact_id)
            remaining_fields = {k: v for k, v in other_fields.items() if k != "is_primary_contact"}
            if remaining_fields:
                merged = merged.model_copy(update={**remaining_fields, "updated_at": now})
                await self.client_contact_store.save(merged)
        else:
            await self.client_contact_store.save(merged)

        await self._record_client_contact_update_activity(existing, merged)
        return merged

    async def _record_client_contact_update_activity(self, before: ClientContact, after: ClientContact) -> None:
        display_name = " ".join(part for part in (after.first_name, after.last_name) if part) or "Unnamed contact"
        if before.archived != after.archived:
            event_type, verb = ("client_contact.archived", "archived") if after.archived else ("client_contact.restored", "restored")
        elif not before.is_primary_contact and after.is_primary_contact:
            event_type, verb = "client_contact.primary_changed", "set as Primary Contact"
        else:
            event_type, verb = "client_contact.updated", "updated"
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'"{display_name}" was {verb}.',
            entity_type="client_contact",
            entity_id=after.client_contact_id,
            entity_name=display_name,
            metadata={"client_id": after.client_id},
        )

    # =====================================================================
    # Engagement -- Client CRM Stage 1E (2026-09-07). Historical/commercial
    # delivery data ONLY -- see this module's own docstring for the "never
    # a side-effect source for Client/ClientContact/CrmContact/Luma" rule.
    # =====================================================================

    @classmethod
    def _normalize_dinner_type(cls, engagement_type: Any, dinner_type: Any) -> Any:
        """Backend stays authoritative regardless of what the frontend
        sends or fails to clear: `dinner_type` is only ever meaningful when
        `engagement_type == DINNER` -- anything else (SPONSORSHIP/OTHER)
        always gets it silently forced to None on write, never rejected
        with an error."""
        value = engagement_type.value if hasattr(engagement_type, "value") else engagement_type
        if value != "dinner":
            return None
        return dinner_type

    async def list_client_engagements(self, client_id: str) -> list[Engagement]:
        await self._require_client(client_id)
        return await self.engagement_store.list_for_client(client_id)

    async def create_client_engagement(self, client_id: str, fields: dict[str, Any]) -> Engagement:
        """`fields` is assumed already validated/shaped by the API layer's
        own EngagementCreateRequest (enum values, field types). No
        canonical-record linking happens here (unlike ClientContact) --
        every field is either user-supplied or server-defaulted."""
        await self._require_client(client_id)
        fields = _strip_server_owned_fields(fields)
        title = (fields.get("title") or "").strip()
        if not title:
            raise ValueError("Engagement title is required.")

        now = datetime.now(timezone.utc)
        engagement = Engagement(
            engagement_id=str(uuid.uuid4()),
            client_id=client_id,
            created_at=now,
            updated_at=now,
            **{**fields, "title": title},
        )
        engagement = engagement.model_copy(
            update={"dinner_type": self._normalize_dinner_type(engagement.engagement_type, engagement.dinner_type)}
        )
        await self.engagement_store.create(engagement)
        await self.activity_log.record(
            event_type="engagement.created",
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'Engagement "{engagement.title}" was created.',
            entity_type="engagement",
            entity_id=engagement.engagement_id,
            entity_name=engagement.title,
            metadata={"client_id": client_id},
        )
        return engagement

    async def get_client_engagement(self, client_id: str, engagement_id: str) -> Engagement:
        return await self._require_engagement(client_id, engagement_id)

    async def update_client_engagement(self, client_id: str, engagement_id: str, patch: dict[str, Any]) -> Engagement:
        """Direct partial update -- matches update_client()'s own "no merge
        rule" base case. `client_id`/`created_at` are stripped from
        `patch` first; `updated_at` is always server-set to now. Archive/
        restore is not a separate method -- both are just
        update_client_engagement(id, {"archived": True/False}), same
        convention as Client/ClientContact."""
        engagement = await self._require_engagement(client_id, engagement_id)
        patch = _strip_server_owned_fields(patch)
        if "title" in patch:
            stripped = (patch["title"] or "").strip()
            if not stripped:
                raise ValueError("Engagement title cannot be blank.")
            patch = {**patch, "title": stripped}

        updated = engagement.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        updated = updated.model_copy(
            update={"dinner_type": self._normalize_dinner_type(updated.engagement_type, updated.dinner_type)}
        )
        await self.engagement_store.save(updated)
        await self._record_engagement_update_activity(engagement, updated)
        return updated

    async def _record_engagement_update_activity(self, before: Engagement, after: Engagement) -> None:
        """Distinguishes archive/restore from a plain edit by diffing the
        `archived` flag across the save -- same convention as Client's own
        _record_update_activity()."""
        if before.archived == after.archived:
            event_type, verb = "engagement.updated", "updated"
        elif after.archived:
            event_type, verb = "engagement.archived", "archived"
        else:
            event_type, verb = "engagement.restored", "restored"
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'Engagement "{after.title}" was {verb}.',
            entity_type="engagement",
            entity_id=after.engagement_id,
            entity_name=after.title,
            metadata={"client_id": after.client_id},
        )

    # =====================================================================
    # EngagementCloseout -- Client CRM Stage 1F (2026-09-08). The same-day
    # factual/qualitative baseline for one Engagement -- see
    # EngagementCloseout's own model docstring for the full architecture
    # boundary (own entity, never Engagement fields, never a structured
    # ClientNote; at most one per Engagement, enforced here, not at the
    # store layer; turnout counts are Stage 1F's own manual snapshot, NOT
    # derived from any per-person record -- Stage 1G, not built yet, will
    # decide separately whether/how that changes).
    # =====================================================================

    _CLOSEOUT_COUNT_FIELDS = (
        "confirmed_guest_count",
        "attended_count",
        "no_show_count",
        "cancelled_count",
        "unexpected_attendee_count",
    )

    @classmethod
    def _validate_non_negative_counts(cls, fields: dict[str, Any]) -> None:
        """Defense in depth: the API layer's own request models already
        reject a negative count with a 422 (Field(ge=0)), and constructing
        a fresh EngagementCloseout already enforces the same constraint --
        but update_engagement_closeout() applies a patch via model_copy(),
        which does NOT re-run field validators. This is the one path that
        needs an explicit check to give the same guarantee to a direct
        service-layer caller as an HTTP caller already gets for free."""
        for field in cls._CLOSEOUT_COUNT_FIELDS:
            value = fields.get(field)
            if value is not None and value < 0:
                raise ValueError(f"{field} cannot be negative.")

    async def _require_engagement_closeout(self, client_id: str, engagement_id: str) -> EngagementCloseout:
        await self._require_engagement(client_id, engagement_id)
        closeout = await self.engagement_closeout_store.get_for_engagement(engagement_id)
        if closeout is None:
            raise EngagementCloseoutNotFound(engagement_id)
        return closeout

    async def get_engagement_closeout(self, client_id: str, engagement_id: str) -> EngagementCloseout:
        """Raises EngagementCloseoutNotFound (-> 404) when none has been
        recorded yet -- a normal, expected state the frontend renders as
        its own "no closeout recorded yet" empty state, not a generic
        error. Returns the closeout regardless of archived state (same
        convention as Engagement/Client themselves) -- the caller decides
        how to render an archived one."""
        return await self._require_engagement_closeout(client_id, engagement_id)

    async def create_engagement_closeout(self, client_id: str, engagement_id: str, fields: dict[str, Any]) -> EngagementCloseout:
        """At most ONE EngagementCloseout is ever created per Engagement --
        raises EngagementCloseoutAlreadyExists (-> 409) if one already
        exists, archived or not; callers must PATCH the existing one
        instead of creating a second."""
        await self._require_engagement(client_id, engagement_id)
        existing = await self.engagement_closeout_store.get_for_engagement(engagement_id)
        if existing is not None:
            raise EngagementCloseoutAlreadyExists(engagement_id)

        self._validate_non_negative_counts(fields)
        now = datetime.now(timezone.utc)
        closeout = EngagementCloseout(
            closeout_id=str(uuid.uuid4()),
            engagement_id=engagement_id,
            client_id=client_id,
            created_at=now,
            updated_at=now,
            **fields,
        )
        await self.engagement_closeout_store.create(closeout)
        await self.activity_log.record(
            event_type="engagement_closeout.created",
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary="An Engagement Closeout was recorded.",
            entity_type="engagement_closeout",
            entity_id=closeout.closeout_id,
            entity_name=None,
            # Deliberately NOT the qualitative text fields or turnout counts
            # -- see this stage's own explicit Activity Log privacy rule.
            metadata={"client_id": client_id, "engagement_id": engagement_id},
        )
        return closeout

    async def update_engagement_closeout(self, client_id: str, engagement_id: str, patch: dict[str, Any]) -> EngagementCloseout:
        """Direct partial update -- same "no merge rule" base case as
        update_client()/update_client_engagement(). Archive/restore is not
        a separate method -- both are just update_engagement_closeout(...,
        {"archived": True/False}), same convention as every other Client
        CRM entity."""
        closeout = await self._require_engagement_closeout(client_id, engagement_id)
        self._validate_non_negative_counts(patch)

        updated = closeout.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        await self.engagement_closeout_store.save(updated)
        await self._record_engagement_closeout_update_activity(closeout, updated)
        return updated

    async def _record_engagement_closeout_update_activity(self, before: EngagementCloseout, after: EngagementCloseout) -> None:
        if before.archived == after.archived:
            event_type, verb = "engagement_closeout.updated", "updated"
        elif after.archived:
            event_type, verb = "engagement_closeout.archived", "archived"
        else:
            event_type, verb = "engagement_closeout.restored", "restored"
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f"An Engagement Closeout was {verb}.",
            entity_type="engagement_closeout",
            entity_id=after.closeout_id,
            entity_name=None,
            metadata={"client_id": after.client_id, "engagement_id": after.engagement_id},
        )

    # =====================================================================
    # EngagementParticipant -- Client CRM Stage 1G (2026-09-08). The
    # person-level relationship between a canonical CrmContact and one
    # Engagement -- see EngagementParticipant's own model docstring for
    # the full architecture (optional crm_contact_id, snapshot fields,
    # two-axis RSVP/attendance status, is_walk_in as provenance not
    # attendance, at-most-one-active-per-Contact-per-Engagement enforced
    # by a real SQLite partial unique index). `source` is entirely
    # server-owned here -- Stage 1G creates and updates ONLY MANUAL
    # participant records; LUMA is a reserved enum value this service
    # never reads, writes, or exposes as a caller-settable option.
    # =====================================================================

    @staticmethod
    def _participant_identity_is_meaningful(first_name: str | None, last_name: str | None, email: str | None) -> bool:
        """The Stage 1G validation rule for an UNRESOLVED participant
        (no crm_contact_id): at least one of first_name/last_name/email
        must be non-blank. Deliberately NOT "first_name specifically" --
        an email-only sign-in-sheet entry, or a surname-only historical
        record, are both legitimate incomplete-but-meaningful identities;
        a resolved participant (crm_contact_id set) never needs this
        check at all, since its snapshot always comes from a real Contact."""
        return bool(first_name or last_name or email)

    @staticmethod
    def _participant_display_name(participant: EngagementParticipant) -> str:
        """Never falls back to email -- see this stage's own Activity Log
        privacy rule (no contact information in Activity Log metadata,
        extended here to entity_name too, out of the same caution)."""
        name = " ".join(part for part in (participant.first_name, participant.last_name) if part)
        return name or "Unnamed participant"

    async def list_engagement_participants(self, client_id: str, engagement_id: str) -> list[EngagementParticipant]:
        await self._require_engagement(client_id, engagement_id)
        return await self.engagement_participant_store.list_for_engagement(engagement_id)

    async def create_engagement_participant(self, client_id: str, engagement_id: str, fields: dict[str, Any]) -> EngagementParticipant:
        """If `crm_contact_id` is provided, snapshot fields are populated
        FROM the canonical CrmContact (any name/email/etc. also present in
        `fields` is ignored -- the canonical record always wins, exactly
        like create_client_contact()) and never written back to it. If
        omitted, this creates an UNRESOLVED participant -- allowed, but
        only when _participant_identity_is_meaningful() holds; historical
        guest lists, walk-ins, and unmatched people are legitimate real
        cases Stage 1G must not force into a fake CrmContact."""
        await self._require_engagement(client_id, engagement_id)

        crm_contact_id = (fields.get("crm_contact_id") or "").strip() or None
        now = datetime.now(timezone.utc)

        if crm_contact_id:
            crm_contact = await self.crm_contact_store.get(crm_contact_id)
            if crm_contact is None:
                raise ValueError("crm_contact_id does not refer to an existing Contact.")
            if crm_contact.archived:
                raise ValueError("Cannot link an archived Contact.")
            first_name, last_name, email = crm_contact.first_name, crm_contact.last_name, crm_contact.email
            title, company = crm_contact.title, crm_contact.company
        else:
            first_name = (fields.get("first_name") or "").strip() or None
            last_name = (fields.get("last_name") or "").strip() or None
            email = (fields.get("email") or "").strip() or None
            title = (fields.get("title") or "").strip() or None
            company = (fields.get("company") or "").strip() or None
            if not self._participant_identity_is_meaningful(first_name, last_name, email):
                raise ValueError(
                    "An unresolved participant needs at least a name or email -- link an existing Contact "
                    "instead, or provide identifying information."
                )

        participant = EngagementParticipant(
            participant_id=str(uuid.uuid4()),
            engagement_id=engagement_id,
            client_id=client_id,
            crm_contact_id=crm_contact_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            title=title,
            company=company,
            role=fields.get("role") or "guest",
            rsvp_status=fields.get("rsvp_status"),
            attendance_status=fields.get("attendance_status"),
            is_walk_in=bool(fields.get("is_walk_in", False)),
            source=ParticipantSource.MANUAL,
            created_at=now,
            updated_at=now,
        )
        try:
            await self.engagement_participant_store.create(participant)
        except EngagementParticipantDuplicateError as exc:
            raise EngagementParticipantDuplicate(exc.engagement_id, exc.crm_contact_id) from exc

        display_name = self._participant_display_name(participant)
        await self.activity_log.record(
            event_type="engagement_participant.created",
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'"{display_name}" was added as a Participant.',
            entity_type="engagement_participant",
            entity_id=participant.participant_id,
            entity_name=display_name,
            metadata={"client_id": client_id, "engagement_id": engagement_id},
        )
        return participant

    async def update_engagement_participant(
        self, client_id: str, engagement_id: str, participant_id: str, patch: dict[str, Any]
    ) -> EngagementParticipant:
        """Genuine partial update. Explicitly SUPPORTS linking/relinking
        `crm_contact_id` (unlike ClientContact's own no-relink rule) --
        the "unresolved participant later matched to a real Contact"
        transition this stage's own approved design requires. Linking
        refreshes the snapshot FROM the newly-linked Contact (superseding
        any manually-entered values) and goes through the SAME duplicate
        protection as create() -- the store's own partial unique index
        rejects it if that crm_contact_id is already active on this
        Engagement, surfaced here as a clean EngagementParticipantDuplicate.

        Allowed identity transitions only: unresolved -> unresolved (edit
        the free-text snapshot fields), unresolved -> resolved (link),
        and resolved -> resolved (relink to a different Contact -- a
        correction operation for V1; no dedicated frontend workflow
        exists for it yet). resolved -> unresolved (clearing
        crm_contact_id back to null on an already-linked participant) is
        explicitly REJECTED with a ValueError -- once linked to a
        canonical Contact, a participant cannot be turned back into an
        unresolved one via this PATCH."""
        await self._require_engagement(client_id, engagement_id)
        participant = await self._require_engagement_participant(engagement_id, participant_id)

        merged_fields = dict(patch)
        if "crm_contact_id" in merged_fields:
            new_crm_contact_id = (merged_fields.get("crm_contact_id") or "").strip() or None
            if new_crm_contact_id is None and participant.crm_contact_id is not None:
                raise ValueError(
                    "This participant is already linked to a Contact -- it cannot be unlinked back to an "
                    "unresolved participant. Link it to a different Contact instead if this was a mistake."
                )
            if new_crm_contact_id and new_crm_contact_id != participant.crm_contact_id:
                crm_contact = await self.crm_contact_store.get(new_crm_contact_id)
                if crm_contact is None:
                    raise ValueError("crm_contact_id does not refer to an existing Contact.")
                if crm_contact.archived:
                    raise ValueError("Cannot link an archived Contact.")
                merged_fields["crm_contact_id"] = new_crm_contact_id
                merged_fields["first_name"] = crm_contact.first_name
                merged_fields["last_name"] = crm_contact.last_name
                merged_fields["email"] = crm_contact.email
                merged_fields["title"] = crm_contact.title
                merged_fields["company"] = crm_contact.company
            else:
                merged_fields["crm_contact_id"] = new_crm_contact_id

        updated = participant.model_copy(update={**merged_fields, "updated_at": datetime.now(timezone.utc)})

        if updated.crm_contact_id is None and not self._participant_identity_is_meaningful(
            updated.first_name, updated.last_name, updated.email
        ):
            raise ValueError(
                "An unresolved participant needs at least a name or email -- link an existing Contact instead, "
                "or provide identifying information."
            )

        try:
            await self.engagement_participant_store.save(updated)
        except EngagementParticipantDuplicateError as exc:
            raise EngagementParticipantDuplicate(exc.engagement_id, exc.crm_contact_id) from exc

        await self._record_engagement_participant_update_activity(participant, updated)
        return updated

    async def _record_engagement_participant_update_activity(
        self, before: EngagementParticipant, after: EngagementParticipant
    ) -> None:
        if before.archived == after.archived:
            event_type, verb = "engagement_participant.updated", "updated"
        elif after.archived:
            event_type, verb = "engagement_participant.archived", "archived"
        else:
            event_type, verb = "engagement_participant.restored", "restored"
        display_name = self._participant_display_name(after)
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.CLIENT_CRM,
            source=ActivitySource.MANUAL_CLIENT_CRM,
            summary=f'"{display_name}" was {verb}.',
            entity_type="engagement_participant",
            entity_id=after.participant_id,
            entity_name=display_name,
            metadata={"client_id": after.client_id, "engagement_id": after.engagement_id},
        )
