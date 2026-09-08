"""
Client CRM API -- Stage 1B (2026-09-07) Client CRUD, extended for
ClientContact (Stage 1D) and Engagement (Stage 1E, 2026-09-07).
ClientNote has no routes yet (a later stage).

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

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_client_crm_service
from app.models.client_crm import (
    Client,
    ClientContact,
    ClientPage,
    ClientRelationshipClassification,
    ClientStatus,
    DinnerType,
    Engagement,
    EngagementContractStatus,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
)
from app.services.client_crm_service import (
    ClientContactNotFound,
    ClientCrmService,
    ClientNotFound,
    EngagementNotFound,
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
    sends. `luma_event_id` is accepted here (reference-only, "expose the
    reference appropriately" per Stage 1E's own approved scope) but is
    deliberately NOT exposed in the frontend Add/Engagement form yet -- no
    Luma picker/sync/auto-create is part of Client CRM yet."""

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
    luma_event_id: str | None = None


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
    try:
        return await service.create_client_engagement(client_id, payload.model_dump())
    except ClientNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
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
    same convention as update_client()/update_client_contact()."""
    try:
        return await service.update_client_engagement(client_id, engagement_id, payload.model_dump(exclude_unset=True))
    except (ClientNotFound, EngagementNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
