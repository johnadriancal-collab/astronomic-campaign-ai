"""
Astronomic Mail -- Phase 1 (Foundation) models.

Astronomic Mail is a NEW, standalone outbound-email feature living entirely
inside the CRM. It is deliberately independent from the existing Apollo-
oriented `Campaign`/`Lead`/`CampaignLead`/`EmailSequence`/`EmailMessage`
system (app/models/campaign.py, app/models/lead.py,
app/models/email_sequence.py, app/models/email_message.py) -- that system
configures and mirrors real Apollo sequences; Astronomic Mail never touches
Apollo, never touches Google, and (in this phase) has no sending capability
of any kind. Every new name here is prefixed `Mail*` specifically to avoid
any confusion with the existing `Campaign` word.

Phase 1 scope, deliberately narrow:
  - MailCampaign: a draft/ready/archived container referencing an existing
    CrmContactList by `list_id` (never a copy of its contacts).
  - MailSequenceStep: campaign-owned email steps, small whitelisted
    {{variables}} only, no template engine.
  - MailEnrollment: an explicit, one-time audience snapshot taken ONLY when
    a campaign transitions DRAFT -> READY (see mail_campaign_service.py's
    mark_ready() docstring for exactly when/why). Never a live view of list
    membership.
  - MailSuppression: a first-class, email-keyed suppression list -- the
    real enforcement mechanism for "never contact this address again",
    independent of CrmContact.email_status (which stays a free-text,
    ITF-only convention, untouched by this file).

Deliberately NOT modeled yet (see this phase's architecture report for why):
  - MailboxConfig / Mailbox -- no real mailbox exists until Phase 2 (OAuth).
    Nothing in Phase 1's actual UI needs a persisted mailbox row; the
    Mailboxes page is a static placeholder with no backend model at all.
  - MailSendJob / MailMessage -- no scheduler, no worker, no Gmail call
    exists yet, and their eventual shape depends on real Gmail integration
    testing (idempotency keys, deterministic Message-ID handling, claim/
    lock columns) that hasn't happened. Building these tables now would be
    unused production schema whose shape is likely to change once that
    testing starts -- exactly what this phase avoids.
  - `assigned_mailbox_id`/`current_step`/`next_send_at` on MailEnrollment --
    all three are meaningless without a real Mailbox row or a scheduler to
    advance them; adding a nullable column later, when they're actually
    needed, is a trivial, non-breaking addition (SQLite stores each row as
    a JSON blob here -- see the repository layer -- so no migration is
    required either).

Phase 1 contains NO code path capable of Gmail send, SMTP send, background
scheduling, or campaign activation. `MailCampaignStatus` intentionally does
NOT include ACTIVE/PAUSED/COMPLETED as enum members at all in this phase --
not merely "unreachable through the API" but structurally absent, so
`MailCampaignStatus("active")` itself raises. Adding those members later is
a one-line Python enum change with no schema migration (status is stored as
plain text).
"""

import re
from datetime import datetime, time
from enum import Enum

from pydantic import BaseModel, Field


class MailCampaignStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"


class MailCampaignSharing(str, Enum):
    """Who can see/edit this campaign -- a stored PREFERENCE only. This app
    has no multi-user/workspace permission system anywhere (no owner/user
    field exists on MailCampaign, CrmContactList, or any other model), so
    nothing anywhere enforces this value -- every campaign remains visible
    to every caller of GET /mail/campaigns regardless of this field. It is
    persisted now so the UI has a clean, honest place to record the user's
    intent for whenever a real permission system exists; see the Campaign
    Manager Integration Phase's Create Campaign modal for the UI."""

    EVERYONE = "everyone"
    ONLY_ME = "only_me"


class MailCampaign(BaseModel):
    """
    References its audience by `source_list_id` (an existing
    CrmContactList.list_id) -- never a duplicated copy of that list's
    contacts. `source_list_id` is nullable because campaign creation and
    audience selection are two separate UI steps (see the Phase 1 goal
    list): a brand-new DRAFT campaign legitimately has no audience yet.

    Editable ONLY while status == DRAFT (see mail_campaign_service.py's
    update_campaign()) -- once READY, the campaign's audience/sequence/
    schedule are locked because MailEnrollment rows have already been
    snapshotted against them; unlock_campaign() is the explicit escape
    hatch back to DRAFT (and clears that now-stale snapshot).

    Schedule fields (`sending_days`/`start_time`/`end_time`/`timezone`) are
    intentionally all independently nullable/empty -- a campaign can be
    saved mid-configuration. mark_ready() is what enforces they're ALL
    present and mutually valid before the campaign can leave DRAFT.

    `all_hours`/`start_immediately`/`daily_lead_start_limit` (Campaign
    Manager Integration Phase addition, see the Create Campaign modal) are
    all campaign-level CONFIGURATION/PREFERENCE fields, not capabilities --
    nothing in this codebase reads them to actually send, schedule, or
    progress anything yet:
      - `all_hours=True` means the campaign's sending window is the full
        day; mail_campaign_service.py enforces this by writing literal
        `start_time=00:00`/`end_time=23:59` whenever it's set True (see
        MailCampaignService.update_campaign()), so validate_mail_schedule()
        and every existing start/end-time consumer needs no changes at all
        -- `all_hours` itself exists only so the UI can round-trip the
        user's actual choice on re-edit, distinct from someone literally
        picking 00:00-23:59 by hand.
      - `start_immediately` is a preference for a future sending engine to
        read ("should a newly-enrolled lead begin immediately, once
        sending exists") -- it never changes `status` and never enables
        any send. MailCampaignStatus still has no ACTIVE member at all.
      - `daily_lead_start_limit` caps how many NEW leads may begin the
        sequence per day, once a future engine exists to enforce it. This
        is deliberately a DIFFERENT concept from a future per-mailbox daily
        send-volume limit (see MailCampaignReview.daily_capacity_estimate)
        -- one caps new starts, the other will cap total sends; they must
        never be collapsed into a single field.
    """

    mail_campaign_id: str
    name: str
    status: MailCampaignStatus = MailCampaignStatus.DRAFT

    source_list_id: str | None = None

    # 0=Monday .. 6=Sunday (Python's date.weekday() convention).
    sending_days: list[int] = Field(default_factory=list)
    start_time: time | None = None
    end_time: time | None = None
    timezone: str | None = None  # IANA identifier, e.g. "America/Chicago"
    all_hours: bool = False

    sharing: MailCampaignSharing = MailCampaignSharing.EVERYONE
    start_immediately: bool = False
    daily_lead_start_limit: int | None = None

    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None = None
    archived_at: datetime | None = None


class MailSequenceStepInput(BaseModel):
    """Request payload for creating/updating a step -- `step_number` is
    deliberately NOT accepted here; it's always assigned by the service
    (append-only at creation, explicit reorder_steps() to change order)."""

    subject: str
    body: str
    delay_days: int = 0
    reply_in_thread: bool = True


class MailSequenceStep(BaseModel):
    """
    Campaign-owned. `step_number` is 1-indexed and unique within a campaign
    (enforced at the DB layer -- see mail_sequence_step_store.py --
    matching CrmContactListMembership's composite-key precedent). Ordering
    is always by `step_number`, never by `created_at`, so a re-order is a
    real, deterministic, persisted change, not an artifact of insertion
    order.

    `subject`/`body` may contain a small whitelist of {{variable}}
    placeholders (see ALLOWED_MAIL_TEMPLATE_VARIABLES below) -- validated
    at write time by mail_campaign_service.py, never silently accepted.
    Nothing in Phase 1 ever RENDERS these placeholders against a real
    contact; that's test-send/send-queue scope (Phase 3+).

    `reply_in_thread` is captured now (cheap, and it's real authoring
    intent) but has zero behavioral effect until a worker exists to honor
    it -- see this module's docstring.
    """

    step_id: str
    mail_campaign_id: str
    step_number: int
    subject: str
    body: str
    delay_days: int = 0
    reply_in_thread: bool = True
    created_at: datetime
    updated_at: datetime


# --- {{variable}} whitelist -------------------------------------------------

ALLOWED_MAIL_TEMPLATE_VARIABLES = frozenset({"first_name", "last_name", "company"})
_MAIL_TEMPLATE_VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def find_unknown_mail_template_variables(text: str) -> list[str]:
    """Every `{{token}}` in `text` that is NOT in ALLOWED_MAIL_TEMPLATE_VARIABLES,
    sorted and deduplicated. Empty list means every placeholder found (if any)
    is safe. Never raises -- a plain string with no `{{...}}` at all returns []."""
    found = _MAIL_TEMPLATE_VARIABLE_PATTERN.findall(text or "")
    return sorted({v for v in found if v not in ALLOWED_MAIL_TEMPLATE_VARIABLES})


# --- Enrollment (audience snapshot) -----------------------------------------


class MailEnrollmentStatus(str, Enum):
    """Deliberately just these two in Phase 1 -- ACTIVE/COMPLETED/STOPPED/
    BOUNCED are all meaningless without a scheduler/worker (nothing ever
    advances a step, nothing ever sends, nothing ever bounces). Adding them
    in a later phase is a one-line enum change, no migration."""

    PENDING = "pending"
    SUPPRESSED = "suppressed"


class MailEnrollment(BaseModel):
    """
    One row per (mail_campaign_id, crm_contact_id) -- created ONLY by
    mail_campaign_service.mark_ready() snapshotting the campaign's
    source_list_id's CURRENT membership at that exact moment (see that
    method's docstring for the full "when exactly" rationale). Never
    created by viewing the Review screen, never created lazily, never
    updated to track a live list.

    `email_at_enrollment` is a frozen snapshot of the contact's email at
    snapshot time -- audit fidelity even if the contact's email field is
    edited afterward. Only contacts with a non-blank email are ever
    enrolled at all (nothing to ever address a message to otherwise).

    `status` starts SUPPRESSED (not PENDING) if the contact's email was
    already on MailSuppression at snapshot time -- an honest reflection of
    reality, not an exclusion from the row's existence. No sending logic
    reads this status in Phase 1; it exists purely for review/audit.
    """

    enrollment_id: str
    mail_campaign_id: str
    crm_contact_id: str
    email_at_enrollment: str
    status: MailEnrollmentStatus
    enrolled_at: datetime
    created_at: datetime


# --- Suppression -------------------------------------------------------------


class MailSuppressionReason(str, Enum):
    MANUAL = "manual"
    UNSUBSCRIBED = "unsubscribed"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"


class MailSuppression(BaseModel):
    """
    Exactly ONE row per normalized email address, ever -- `email_normalized`
    IS the primary key (see mail_suppression_store.py), reusing
    app.models.crm.normalize_email() for identical matching semantics to
    every other email-keyed lookup in this app. This is a deliberate,
    first-class safety mechanism, entirely independent of
    CrmContact.email_status (a free-text field with no enforced meaning --
    see that field's docstring in app/models/crm.py) -- suppression is
    checked by the address a message would be sent to, not by whatever a
    CRM contact record currently says, and it must work even for an
    address with no matching CrmContact at all.

    Suppressing an email that's already `active=True` is a pure no-op
    (idempotent, per this phase's explicit requirement) -- no duplicate
    row is ever created (impossible anyway, given the primary key).
    Unsuppressing never deletes the row -- it flips `active` to False and
    stamps `unsuppressed_at`, preserving the full audit trail (reason,
    original suppression time, who/when it was lifted) rather than losing
    history. Re-suppressing an inactive row reactivates it in place
    (updates reason/notes/timestamps) rather than erroring.
    """

    email_normalized: str
    reason: MailSuppressionReason
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    active: bool = True
    unsuppressed_at: datetime | None = None


# --- Review (pure, read-only calculation -- see mail_campaign_service.py) --


class MailCampaignReview(BaseModel):
    """
    The Review screen's entire data contract. Every field here is computed
    fresh, on every call, directly from the campaign's CURRENT
    source_list_id membership + CURRENT MailSuppression state + CURRENT
    sequence steps -- never from a MailEnrollment snapshot, and never
    written anywhere. See MailCampaignService.get_review()'s docstring for
    the exact, zero-mutation guarantee.
    """

    mail_campaign_id: str
    source_list_id: str | None
    source_list_name: str | None
    source_list_exists: bool

    total_contacts: int
    contacts_missing_email: int
    contacts_suppressed: int
    contacts_eligible: int

    sequence_step_count: int
    theoretical_total_sends: int  # contacts_eligible * sequence_step_count

    # None until Phase 2 introduces a real, persisted per-mailbox daily
    # send limit -- there is nothing to estimate against yet, and this
    # deliberately reports that honestly rather than fabricating a number.
    daily_capacity_estimate: int | None
    daily_capacity_note: str

    # Every real reason mark_ready() would currently refuse this campaign,
    # computed by the exact same shared check
    # (mail_campaign_service._compute_readiness_warnings) that mark_ready()
    # itself calls -- never a separately-maintained list. Empty means the
    # campaign is ready to be marked Ready right now.
    readiness_warnings: list[str] = Field(default_factory=list)


class MailScheduleValidationError(ValueError):
    """Raised by validate_mail_schedule() below -- a plain ValueError
    subclass (matching this codebase's existing convention of raising bare
    ValueError for simple domain validation, e.g. CrmService.create_contact)
    so callers can catch it specifically without changing normal
    ValueError-catching behavior elsewhere."""


def validate_mail_schedule(
    sending_days: list[int],
    start_time: time | None,
    end_time: time | None,
    timezone_name: str | None,
) -> None:
    """
    Strict, "is this schedule complete and usable" validation -- used as one
    of mark_ready()'s gates. NOT used to reject a merely-incomplete
    in-progress DRAFT edit (see update_mail_schedule_fields() below for the
    softer, per-field validation used there). Never touches the network or
    the database; pure.
    """
    if not sending_days:
        raise MailScheduleValidationError("At least one sending day is required.")
    if any(d < 0 or d > 6 for d in sending_days):
        raise MailScheduleValidationError("Sending days must be 0 (Monday) through 6 (Sunday).")
    if start_time is None or end_time is None:
        raise MailScheduleValidationError("Start and end time are both required.")
    if start_time >= end_time:
        raise MailScheduleValidationError("Start time must be before end time.")
    if not timezone_name:
        raise MailScheduleValidationError("A timezone is required.")
    validate_mail_timezone(timezone_name)


def validate_mail_timezone(timezone_name: str) -> None:
    """Isolated so update_campaign() can validate a timezone string in
    isolation (soft, per-field validation) without requiring every other
    schedule field to be present yet."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
        raise MailScheduleValidationError(f"'{timezone_name}' is not a valid IANA timezone.") from e


class MailListBulkAddResultLike(BaseModel):
    """Result of a mark_ready() enrollment snapshot -- deliberately named/shaped
    like CrmListBulkAddResult (app/models/crm.py) since it answers the same
    question ("how many rows were newly created vs already there") for the
    same reason: mark_ready() must be safely re-callable (e.g. after fixing a
    validation error) without ever duplicating an enrollment row."""

    enrolled: int
    already_enrolled: int
    suppressed_at_enrollment: int


class MailContactSuppressionStatus(BaseModel):
    """What GET /mail/suppressions/{email} and the CRM contact page's
    suppression badge render from -- deliberately a thin, purpose-built
    shape rather than exposing the raw MailSuppression row, so the contact
    page never needs to know about `active`/`unsuppressed_at` bookkeeping."""

    email_normalized: str
    suppressed: bool
    reason: MailSuppressionReason | None
    notes: str | None
    created_at: datetime | None
    unsuppressed_at: datetime | None
