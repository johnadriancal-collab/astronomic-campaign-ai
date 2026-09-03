"""
CRM routes -- a standalone top-level area (see docs/ARCHITECTURE.md-style
convention: this module owns everything under /crm, the same way
app/api/campaign.py owns everything under /campaign). Nothing here reads
or writes Campaign/Lead/CampaignLead/EmailSequence/EmailMessage.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from pydantic import BaseModel

from app.dependencies import get_crm_import_service, get_crm_service, get_luma_sync_service
from app.models.crm import (
    CrmContact,
    CrmContactExportField,
    CrmContactListSummary,
    CrmContactPage,
    CrmCustomFieldDefinition,
    CrmImportBatch,
    CrmImportReport,
    CrmListBulkAddResult,
    CrmListBulkRemoveResult,
    CustomFieldType,
    FilterFieldMeta,
    FilterQuery,
    get_contact_export_fields,
)
from app.models.luma import CrmContactLumaRegistration
from app.services.crm_filter_service import FilterValidationError
from app.services.crm_import_service import CrmImportBatchNotFound, CrmImportService
from app.services.crm_migration import (
    reconcile_legacy_fields,
    repair_all_contacts_comma_delimited_fields,
    translate_legacy_import_batch,
)
from app.services.crm_service import (
    CrmContactListNotFound,
    CrmContactNotFound,
    CrmCustomFieldNotFound,
    CrmDuplicateFieldKeyError,
    CrmService,
)
from app.services.luma_sync_service import LumaSyncService

router = APIRouter(prefix="/crm", tags=["crm"])


def _operator_actor(request: Request) -> str | None:
    """Same convention as app/api/mail.py's _operator_actor -- see that
    function's docstring and app/session_auth_middleware.py's own
    "Attribution" docstring section."""
    return "claude_operator" if getattr(request.state, "identity", None) == "service_operator" else None


class CrmCustomFieldCreateRequest(BaseModel):
    field_key: str
    label: str
    field_type: CustomFieldType
    description: str | None = None
    options: list[str] = []
    required: bool = False


class CrmListCreateRequest(BaseModel):
    name: str
    description: str | None = None


class CrmListBulkContactIdsRequest(BaseModel):
    contact_ids: list[str]


class CrmImportPreviewRequest(BaseModel):
    column_mapping: dict[str, str]


class CrmImportCommitRequest(BaseModel):
    decisions: dict[str, str] = {}  # row_index (as string, JSON object keys) -> create|update|skip


# --- Contacts ---


@router.get("/contacts", response_model=CrmContactPage)
async def list_contacts(
    q: str | None = None,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    company: str | None = None,
    industry: str | None = None,
    deal_stage: str | None = None,
    check_size: str | None = None,
    investor_mode: str | None = None,
    email_status: str | None = None,
    include_archived: bool = False,
    page: int = 1,
    page_size: int = 50,
    service: CrmService = Depends(get_crm_service),
):
    """
    Search/filter/paginate over every stored contact. Archived contacts
    hidden by default. Always returns exactly one page (`items`) plus the
    full filtered `total` count -- the caller never has to fetch
    everything to know how many pages exist.
    """
    return await service.list_contacts(
        q=q, city=city, state=state, country=country, company=company, industry=industry,
        deal_stage=deal_stage, check_size=check_size, investor_mode=investor_mode,
        email_status=email_status, include_archived=include_archived,
        page=page, page_size=page_size,
    )


@router.post("/contacts/query", response_model=CrmContactPage)
async def query_contacts(query: FilterQuery, service: CrmService = Depends(get_crm_service)):
    """
    More Filters: a dynamic field+operator+value query over every filterable field
    (core, thesis, and active custom fields alike) -- entirely additive, does not
    replace or alter GET /contacts above, which keeps backing the existing Contacts
    page's keyword/city/investor-mode search unmodified. Filtering, sorting, and
    pagination all happen server-side; the caller never has to fetch more than the
    one page it asked for. Every field/operator/value is validated against the live
    field registry (GET /filterable-fields) before anything runs -- an unrecognized
    field or a disallowed operator for that field's type is rejected with a 400,
    never silently ignored or passed through to a raw query.
    """
    try:
        return await service.query_contacts(query)
    except FilterValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/contacts", response_model=CrmContact)
async def create_contact(fields: dict[str, Any] = Body(...), service: CrmService = Depends(get_crm_service)):
    """Manual creation. Rejects a duplicate on the three confident dedup tiers only."""
    try:
        return await service.create_contact(fields)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/contacts/export-fields", response_model=list[CrmContactExportField])
async def list_contact_export_fields():
    """
    Every core/thesis field on CrmContact, computed via introspection --
    schema metadata only, never contact data. Backs the CRM contacts CSV
    export feature's dynamic column list (paired with GET /custom-fields
    for custom columns) so a field added to the model later is included
    automatically, with no route/list to update by hand. Declared ahead
    of GET /contacts/{crm_contact_id} so "export-fields" is never matched
    as a contact id.
    """
    return get_contact_export_fields()


@router.get("/filterable-fields", response_model=list[FilterFieldMeta])
async def list_filterable_fields(service: CrmService = Depends(get_crm_service)):
    """
    The complete More Filters field registry -- core/thesis fields (hand-registered
    in crm_filter_service.py) merged with every ACTIVE custom field definition, each
    normalized to the same shape (key/label/category/type/options/operators/ordered).
    This is the single source of truth the frontend builds its entire filter UI
    from; it never maintains a second, independent field list of its own.
    """
    return await service.get_filterable_fields()


@router.get("/contacts/{crm_contact_id}", response_model=CrmContact)
async def get_contact(crm_contact_id: str, service: CrmService = Depends(get_crm_service)):
    try:
        return await service.get_contact(crm_contact_id)
    except CrmContactNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/contacts/{crm_contact_id}/luma-registrations", response_model=list[CrmContactLumaRegistration])
async def get_contact_luma_registrations(
    crm_contact_id: str,
    crm_service: CrmService = Depends(get_crm_service),
    luma_service: LumaSyncService = Depends(get_luma_sync_service),
):
    """Read-only Event History for the contact detail page -- reuses
    LumaRegistrationStore.list_for_contact() (already built, joined
    server-side with each registration's event name) so the frontend never
    sees raw registration_answers. Purely a read: no enrichment, no
    Activity Log event, no Luma API call happens here."""
    try:
        await crm_service.get_contact(crm_contact_id)
    except CrmContactNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await luma_service.list_contact_event_history(crm_contact_id)


@router.patch("/contacts/{crm_contact_id}", response_model=CrmContact)
async def update_contact(
    crm_contact_id: str, patch: dict[str, Any] = Body(...), service: CrmService = Depends(get_crm_service)
):
    """Direct partial edit -- core fields, thesis fields, or custom_fields. No merge rule; a
    human editing directly may set or clear any field."""
    try:
        return await service.update_contact(crm_contact_id, patch)
    except CrmContactNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/contacts/{crm_contact_id}", response_model=CrmContact)
async def archive_contact(crm_contact_id: str, service: CrmService = Depends(get_crm_service)):
    """Archives (soft-deletes) the contact -- never hard-deleted."""
    try:
        return await service.archive_contact(crm_contact_id)
    except CrmContactNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Lists: named, persistent groupings of existing contacts ---
#
# A list is a real, permanent delete (the one entity in this codebase that
# isn't archive-only) -- see crm_contact_list_store.py's docstring for why
# that's safe here. Every route below is read/write only against the list
# and its memberships; none of them ever create, edit, or archive a
# CrmContact. Declared ahead of the generic /contacts/{crm_contact_id}
# routes is unnecessary here since "lists" can't collide with a contact id,
# but grouped together for readability.


@router.get("/lists", response_model=list[CrmContactListSummary])
async def list_contact_lists(service: CrmService = Depends(get_crm_service)):
    return await service.list_contact_lists()


@router.post("/lists", response_model=CrmContactListSummary)
async def create_contact_list(req: CrmListCreateRequest, request: Request, service: CrmService = Depends(get_crm_service)):
    return await service.create_contact_list(name=req.name, description=req.description, actor=_operator_actor(request))


@router.get("/lists/{list_id}", response_model=CrmContactListSummary)
async def get_contact_list(list_id: str, service: CrmService = Depends(get_crm_service)):
    try:
        return await service.get_contact_list(list_id)
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/lists/{list_id}", response_model=CrmContactListSummary)
async def update_contact_list(
    list_id: str, request: Request, patch: dict[str, Any] = Body(...), service: CrmService = Depends(get_crm_service)
):
    """Rename and/or edit the description. See CrmService.update_contact_list --
    any other key in the body (e.g. list_id, created_at) is silently ignored."""
    try:
        return await service.update_contact_list(list_id, patch, actor=_operator_actor(request))
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/lists/{list_id}", response_model=CrmContactListSummary)
async def delete_contact_list(list_id: str, service: CrmService = Depends(get_crm_service)):
    """Permanently deletes the list and its memberships. Never deletes, archives,
    or edits any CrmContact -- see CrmService.delete_contact_list. Returns the
    summary as it was immediately before deletion (every route in this API
    returns JSON on success, no 204s)."""
    try:
        return await service.delete_contact_list(list_id)
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/lists/{list_id}/contacts", response_model=CrmContactPage)
async def get_list_contacts(
    list_id: str, page: int = 1, page_size: int = 50, service: CrmService = Depends(get_crm_service)
):
    """Same CrmContactPage shape as GET /contacts and POST /contacts/query --
    the frontend's shared ContactResults component needs no special-casing to
    render a list's contacts. Contacts are always fetched live from
    crm_contacts; this never returns a stored copy."""
    try:
        return await service.get_list_contacts(list_id, page=page, page_size=page_size)
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/lists/{list_id}/contacts/bulk-add", response_model=CrmListBulkAddResult)
async def bulk_add_to_list(
    list_id: str, req: CrmListBulkContactIdsRequest, request: Request, service: CrmService = Depends(get_crm_service)
):
    """The ONE call the frontend makes regardless of whether it's adding 1 contact
    or every contact currently selected (which may already be thousands, from
    Contacts/More Filters/Astro Search's existing "Select all N matching") --
    never one request per contact. Duplicate membership is idempotent, never an
    error; see CrmService.bulk_add_to_list for why the response reports counts
    instead of raising on a repeat or an unrecognized id."""
    try:
        return await service.bulk_add_to_list(list_id, req.contact_ids, actor=_operator_actor(request))
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/lists/{list_id}/contacts/bulk-remove", response_model=CrmListBulkRemoveResult)
async def bulk_remove_from_list(
    list_id: str, req: CrmListBulkContactIdsRequest, request: Request, service: CrmService = Depends(get_crm_service)
):
    try:
        return await service.bulk_remove_from_list(list_id, req.contact_ids, actor=_operator_actor(request))
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/lists/{list_id}/contacts/{crm_contact_id}", response_model=CrmContactListSummary)
async def remove_contact_from_list(
    list_id: str, crm_contact_id: str, service: CrmService = Depends(get_crm_service)
):
    """Idempotent -- removing a contact that isn't currently a member is a
    no-op, not a 404. Never archives or deletes the contact itself. Returns
    the list's updated summary (contact_count reflects the removal)."""
    try:
        return await service.remove_contact_from_list(list_id, crm_contact_id)
    except CrmContactListNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- Backup + legacy-field reconciliation ---


@router.get("/backup/export")
async def export_backup(service: CrmService = Depends(get_crm_service)):
    """Full JSON snapshot of every contact + custom field definition -- take one of
    these before any bulk operation (e.g. reconcile-legacy-fields below)."""
    return await service.export_backup()


@router.post("/reconcile-legacy-fields")
async def reconcile_legacy_fields_route(service: CrmService = Depends(get_crm_service)):
    """
    Seeds the CrmCustomFieldDefinitions for Astronomic's pre-existing CRM
    fields that have no core/thesis equivalent, then migrates any values
    already sitting under the old duplicate field keys (Deal Stage, Check
    Size, ...) into the matching Investor Thesis field. Additive only --
    never deletes or overwrites existing data. Safe to call more than
    once; take a GET /crm/backup/export snapshot first regardless.
    """
    return await reconcile_legacy_fields(service)


@router.post("/repair-comma-delimited-custom-fields")
async def repair_comma_delimited_custom_fields_route(service: CrmService = Depends(get_crm_service)):
    """
    One-time targeted repair for Investor Type/Dinners Attended values
    committed before translate_legacy_import_batch() covered them --
    multi-selection values were stored as one comma-joined string in a
    one-item list instead of separate list items. Re-derives the correct
    value from each contact's own `source_snapshot`; touches only
    `custom_fields`/`updated_at`; leaves genuinely single-value contacts
    untouched. Idempotent. Take a GET /crm/backup/export snapshot first.
    """
    return await repair_all_contacts_comma_delimited_fields(service.contact_store)


# --- Custom field definitions ---


@router.get("/custom-fields", response_model=list[CrmCustomFieldDefinition])
async def list_custom_fields(include_inactive: bool = True, service: CrmService = Depends(get_crm_service)):
    return await service.list_custom_fields(include_inactive=include_inactive)


@router.post("/custom-fields", response_model=CrmCustomFieldDefinition)
async def create_custom_field(req: CrmCustomFieldCreateRequest, service: CrmService = Depends(get_crm_service)):
    try:
        return await service.create_custom_field(
            field_key=req.field_key, label=req.label, field_type=req.field_type,
            description=req.description, options=req.options, required=req.required,
        )
    except CrmDuplicateFieldKeyError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.patch("/custom-fields/{crm_custom_field_id}", response_model=CrmCustomFieldDefinition)
async def update_custom_field(
    crm_custom_field_id: str, patch: dict[str, Any] = Body(...), service: CrmService = Depends(get_crm_service)
):
    """Edits a definition, or flips active/inactive. Never deletes -- deactivating hides it
    from the UI without touching contacts that already have data under this field_key."""
    try:
        return await service.update_custom_field(crm_custom_field_id, patch)
    except CrmCustomFieldNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- CSV import: upload -> preview -> commit ---


@router.post("/import/upload", response_model=CrmImportBatch)
async def upload_import(file: UploadFile = File(...), service: CrmImportService = Depends(get_crm_import_service)):
    """Parses the CSV and persists it immediately -- row count, headers, and a suggested
    mapping are available right away; nothing is written to CrmContact yet."""
    content = await file.read()
    try:
        return await service.upload(file.filename or "upload.csv", content)
    except Exception as e:
        logger.error(f"CRM import upload failed: {e}")
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {e}")


@router.get("/import/{import_batch_id}", response_model=CrmImportBatch)
async def get_import_batch(import_batch_id: str, service: CrmImportService = Depends(get_crm_import_service)):
    try:
        return await service.get_batch(import_batch_id)
    except CrmImportBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/import/{import_batch_id}/translate-legacy-values", response_model=CrmImportBatch)
async def translate_legacy_values(import_batch_id: str, service: CrmImportService = Depends(get_crm_import_service)):
    """
    Astronomic-specific, not a generic import step: rewrites the five
    legacy multi-select thesis columns from comma-joined abbreviated text
    into semicolon-joined canonical Investor Thesis wording, and re-
    tokenizes "How early do you invest?" by known-phrase match (two of
    its six real options contain their own comma). Call this once, right
    after upload, before /preview. A no-op for any batch that doesn't
    contain these column headers.
    """
    try:
        batch = await service.get_batch(import_batch_id)
    except CrmImportBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    translated = translate_legacy_import_batch(batch)
    await service.batch_store.save(translated)
    return translated


@router.post("/import/{import_batch_id}/preview", response_model=CrmImportBatch)
async def preview_import(
    import_batch_id: str, req: CrmImportPreviewRequest, service: CrmImportService = Depends(get_crm_import_service)
):
    """Applies the confirmed column mapping and classifies every row (new/existing/possible
    duplicate/error) against both the CRM and the rest of this same file. Makes no writes."""
    try:
        return await service.preview(import_batch_id, req.column_mapping)
    except CrmImportBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/import/{import_batch_id}/commit", response_model=CrmImportReport)
async def commit_import(
    import_batch_id: str, req: CrmImportCommitRequest, service: CrmImportService = Depends(get_crm_import_service)
):
    """Applies create/update/skip per row. A possible_duplicate row with no explicit
    decision defaults to skip -- it is never silently created or merged."""
    try:
        decisions = {int(row_index): decision for row_index, decision in req.decisions.items()}
        return await service.commit(import_batch_id, decisions)
    except CrmImportBatchNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
