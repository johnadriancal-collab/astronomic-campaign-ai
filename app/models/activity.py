"""
Models for the CRM Activity Log -- a persistent, append-only trail of
meaningful create/change/delete/export/automation actions (ITF intake, CRM
contact CRUD, CSV import, Lists, Campaigns). Deliberately excludes ordinary
read-only activity (search, view, pagination, Astro refinements) -- see
ActivityLogService's docstring in app/services/activity_log_service.py for
the exact line and every emission call site.

`actor` is always None today: no authenticated-user system exists anywhere
in this app, and this feature does not invent one. The field exists purely
so a real identity can be attached later (once real auth exists) without a
schema migration -- populating an already-optional column, not adding one.

`entity_name` is a snapshot taken at write time, not a live join against the
entity's current record -- this is what keeps a "List deleted" event
readable after the list itself is gone (same "snapshot, not live reference"
instinct as CrmContact.source_snapshot).
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActivityCategory(str, Enum):
    ITF = "itf"
    CONTACTS = "contacts"
    IMPORTS = "imports"
    LISTS = "lists"
    EXPORTS = "exports"
    CAMPAIGNS = "campaigns"
    EMAIL_INTAKE = "email_intake"
    ERRORS = "errors"
    # Astronomic Mail (app/models/mail.py) -- deliberately its own category,
    # never CAMPAIGNS, so its event stream is never confused with the
    # existing Apollo-oriented Campaign Manager's events.
    MAIL = "mail"
    # Luma (lu.ma) event-registration sync -- app/services/luma_sync_service.py.
    LUMA = "luma"


class ActivitySource(str, Enum):
    """WHERE an action originated -- distinct from `actor` (WHO). Exists so the
    feed can distinguish "a human editing Contacts" from "the ITF automation"
    from "the CSV importer" etc. even with no real user identity available."""

    MANUAL_CRM = "manual_crm"
    ITF_AUTOMATION = "itf_automation"
    CSV_IMPORT = "csv_import"
    LISTS = "lists"
    CONTACTS_PAGE = "contacts_page"
    MORE_FILTERS = "more_filters"
    ASTRO_SEARCH = "astro_search"
    CAMPAIGN_SYSTEM = "campaign_system"
    EMAIL_INTAKE = "email_intake"
    SYSTEM = "system"
    # Astronomic Mail -- distinct from CAMPAIGN_SYSTEM (Apollo).
    MAIL_SYSTEM = "mail_system"
    # Astro AI's chat-driven CRM export (app/services/astro_crm_tools.py) --
    # distinct from ASTRO_SEARCH, which is a different, deterministic
    # Claude-free CRM query feature. Using ASTRO_SEARCH here would
    # misattribute Astro AI's exports to that unrelated feature.
    ASTRO_AI = "astro_ai"
    # Luma (lu.ma) event-registration sync (webhook-triggered or backfill) --
    # app/services/luma_sync_service.py.
    LUMA_SYNC = "luma_sync"


class ActivityEvent(BaseModel):
    event_id: str
    event_type: str  # dot-namespaced, e.g. "list.contacts_added" -- see the taxonomy table
    category: ActivityCategory
    created_at: datetime
    source: ActivitySource
    actor: str | None = None
    entity_type: str | None = None  # "contact" | "list" | "campaign" | "import_batch" | None
    entity_id: str | None = None
    entity_name: str | None = None
    summary: str  # pre-rendered human sentence -- the feed never reconstructs this from metadata
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityEventPage(BaseModel):
    items: list[ActivityEvent]
    total: int
    page: int
    page_size: int
