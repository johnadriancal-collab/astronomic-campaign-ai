"""
Narrow, read-only routes for external machine integrations that need a
single, minimal fact about a Contact -- never the general CRM API, which
returns the full CrmContact (every Apollo/thesis/custom field, notes,
IDs, everything). Each route here returns exactly the smallest response
its one caller needs, nothing more. Today: the existing Leads List Google
Apps Script (Karla's Luma guest.registered -> Accredited Leads Sheet
automation) resolving a Contact's canonical profile_photo_url by email,
so the Sheet's existing photo column can be populated via
`=IMAGE(profile_photo_url)` without exposing anything else about the
Contact.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import get_crm_service, verify_integrations_api_token
from app.models.crm import normalize_email
from app.services.crm_service import CrmService

router = APIRouter(prefix="/integrations", tags=["integrations"])


class ContactPhotoLookupResponse(BaseModel):
    """Deliberately only these two fields -- no name, email, phone,
    LinkedIn, custom_fields, investor data, notes, or crm_contact_id.
    `found` distinguishes "no Contact for this email" from "Contact
    exists but has no photo" for the caller's own logging; both leave
    profile_photo_url null, and either way the Apps Script's own behavior
    is identical (leave the image cell blank)."""

    found: bool
    profile_photo_url: str | None = None


@router.get("/contacts/photo", response_model=ContactPhotoLookupResponse)
async def get_contact_photo_by_email(
    email: str = Query(...),
    _auth: None = Depends(verify_integrations_api_token),
    service: CrmService = Depends(get_crm_service),
):
    """
    Read-only. Auth (verify_integrations_api_token) runs first and
    rejects a missing/invalid token before this body -- which touches the
    CRM -- ever executes. Normalizes `email` exactly like every other
    dedup/matching path in this codebase (app.models.crm.normalize_email)
    before looking it up via CrmContactStore's existing indexed
    get_by_email() -- no new lookup mechanism, no full-table scan.

    A blank/whitespace-only email (normalize_email returns None) is
    rejected with 422 before any CRM lookup happens -- the same
    "reject before touching anything" discipline as the auth check
    above, just for a different failure class.
    """
    normalized = normalize_email(email)
    if normalized is None:
        raise HTTPException(status_code=422, detail="A non-blank email is required.")

    contact = await service.contact_store.get_by_email(normalized)
    if contact is None:
        return ContactPhotoLookupResponse(found=False, profile_photo_url=None)
    return ContactPhotoLookupResponse(found=True, profile_photo_url=contact.profile_photo_url)
