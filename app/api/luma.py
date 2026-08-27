"""
Luma (lu.ma) event-registration sync routes.

POST /sync/luma-event -- the live webhook target. Deliberately NOT session-
authenticated (Luma's own browser never holds a Hub session cookie) --
its authentication is entirely Luma's own webhook signature, verified by
verify_luma_webhook_request (see app/dependencies.py) BEFORE this body
runs. This path is on session_auth_middleware.py's PUBLIC_PATHS allowlist
for exactly that reason, mirroring /sync/itf-contact and
/sync/email-intake's precedent -- being public only exempts it from the
session-cookie gate; the signature check is the real guard.

POST /sync/luma-backfill -- the one-time historical import trigger.
Deliberately session-authenticated (an internal admin action a logged-in
Hub user triggers), so it carries NO auth dependency of its own -- it's
simply absent from PUBLIC_PATHS, protected by the same deny-by-default
middleware as every other data route.

`mapping_router` below (GET/POST/PATCH /crm/luma-question-mappings) is the
minimal INTERNAL config API for the label -> CRM field mapping layer
(app/models/luma.py's LumaQuestionMapping) -- also session-authenticated,
also never in PUBLIC_PATHS. No mapping-management frontend exists yet by
design; these routes exist so mappings can be configured (e.g. via
Swagger UI or curl) once real Luma registration questions are known,
without needing a code change per mapping. Luma's webhook payload has NO
path to these routes or to LumaSyncService's mapping-mutation methods --
handle_webhook()/process_guest_event() only ever call
mapping_store.list(), never create/save.

Every error path below returns a clean, generic message -- never Luma's
raw response body, never the API key/webhook secret.
"""

import json

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger

from app.dependencies import get_luma_sync_service, verify_luma_webhook_request
from app.models.luma import LumaBackfillCheckpoint, LumaQuestionMapping, LumaQuestionMappingCreateRequest, LumaQuestionMappingUpdateRequest
from app.services.luma_sync_service import LumaMappingNotFoundError, LumaSyncError, LumaSyncService

router = APIRouter(prefix="/sync", tags=["luma"])
mapping_router = APIRouter(prefix="/crm/luma-question-mappings", tags=["luma"])


@router.post("/luma-event")
async def luma_webhook(
    raw_body: bytes = Depends(verify_luma_webhook_request),
    webhook_id: str | None = Header(default=None, alias="Webhook-Id"),
    service: LumaSyncService = Depends(get_luma_sync_service),
):
    """
    verify_luma_webhook_request already verified the signature and handed
    back the exact raw bytes it verified -- parsed here, never re-derived
    from a separately-bound Pydantic model, so what we process is
    guaranteed to be what was actually signed.

    Always returns 200 for a structurally valid, signed delivery -- even
    one we choose to skip (an out-of-scope event type, or a payload we
    genuinely can't process) -- since a non-2xx here just makes Luma retry
    a delivery that retrying can't fix. Only a missing Webhook-Id or
    malformed JSON/envelope (a payload shape Luma itself wouldn't send)
    returns 400.
    """
    if not webhook_id:
        raise HTTPException(status_code=400, detail="Missing Webhook-Id header.")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON body.")

    event_type = payload.get("type") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not event_type or not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Malformed webhook envelope.")

    try:
        await service.handle_webhook(event_type, data, webhook_delivery_id=webhook_id)
    except LumaSyncError as e:
        logger.warning(f"Luma webhook delivery skipped ({event_type}): {e}")
    return {"status": "ok"}


@router.post("/luma-backfill", response_model=LumaBackfillCheckpoint)
async def luma_backfill(resume: bool = True, service: LumaSyncService = Depends(get_luma_sync_service)):
    """
    Runs the one-time historical import: every event on the configured
    calendar, every guest on every event, through the exact same
    process_guest_event() path the live webhook uses. `resume` (default
    True) continues from the last durable checkpoint if a prior run was
    interrupted or failed; pass false to force a fresh run.
    """
    try:
        return await service.run_backfill(resume=resume)
    except LumaSyncError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Luma backfill failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Luma backfill failed -- see Activity Log / server logs for detail.")


# --- question mapping management (internal, session-authenticated only) ----


@mapping_router.get("", response_model=list[LumaQuestionMapping])
async def list_mappings(include_inactive: bool = True, service: LumaSyncService = Depends(get_luma_sync_service)):
    return await service.list_question_mappings(include_inactive=include_inactive)


@mapping_router.post("", response_model=LumaQuestionMapping)
async def create_mapping(req: LumaQuestionMappingCreateRequest, service: LumaSyncService = Depends(get_luma_sync_service)):
    try:
        return await service.create_question_mapping(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@mapping_router.patch("/{luma_question_mapping_id}", response_model=LumaQuestionMapping)
async def update_mapping(
    luma_question_mapping_id: str,
    req: LumaQuestionMappingUpdateRequest,
    service: LumaSyncService = Depends(get_luma_sync_service),
):
    patch = req.model_dump(exclude_unset=True)
    try:
        return await service.update_question_mapping(luma_question_mapping_id, patch)
    except LumaMappingNotFoundError:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@mapping_router.post("/{luma_question_mapping_id}/deactivate", response_model=LumaQuestionMapping)
async def deactivate_mapping(luma_question_mapping_id: str, service: LumaSyncService = Depends(get_luma_sync_service)):
    try:
        return await service.deactivate_question_mapping(luma_question_mapping_id)
    except LumaMappingNotFoundError:
        raise HTTPException(status_code=404, detail="Mapping not found.")
