"""
Models for the Automated ITF (Investor Thesis Form) Contact Intake pipeline.
A Google Apps Script bound to the ITF response Sheet POSTs each new
submission (via an installable onFormSubmit trigger) to POST
/sync/itf-contact, which hands it to CrmImportService.import_one_row() --
see app/services/itf_ingestion_service.py for the header-disambiguation/
column-mapping/idempotency plumbing between the two. Nothing here is read
by, or writes to, Campaign/Lead/CampaignLead/EmailSequence/EmailMessage.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ItfRowStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    POSSIBLE_DUPLICATE = "possible_duplicate"  # never written -- always human-reviewed
    ALREADY_PROCESSED = "already_processed"  # idempotency: same content_hash already processed
    ERROR = "error"


class ItfIngestionLogEntry(BaseModel):
    """
    One submission's ingestion outcome -- the idempotency ledger. Keyed on
    row_number (the Sheet row the Apps Script trigger reported via
    e.range.getRow() -- stable, since rows are only ever appended, never
    reordered, by a Google Form). A submission is treated as already
    processed only when a log entry already exists for its row_number with
    status != error AND a matching content_hash -- this correctly resumes
    after a crash (unfinished rows have no log entry, so they're retried),
    automatically retries a previously failed row (status=error never blocks
    reprocessing), and safely reprocesses if the same row is somehow
    resubmitted with different content. Never written during a dry run.
    """

    row_number: int
    content_hash: str
    status: ItfRowStatus
    response_id: str | None = None
    crm_contact_id: str | None = None
    email: str | None = None
    error_message: str | None = None
    processed_at: datetime


class ItfWebhookRequest(BaseModel):
    """
    POST /sync/itf-contact's request body -- sent by the Apps Script bridge
    on every onFormSubmit(e) firing. `headers`/`values` are positional,
    parallel arrays (Apps Script's e.values plus a one-time read of the
    Sheet's header row), NOT a header->value dict -- the ITF Form asks
    several IDENTICALLY-worded questions once for its private section and
    once for its institutional section, so a plain dict (or Apps Script's own
    e.namedValues) would silently collide and lose one section's answer.
    Header disambiguation happens server-side (see
    itf_ingestion_service._disambiguate_headers) so Apps Script needs zero
    knowledge of which questions repeat.
    """

    source: str = "itf"
    row_number: int = Field(gt=0)
    response_id: str | None = None
    headers: list[str] = Field(min_length=1)
    values: list[str] = Field(default_factory=list)


class ItfWebhookResult(BaseModel):
    """
    POST /sync/itf-contact's response body. `status` mirrors ItfRowStatus's
    values exactly (created/updated/possible_duplicate/already_processed/
    error) -- Apps Script branches on this string. `mapped_fields` is
    populated only when `dry_run=True` (the classified CRM fields this
    submission WOULD write) -- omitted in a real run to keep the response
    small and avoid echoing contact PII back through a channel (Apps
    Script's execution log) that isn't meant to store it.
    """

    status: str
    dry_run: bool
    contact_id: str | None = None
    matched_on: str | None = None  # "email" | "apollo_contact_id" | "linkedin_url" | "name_company" | None
    source: str = "itf"
    mapped_fields: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
