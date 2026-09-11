"""
Client CRM API -- Stage 1B (2026-09-07) Client CRUD, extended for
ClientContact (Stage 1D), Engagement (Stage 1E, 2026-09-07),
EngagementCloseout (Stage 1F, 2026-09-08), and EngagementParticipant
(Stage 1G, 2026-09-08). ClientNote has no routes yet (a later stage).

Deliberately its own prefix ("/client-crm", NOT "/crm") -- the existing
read-only service token's scope is hardcoded to any path starting with
"/crm/" (see session_auth_middleware.py's own docstring). Using a
different prefix means these routes are automatically outside that
token's existing grant, with zero risk of silently expanding it. The
existing operator token's own explicit method+path allowlist
(_SERVICE_OPERATOR_RULES in the same file) is untouched by this stage --
no rule added there, so that identity also has no access to these routes.
Normal Hub session-cookie auth applies with no extra wiring: this app's
middleware denies-by-default outside an explicit PUBLIC_PATHS allowlist,
and nothing here is added to it.

Typed Pydantic request models (ClientCreateRequest/ClientUpdateRequest)
here, NOT a raw `dict[str, Any]` body like POST /crm/contacts uses --
an intentional deviation from that precedent (see this stage's own STOP
report): Client has a small, stable, fully-known field set, unlike
CrmContact's much larger core+thesis+open-ended-custom_fields shape that
a raw dict body suits better. A typed model gets real request validation
(enum values, field types) and correct OpenAPI docs for free, with no
meaningful downside for a model this size.
"""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_client_crm_service
from app.models.client_crm import (
    Client,
    ClientContact,
    ClientPage,
    ClientRelationshipClassification,
    ClientStatus,
    ClientTouchpoint,
    ContactType,
    DeclineOrigin,
    DinnerType,
    Engagement,
    EngagementCloseout,
    EngagementContractStatus,
    EngagementParticipant,
    EngagementParticipantView,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
    ParticipantAttendanceStatus,
    ParticipantRole,
    ParticipantRsvpStatus,
)
from app.models.luma import LumaEventSummary
from app.services.client_crm_service import (
    ClientContactNotFound,
    ClientCrmService,
    ClientNotFound,
    ClientTouchpointNotFound,
    EngagementCloseoutAlreadyExists,
    EngagementCloseoutNotFound,
    EngagementLumaEventAlreadyLinked,
    EngagementNotFound,
    EngagementParticipantDuplicate,
    EngagementParticipantNotFound,
    LumaEventNotFound,
)

router = APIRouter(prefix="/client-crm", tags=["client-crm"])


class ClientCreateRequest(BaseModel):
    name: str
    website: str | None = None
    industry: str | None = None
    status: ClientStatus = ClientStatus.ACTIVE
    relationship_classification: ClientRelationshipClassification | None = None
    owner: str | None = None
    next_action: str | None = None
    next_action_due: date | None = None


class ClientUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH. `client_id`/
    `created_at`/`updated_at` have no fields here at all (not merely
    ignored), so there is nothing for a caller to even attempt to
    overwrite; `updated_at` is always server-set in
    ClientCrmService.update_client()."""

    name: str | None = None
    website: str | None = None
    industry: str | None = None
    status: ClientStatus | None = None
    relationship_classification: ClientRelationshipClassification | None = None
    owner: str | None = None
    next_action: str | None = None
    next_action_due: date | None = None
    archived: bool | None = None  # archive (true) / restore (false) -- see module docstring


class ClientContactCreateRequest(BaseModel):
    """Client CRM Stage 1D. `crm_contact_id` is required -- V1 has no
    free-text-only person-creation path (see this stage's own STOP
    report). Snapshot fields (name/email/phone) are populated server-side
    from the canonical Contact, not accepted here."""

    crm_contact_id: str
    title: str | None = None  # role AT THIS CLIENT, e.g. "VP of BD"
    is_primary_contact: bool = False
    is_decision_maker: bool = False
    role_notes: str | None = None


class ClientContactUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH. No crm_contact_id/
    snapshot fields here -- re-linking to a different person isn't
    supported in V1 (archive this relationship and add a new one
    instead)."""

    title: str | None = None
    is_primary_contact: bool | None = None
    is_decision_maker: bool | None = None
    role_notes: str | None = None
    archived: bool | None = None


class EngagementCreateRequest(BaseModel):
    """Client CRM Stage 1E, taxonomy corrected in Stage 1E.1. `dinner_type`
    is only ever meaningful when `engagement_type == DINNER` -- the
    service layer is the authoritative enforcement of that (forces it to
    None otherwise), this request model just accepts whatever the caller
    sends. `luma_event_id` is accepted here (validated against stored
    luma_events, Stage 1H-A) but is deliberately NOT exposed on the
    Add/Engagement form -- Stage 1H-A's own linking UI lives as a
    dedicated section on the Engagement detail page instead (PATCH via
    EngagementUpdateRequest below), not this create form. No participant
    sync/auto-create from Luma exists yet -- link-only."""

    title: str
    engagement_type: EngagementType
    dinner_type: DinnerType | None = None
    engagement_date: date | None = None
    location: str | None = None
    status: EngagementStatus = EngagementStatus.PLANNED
    owner: str | None = None
    fee: float | None = None
    contract_status: EngagementContractStatus = EngagementContractStatus.NOT_SENT
    contract_url: str | None = None
    signed_date: date | None = None
    payment_status: EngagementPaymentStatus = EngagementPaymentStatus.UNPAID
    luma_event_id: str | None = None  # validated against stored luma_events, see
    # ClientCrmService._validated_luma_event_id() -- Stage 1H-A


class EngagementUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH. `engagement_id`/
    `client_id`/`created_at`/`updated_at` have no fields here at all."""

    title: str | None = None
    engagement_type: EngagementType | None = None
    dinner_type: DinnerType | None = None
    engagement_date: date | None = None
    location: str | None = None
    status: EngagementStatus | None = None
    owner: str | None = None
    fee: float | None = None
    contract_status: EngagementContractStatus | None = None
    contract_url: str | None = None
    signed_date: date | None = None
    payment_status: EngagementPaymentStatus | None = None
    luma_event_id: str | None = None
    archived: bool | None = None  # archive (true) / restore (false) -- see module docstring


_NonNegativeCountField = Annotated[int, Field(ge=0)] | None


class EngagementCloseoutCreateRequest(BaseModel):
    """Client CRM Stage 1F. Every count is nullable and non-negative --
    Field(ge=0) rejects a negative count with a 422 before this ever
    reaches the service layer. Null means "not entered"; an explicit 0
    means "entered, confirmed zero" -- this request model never coerces
    one into the other."""

    confirmed_guest_count: _NonNegativeCountField = None
    attended_count: _NonNegativeCountField = None
    no_show_count: _NonNegativeCountField = None
    cancelled_count: _NonNegativeCountField = None
    unexpected_attendee_count: _NonNegativeCountField = None

    guest_quality: str | None = None
    dinner_dynamics: str | None = None
    initial_client_experience: str | None = None
    immediate_outcomes: str | None = None
    notable_signals: str | None = None
    issues: str | None = None
    referrals: str | None = None
    future_opportunities: str | None = None
    internal_notes: str | None = None

    completed_at: datetime | None = None
    completed_by: str | None = None


class EngagementCloseoutUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH. `closeout_id`/
    `engagement_id`/`client_id`/`created_at`/`updated_at` have no fields
    here at all."""

    confirmed_guest_count: _NonNegativeCountField = None
    attended_count: _NonNegativeCountField = None
    no_show_count: _NonNegativeCountField = None
    cancelled_count: _NonNegativeCountField = None
    unexpected_attendee_count: _NonNegativeCountField = None

    guest_quality: str | None = None
    dinner_dynamics: str | None = None
    initial_client_experience: str | None = None
    immediate_outcomes: str | None = None
    notable_signals: str | None = None
    issues: str | None = None
    referrals: str | None = None
    future_opportunities: str | None = None
    internal_notes: str | None = None

    completed_at: datetime | None = None
    completed_by: str | None = None
    archived: bool | None = None  # archive (true) / restore (false) -- see module docstring


class EngagementParticipantCreateRequest(BaseModel):
    """Client CRM Stage 1G. `crm_contact_id` is OPTIONAL, unlike
    ClientContactCreateRequest's own effectively-mandatory rule -- an
    unresolved participant (no crm_contact_id) is a legitimate case here;
    see EngagementParticipant's own model docstring. When omitted,
    `first_name`/`last_name`/`email`/`title`/`company` are the request's
    own identity fields (at least one of first_name/last_name/email must
    be non-blank -- enforced by the service layer, not this schema, since
    it's a cross-field rule). When `crm_contact_id` IS provided, any of
    those fields sent here are ignored -- the canonical Contact's own
    data always wins. `source` has no field here at all -- Stage 1G
    creates ONLY MANUAL records, server-owned, never caller-settable.

    `decline_origin` (Client CRM Stage 5A/5B) -- only meaningful alongside
    rsvp_status == declined; ClientCrmService normalizes/forces it to null
    for any other rsvp_status regardless of what's sent here. No
    `decline_origin_is_manual` field here at all -- that's backend-owned
    provenance, never caller-settable (the service always marks anything
    submitted through this create path as human-authoritative)."""

    crm_contact_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    title: str | None = None
    company: str | None = None
    role: ParticipantRole = ParticipantRole.GUEST
    rsvp_status: ParticipantRsvpStatus | None = None
    decline_origin: DeclineOrigin | None = None
    attendance_status: ParticipantAttendanceStatus | None = None
    is_walk_in: bool = False


class EngagementParticipantUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH. Unlike
    ClientContactUpdateRequest, `crm_contact_id` IS settable here -- this
    is the explicit "unresolved participant later linked to a canonical
    Contact" transition Stage 1G's own approved design requires (see
    ClientCrmService.update_engagement_participant()'s own docstring for
    the snapshot-refresh and duplicate-protection behavior this triggers).
    Clearing `crm_contact_id` back to null on an already-linked
    participant is explicitly rejected (400) -- resolved -> unresolved is
    not an allowed transition. No `source` field here either -- still
    entirely server-owned.

    `decline_origin` (Client CRM Stage 5A/5B) -- omitted entirely means
    "leave it as it currently is" (genuine partial-PATCH semantics, same
    as every other field here); explicitly sent (including `null`) is a
    direct human assertion and becomes authoritative -- see
    ClientCrmService._normalize_decline_origin_for_update()'s own
    docstring for the exact precedence/clearing rules. No
    `decline_origin_is_manual` field here either -- backend-owned
    provenance, never caller-settable."""

    crm_contact_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    title: str | None = None
    company: str | None = None
    role: ParticipantRole | None = None
    rsvp_status: ParticipantRsvpStatus | None = None
    decline_origin: DeclineOrigin | None = None
    attendance_status: ParticipantAttendanceStatus | None = None
    is_walk_in: bool | None = None
    archived: bool | None = None  # archive (true) / restore (false) -- see module docstring


class ClientTouchpointCreateRequest(BaseModel):
    """Client CRM Stage 2A. `occurred_at` is optional here -- omitting it
    defaults to "now" in ClientCrmService.create_client_touchpoint() (the
    "Date defaults to today" UX requirement). `contact_type`/`contacted_by`
    are required. `contact_name` has no field here at all -- always
    server-derived from `crm_contact_id`, never caller-settable."""

    crm_contact_id: str | None = None
    occurred_at: datetime | None = None
    contact_type: ContactType
    contacted_by: str
    note: str | None = None


class ClientTouchpointUpdateRequest(BaseModel):
    """Every field optional -- a genuine partial PATCH, same convention as
    every other Client CRM update request. Sending `crm_contact_id`
    links/relinks (re-validated + re-snapshotted) or, sent as `null`,
    unlinks (clearing `contact_name` too) -- see
    ClientCrmService.update_client_touchpoint()'s own docstring. No
    `contact_name` field here either -- still entirely server-owned."""

    crm_contact_id: str | None = None
    occurred_at: datetime | None = None
    contact_type: ContactType | None = None
    contacted_by: str | None = None
    note: str | None = None
    archived: bool | None = None  # archive (true) / restore (false) -- see module docstring


@router.get("/clients", response_model=ClientPage)
async def list_clients(
    q: str | None = None,
    status: ClientStatus | None = None,
    relationship_classification: ClientRelationshipClassification | None = None,
    owner: str | None = None,
    include_archived: bool = False,
    sort_by: str = "name",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 50,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """Search/filter/sort/paginate over every stored Client. Archived
    Clients hidden by default. Always returns exactly one page (`items`)
    plus the full filtered `total` count."""
    try:
        return await service.list_clients(
            q=q,
            status=status,
            relationship_classification=relationship_classification,
            owner=owner,
            include_archived=include_archived,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/clients", response_model=Client)
async def create_client(payload: ClientCreateRequest, service: ClientCrmService = Depends(get_client_crm_service)):
    try:
        return await service.create_client(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clients/{client_id}", response_model=Client)
async def get_client(client_id: str, service: ClientCrmService = Depends(get_client_crm_service)):
    try:
        return await service.get_client(client_id)
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/clients/{client_id}", response_model=Client)
async def update_client(
    client_id: str, payload: ClientUpdateRequest, service: ClientCrmService = Depends(get_client_crm_service)
):
    """`exclude_unset=True` is what makes this a genuine partial PATCH --
    a field the caller's JSON body never mentioned is left alone entirely,
    while a field explicitly sent as `null` (e.g. clearing next_action_due)
    IS applied, since it was still present in the request body."""
    try:
        return await service.update_client(client_id, payload.model_dump(exclude_unset=True))
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clients/{client_id}/contacts", response_model=list[ClientContact])
async def list_client_contacts(client_id: str, service: ClientCrmService = Depends(get_client_crm_service)):
    try:
        return await service.list_client_contacts(client_id)
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/clients/{client_id}/contacts", response_model=ClientContact)
async def create_client_contact(
    client_id: str, payload: ClientContactCreateRequest, service: ClientCrmService = Depends(get_client_crm_service)
):
    try:
        return await service.create_client_contact(client_id, payload.model_dump())
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/clients/{client_id}/contacts/{client_contact_id}", response_model=ClientContact)
async def update_client_contact(
    client_id: str,
    client_contact_id: str,
    payload: ClientContactUpdateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    try:
        return await service.update_client_contact(client_id, client_contact_id, payload.model_dump(exclude_unset=True))
    except (ClientNotFound, ClientContactNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clients/{client_id}/engagements", response_model=list[Engagement])
async def list_client_engagements(client_id: str, service: ClientCrmService = Depends(get_client_crm_service)):
    try:
        return await service.list_client_engagements(client_id)
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/clients/{client_id}/engagements", response_model=Engagement)
async def create_client_engagement(
    client_id: str, payload: EngagementCreateRequest, service: ClientCrmService = Depends(get_client_crm_service)
):
    """400 if `luma_event_id` is set but doesn't refer to a stored Luma
    event. 409 if it refers to one already linked to a DIFFERENT
    Engagement (Stage 1H-A: one Luma event links to at most one
    Engagement)."""
    try:
        return await service.create_client_engagement(client_id, payload.model_dump())
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EngagementLumaEventAlreadyLinked as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clients/{client_id}/engagements/{engagement_id}", response_model=Engagement)
async def get_client_engagement(
    client_id: str, engagement_id: str, service: ClientCrmService = Depends(get_client_crm_service)
):
    try:
        return await service.get_client_engagement(client_id, engagement_id)
    except EngagementNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/clients/{client_id}/engagements/{engagement_id}", response_model=Engagement)
async def update_client_engagement(
    client_id: str,
    engagement_id: str,
    payload: EngagementUpdateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """`exclude_unset=True` is what makes this a genuine partial PATCH --
    same convention as update_client()/update_client_contact(). Sending
    `luma_event_id` links (or, sent as `null`, unlinks) this Engagement's
    Luma event -- see EngagementUpdateRequest's own docstring. 400 if the
    given id doesn't refer to a stored Luma event; 409 if it's already
    linked to a DIFFERENT Engagement (Stage 1H-A: one Luma event links to
    at most one Engagement)."""
    try:
        return await service.update_client_engagement(client_id, engagement_id, payload.model_dump(exclude_unset=True))
    except (ClientNotFound, EngagementNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EngagementLumaEventAlreadyLinked as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/luma-events", response_model=list[LumaEventSummary])
async def list_luma_events(q: str | None = None, service: ClientCrmService = Depends(get_client_crm_service)):
    """Read-only listing of persisted Luma events for the Engagement-
    linking picker (Stage 1H-A) -- see ClientCrmService.list_luma_events()'s
    own docstring. Reads only what's already stored via the live Luma
    webhook/backfill; makes no Luma API call itself. `q`, when given,
    filters by a case-insensitive substring match against the event name."""
    return await service.list_luma_events(q=q)


@router.get("/luma-events/{luma_event_id}", response_model=LumaEventSummary)
async def get_luma_event(luma_event_id: str, service: ClientCrmService = Depends(get_client_crm_service)):
    """Single-record counterpart to list_luma_events() -- the Engagement
    detail page's own "show the linked event's name/date" need. 404 if
    nothing is stored under this id."""
    try:
        return await service.get_luma_event(luma_event_id)
    except LumaEventNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/clients/{client_id}/engagements/{engagement_id}/closeout", response_model=EngagementCloseout)
async def get_engagement_closeout(
    client_id: str, engagement_id: str, service: ClientCrmService = Depends(get_client_crm_service)
):
    """404 when no Closeout has been recorded yet -- a normal, expected
    state the frontend renders as its own empty state, not a generic
    error (same convention the rest of this API already uses for a
    genuinely-missing resource)."""
    try:
        return await service.get_engagement_closeout(client_id, engagement_id)
    except (ClientNotFound, EngagementNotFound, EngagementCloseoutNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/clients/{client_id}/engagements/{engagement_id}/closeout", response_model=EngagementCloseout)
async def create_engagement_closeout(
    client_id: str,
    engagement_id: str,
    payload: EngagementCloseoutCreateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """409 if this Engagement already has a Closeout -- at most one is
    ever created; PATCH the existing one instead."""
    try:
        return await service.create_engagement_closeout(client_id, engagement_id, payload.model_dump())
    except (ClientNotFound, EngagementNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EngagementCloseoutAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/clients/{client_id}/engagements/{engagement_id}/closeout", response_model=EngagementCloseout)
async def update_engagement_closeout(
    client_id: str,
    engagement_id: str,
    payload: EngagementCloseoutUpdateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """`exclude_unset=True` is what makes this a genuine partial PATCH --
    same convention as every other Client CRM update route. 404 if no
    Closeout exists yet -- use POST to create one first."""
    try:
        return await service.update_engagement_closeout(client_id, engagement_id, payload.model_dump(exclude_unset=True))
    except (ClientNotFound, EngagementNotFound, EngagementCloseoutNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/clients/{client_id}/engagements/{engagement_id}/participants", response_model=list[EngagementParticipantView]
)
async def list_engagement_participants(
    client_id: str, engagement_id: str, service: ClientCrmService = Depends(get_client_crm_service)
):
    try:
        return await service.list_engagement_participants(client_id, engagement_id)
    except (ClientNotFound, EngagementNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/clients/{client_id}/engagements/{engagement_id}/participants", response_model=EngagementParticipant)
async def create_engagement_participant(
    client_id: str,
    engagement_id: str,
    payload: EngagementParticipantCreateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """409 if `crm_contact_id` is already an active participant of this
    Engagement. 400 if an unresolved participant (no crm_contact_id) has
    no name or email at all."""
    try:
        return await service.create_engagement_participant(client_id, engagement_id, payload.model_dump())
    except (ClientNotFound, EngagementNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EngagementParticipantDuplicate as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/clients/{client_id}/engagements/{engagement_id}/participants/{participant_id}",
    response_model=EngagementParticipant,
)
async def update_engagement_participant(
    client_id: str,
    engagement_id: str,
    participant_id: str,
    payload: EngagementParticipantUpdateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """`exclude_unset=True` is what makes this a genuine partial PATCH --
    same convention as every other Client CRM update route. Sending
    `crm_contact_id` links/relinks an unresolved participant to a
    canonical Contact -- 409 if that Contact is already an active
    participant of this Engagement. 400 if this would clear an
    already-linked participant's `crm_contact_id` back to null --
    resolved -> unresolved is not an allowed transition."""
    try:
        return await service.update_engagement_participant(
            client_id, engagement_id, participant_id, payload.model_dump(exclude_unset=True)
        )
    except (ClientNotFound, EngagementNotFound, EngagementParticipantNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EngagementParticipantDuplicate as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# ClientTouchpoint -- Client CRM Stage 2A (2026-09-11). Backend foundation
# only -- no derived Last Contact/Last Contacted/Next Dinner is computed
# or returned anywhere in this stage. No DELETE route -- archive/restore
# only, same convention as every other Client CRM entity.
# =========================================================================


@router.get("/clients/{client_id}/touchpoints", response_model=list[ClientTouchpoint])
async def list_client_touchpoints(
    client_id: str, include_archived: bool = False, service: ClientCrmService = Depends(get_client_crm_service)
):
    """Newest first. Archived Touchpoints are excluded by default --
    pass `include_archived=true` to include them too -- see
    ClientCrmService.list_client_touchpoints()'s own docstring for why
    this stage's default differs from list_client_contacts()/
    list_engagement_participants() (neither of which filters)."""
    try:
        return await service.list_client_touchpoints(client_id, include_archived=include_archived)
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/clients/{client_id}/touchpoints", response_model=ClientTouchpoint)
async def create_client_touchpoint(
    client_id: str, payload: ClientTouchpointCreateRequest, service: ClientCrmService = Depends(get_client_crm_service)
):
    """400 if `contacted_by` is blank, `contact_type` is invalid, or
    `crm_contact_id` doesn't refer to a Contact already linked to this
    Client via an active ClientContact."""
    try:
        return await service.create_client_touchpoint(client_id, payload.model_dump())
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/clients/{client_id}/touchpoints/{touchpoint_id}", response_model=ClientTouchpoint)
async def update_client_touchpoint(
    client_id: str,
    touchpoint_id: str,
    payload: ClientTouchpointUpdateRequest,
    service: ClientCrmService = Depends(get_client_crm_service),
):
    """`exclude_unset=True` is what makes this a genuine partial PATCH --
    same convention as every other Client CRM update route. 400 for the
    same validation cases as create, plus an attempt to blank out an
    already-set `contacted_by`."""
    try:
        return await service.update_client_touchpoint(client_id, touchpoint_id, payload.model_dump(exclude_unset=True))
    except (ClientNotFound, ClientTouchpointNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
