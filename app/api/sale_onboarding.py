"""
POST /sync/sale-onboarding -- the Sale Bot (astronomic-sale-automation, a
separate Render service) calling in once completion-tracker.js confirms a
sale is signed AND paid. See app/services/sale_onboarding_service.py for
the actual orchestration; this module is request/response shaping and
error-to-HTTP-status mapping only.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies import get_client_crm_service, get_crm_service, verify_sale_bot_webhook_token
from app.services.client_crm_service import ClientCrmService
from app.services.crm_service import CrmService
from app.services.sale_onboarding_service import (
    SaleOnboardingConflict,
    SaleOnboardingPayload,
    SaleOnboardingService,
)

router = APIRouter(prefix="/sync", tags=["sale-onboarding"])


class SaleOnboardingRequest(BaseModel):
    """Mirrors astronomic-sale-automation's own entry/sale shape exactly
    (see that repo's src/sale-record.js and src/index.js FIELDS) -- no
    renaming, no restructuring, so the Sale Bot's own payload construction
    stays a near-literal field-for-field mapping. qb_amount is the
    Sale Bot's OWN already-parsed float (entry.qbAmount), not the raw
    total_amount string -- deliberately preferred over re-parsing
    sale.total_amount here, since the Sale Bot's parse is what QuickBooks
    itself was actually invoiced for."""

    sale_id: str
    docusign_envelope_id: str
    mercury_invoice_id: str
    qb_invoice_id: str
    qb_customer_id: str
    qb_amount: float
    client_company: str
    primary_contact: str
    contact_email: str
    signer_name: str
    signer_email: str
    service_sold: str
    city: str
    event_date: str
    internal_owner: str | None = None
    referral_source: str | None = None
    special_terms: str | None = None


class SaleOnboardingResponse(BaseModel):
    already_processed: bool
    client_id: str
    engagement_id: str
    client_contact_id: str | None
    crm_contact_id: str | None
    created_new_client: bool
    created_new_contact: bool
    event_date_parsed: bool
    warnings: list[str]


def get_sale_onboarding_service(
    client_crm_service: ClientCrmService = Depends(get_client_crm_service),
    crm_service: CrmService = Depends(get_crm_service),
) -> SaleOnboardingService:
    # Built fresh per-request from the app's own already-constructed
    # singletons (never a second, independently-configured copy of
    # ClientCrmService/CrmService) -- same "compose from app.state, don't
    # re-instantiate" convention as every other Depends(...) function in
    # app/dependencies.py.
    return SaleOnboardingService(client_crm_service=client_crm_service, crm_service=crm_service)


@router.post("/sale-onboarding", response_model=SaleOnboardingResponse)
async def sale_onboarding(
    payload: SaleOnboardingRequest,
    _auth: None = Depends(verify_sale_bot_webhook_token),
    service: SaleOnboardingService = Depends(get_sale_onboarding_service),
):
    """
    Auth (verify_sale_bot_webhook_token) runs first and rejects a
    missing/invalid token before this body -- which creates/updates Client
    CRM records -- ever executes.

    Idempotent by sale_id (see SaleOnboardingService.onboard()'s own
    docstring): a repeat request for an already-onboarded sale returns
    200 with already_processed=true, never a duplicate write. A genuine
    docusign_envelope_id conflict (the same contract under a DIFFERENT
    sale_id) is the one case this rejects, as 409 -- a real safety
    condition, not a retry.
    """
    try:
        result = await service.onboard(SaleOnboardingPayload(**payload.model_dump()))
    except SaleOnboardingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SaleOnboardingResponse(
        already_processed=result.already_processed,
        client_id=result.client_id,
        engagement_id=result.engagement_id,
        client_contact_id=result.client_contact_id,
        crm_contact_id=result.crm_contact_id,
        created_new_client=result.created_new_client,
        created_new_contact=result.created_new_contact,
        event_date_parsed=result.event_date_parsed,
        warnings=result.warnings,
    )
