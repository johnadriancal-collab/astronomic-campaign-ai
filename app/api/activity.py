"""
CRM Activity Log routes -- its own namespace under /crm/activity, same
"own the whole area" convention as app/api/sync.py. Two concerns live here:

1. GET /crm/activity -- read access to the log (list/filter/search/paginate).
   No PATCH/DELETE anywhere in this router: manual editing of logs is out of
   scope, and historical events must survive deletion of the entities they
   describe (see ActivityLogService's docstring).

2. POST /crm/activity/exports -- the one deliberate exception to "events are
   only ever emitted from a service layer write path." CSV export today is
   100% client-side (frontend/lib/csv-export.ts builds and downloads the
   file entirely in the browser from contacts already in React state) -- no
   backend endpoint is involved in producing the export at all, so there is
   no existing service-layer choke point to hook a `contacts.exported`
   event into. This route's only job is recording that an export happened;
   the frontend calls it AFTER the download has already been triggered
   (see the four export handlers in frontend/app/crm/*), and a failure here
   must never surface as an export error -- the file is already on the
   user's machine by the time this call is made.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies import get_activity_log_service
from app.models.activity import ActivityCategory, ActivityEventPage, ActivitySource
from app.services.activity_log_service import ActivityLogService

router = APIRouter(prefix="/crm/activity", tags=["activity"])

_EXPORT_SOURCE_LABELS: dict[str, str] = {
    "contacts": "Contacts",
    "more_filters": "More Filters",
    "astro_search": "Astro Search",
    "list": "a list",
}

_EXPORT_SOURCE_MAP: dict[str, ActivitySource] = {
    "contacts": ActivitySource.CONTACTS_PAGE,
    "more_filters": ActivitySource.MORE_FILTERS,
    "astro_search": ActivitySource.ASTRO_SEARCH,
    "list": ActivitySource.LISTS,
}


class CrmExportLogRequest(BaseModel):
    source: str  # "contacts" | "more_filters" | "astro_search" | "list"
    contact_count: int
    format: str = "csv"
    list_id: str | None = None
    list_name: str | None = None


@router.get("", response_model=ActivityEventPage)
async def list_activity_events(
    category: ActivityCategory | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
    service: ActivityLogService = Depends(get_activity_log_service),
):
    return await service.list_events(
        category=category, q=q, date_from=date_from, date_to=date_to, page=page, page_size=page_size
    )


@router.post("/exports")
async def log_export(req: CrmExportLogRequest, service: ActivityLogService = Depends(get_activity_log_service)):
    """Write-only, best-effort -- see this module's docstring. Always returns
    200; ActivityLogService.record() itself never raises, so there is no
    failure path for the frontend to handle here at all."""
    source_label = req.list_name and f'"{req.list_name}"' or _EXPORT_SOURCE_LABELS.get(req.source, req.source)
    count = req.contact_count
    await service.record(
        event_type="contacts.exported",
        category=ActivityCategory.EXPORTS,
        source=_EXPORT_SOURCE_MAP.get(req.source, ActivitySource.SYSTEM),
        summary=f'{count} contact{"s" if count != 1 else ""} exported from {source_label}.',
        entity_type="list" if req.list_id else None,
        entity_id=req.list_id,
        entity_name=req.list_name,
        metadata={"source": req.source, "contact_count": count, "format": req.format},
    )
    return {"status": "ok"}
