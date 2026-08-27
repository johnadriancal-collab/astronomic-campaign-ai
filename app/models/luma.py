"""
Luma (lu.ma) event-registration sync models. Deliberately separate from
CrmContact -- a Luma registration is event-history data, not contact data
(see app/services/luma_sync_service.py's module docstring): one CrmContact
may have many LumaRegistrations (one per event they registered for), and a
LumaRegistration links to at most one CrmContact.

`LumaEvent`/`LumaRegistration` fields are limited to what Luma's API
actually documents (confirmed live against docs.luma.com) -- nothing here
is invented. Notably absent on purpose: an `approved_at` timestamp (Luma's
Guest schema has no such field -- approval is a status transition with no
dedicated timestamp) and a single canonical event "status" enum (Luma's
Event schema doesn't expose one; `status` here is best-effort/optional).

Registration answers stay structured JSON (`LumaRegistrationAnswer`) --
never collapsed into a CRM notes field or a single blob -- so an unmapped
question is still fully preserved, queryable later, and auditable.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LumaApprovalStatus(str, Enum):
    """Luma's `approval_status` enum, verbatim from their OpenAPI schema.
    `SESSION` appears only in the raw schema (never in Luma's own prose
    docs) -- treated as a valid-but-undocumented value, never rejected."""

    APPROVED = "approved"
    PENDING_APPROVAL = "pending_approval"
    INVITED = "invited"
    WAITLIST = "waitlist"
    DECLINED = "declined"
    SESSION = "session"


class LumaMatchStatus(str, Enum):
    """Whether this registration is confidently linked to a CrmContact.
    NEEDS_REVIEW means CrmService.classify_match() returned
    POSSIBLE_DUPLICATE -- crm_contact_id is deliberately left null rather
    than guessing (see luma_sync_service.py)."""

    MATCHED = "matched"
    NEEDS_REVIEW = "needs_review"


class LumaRegistrationAnswer(BaseModel):
    """One entry from Luma's `registration_answers[]` -- kept as-is
    (label/question_id/question_type/value), not reshaped, so an unmapped
    question is preserved exactly as Luma sent it. `value` is deliberately
    `Any`: a string, a list[str] (multi-select), or a dict (the `company`
    question type's {company, job_title} shape) depending on question_type."""

    question_id: str | None = None
    label: str
    question_type: str
    value: Any = None


class LumaEventTicket(BaseModel):
    """One entry from Luma's `event_tickets[]` -- only the fields the sync
    actually uses (check-in derivation, basic ticket identity)."""

    id: str
    name: str | None = None
    checked_in_at: datetime | None = None


class LumaEvent(BaseModel):
    luma_event_id: str
    calendar_id: str | None = None
    name: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str | None = None  # best-effort/optional -- see module docstring
    location_summary: str | None = None
    url: str | None = None
    synced_at: datetime
    updated_at: datetime


class LumaRegistration(BaseModel):
    luma_guest_id: str  # Luma's "gst-..." id -- the real identity/idempotency key
    luma_event_id: str
    crm_contact_id: str | None = None  # null while match_status == NEEDS_REVIEW
    email_normalized: str | None = None
    match_status: LumaMatchStatus = LumaMatchStatus.MATCHED
    approval_status: LumaApprovalStatus
    registered_at: datetime | None = None
    invited_at: datetime | None = None
    joined_at: datetime | None = None
    checked_in_at: datetime | None = None  # derived: earliest non-null event_tickets[].checked_in_at
    utm_source: str | None = None
    registration_answers: list[LumaRegistrationAnswer] = Field(default_factory=list)
    event_tickets: list[LumaEventTicket] = Field(default_factory=list)
    last_webhook_delivery_id: str | None = None  # Luma's "Webhook-Id" header -- exact-delivery dedup
    synced_at: datetime
    updated_at: datetime


class LumaAnswerNormalizer(str, Enum):
    """A small, closed set of SAFE, narrowly-typed value transforms a
    mapping can request -- deliberately NOT a generic transformation/
    execution engine (no arbitrary code, no user-supplied expressions).
    See app/services/luma_answer_normalizers.py for the actual (pure,
    unit-tested) implementation each member dispatches to."""

    LINKEDIN_URL = "linkedin_url"


class LumaQuestionMapping(BaseModel):
    """Configurable label -> CRM field mapping (never hardcoded in
    application logic). `extract_key` handles Luma's non-scalar question
    types (currently just `company`, whose value is {company, job_title})
    -- when set, the mapped value is `answer.value[extract_key]` rather
    than `answer.value` itself. `normalizer`, when set, additionally runs
    the (possibly extract_key'd) value through one of the closed set of
    LumaAnswerNormalizer transforms before it's applied to the contact --
    e.g. turning Luma's bare "/in/example" LinkedIn answer into a real
    "https://www.linkedin.com/in/example" URL. Applied AFTER extract_key,
    so the two compose (extract, then normalize the extracted value)."""

    luma_question_mapping_id: str
    question_label: str  # matched case-insensitively against registration_answers[].label
    question_type: str | None = None  # optional extra guard; None matches any question_type
    target_field_key: str  # a core/thesis field name, or "custom:<key>"
    extract_key: str | None = None  # e.g. "company" or "job_title" for question_type="company"
    normalizer: LumaAnswerNormalizer | None = None
    active: bool = True
    created_at: datetime
    updated_at: datetime


class LumaQuestionMappingCreateRequest(BaseModel):
    question_label: str
    question_type: str | None = None
    target_field_key: str
    extract_key: str | None = None
    normalizer: LumaAnswerNormalizer | None = None
    active: bool = True


class LumaQuestionMappingUpdateRequest(BaseModel):
    """All fields optional -- a PATCH. Only fields actually present in the
    request body are applied (see app/api/luma.py's use of
    `model_dump(exclude_unset=True)`), so explicitly setting e.g.
    extract_key to null is distinguishable from simply not mentioning it."""

    question_label: str | None = None
    question_type: str | None = None
    target_field_key: str | None = None
    extract_key: str | None = None
    normalizer: LumaAnswerNormalizer | None = None
    active: bool | None = None


class LumaSyncCounts(BaseModel):
    """Aggregate counters for one sync run (webhook batch, reconciliation,
    or backfill) -- what luma.sync.completed's Activity Log metadata holds.
    Never per-registration detail."""

    events_processed: int = 0
    registrations_created: int = 0
    registrations_updated: int = 0
    contacts_created: int = 0
    contacts_enriched: int = 0
    needs_review: int = 0
    errors: int = 0


class LumaBackfillStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LumaBackfillCheckpoint(BaseModel):
    """Durable resume state for the one-time historical backfill -- single
    row (checkpoint_id is always "default", single-calendar scope) so a
    crash/restart resumes from the last successfully-completed page rather
    than restarting from zero. Safe to rerun regardless: LumaRegistration's
    own guest_id-keyed upsert makes reprocessing idempotent even without
    this checkpoint -- this exists for efficiency, not correctness."""

    checkpoint_id: str = "default"
    status: LumaBackfillStatus = LumaBackfillStatus.NOT_STARTED
    event_cursor: str | None = None  # next calendars/events/list cursor to resume from
    in_progress_event_id: str | None = None
    in_progress_guest_cursor: str | None = None
    counts: LumaSyncCounts = Field(default_factory=LumaSyncCounts)
    started_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
