"""
ClientCrmService -- Client CRM Stage 1B (2026-09-07). Client CRUD only --
ClientContact/Engagement/ClientNote remain completely inert: their Stage
1A stores exist, but nothing constructs or wires them into live app state
yet, and this service never touches them. Their own CRUD arrives in later
stages.

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
from app.models.client_crm import Client, ClientPage, ClientRelationshipClassification, ClientStatus
from app.repositories.client_store import ClientStore
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


class ClientCrmService:
    def __init__(self, *, client_store: ClientStore, activity_log: ActivityLogService):
        self.client_store = client_store
        self.activity_log = activity_log

    async def _require_client(self, client_id: str) -> Client:
        client = await self.client_store.get(client_id)
        if client is None:
            raise ClientNotFound(client_id)
        return client

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
