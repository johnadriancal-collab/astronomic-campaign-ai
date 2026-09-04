"""
Astronomic Mail -- Phase A (Durable Execution Model) models.

Astronomic Mail is a NEW, standalone outbound-email feature living entirely
inside the CRM. It is deliberately independent from the existing Apollo-
oriented `Campaign`/`Lead`/`CampaignLead`/`EmailSequence`/`EmailMessage`
system (app/models/campaign.py, app/models/lead.py,
app/models/email_sequence.py, app/models/email_message.py) -- that system
configures and mirrors real Apollo sequences; Astronomic Mail never touches
Apollo. Every new name here is prefixed `Mail*` specifically to avoid any
confusion with the existing `Campaign` word.

Phase C (later than the rest of this docstring's Phase A narrative, see
below) added a real execution worker and Gmail sender -- Astronomic Mail
CAN send real email today through a connected/gmail.send-authorized
mailbox once a campaign is activated. The rest of this docstring
describes Phase A's own scope accurately as of when Phase A shipped; see
app/services/mail_execution_worker.py and app/services/mail_sending_service.py
for the current, real sending path.

Phase A adds a durable execution model on top of Phase 1's configuration
shell (MailCampaign/MailSequenceStep/MailEnrollment/MailSuppression,
unchanged in spirit, extended below) and Phase 2's mailbox connection
(app/models/mailbox.py):
  - MailCampaignStatus gains ACTIVE/PAUSED/COMPLETED -- see that enum's own
    docstring for the full state machine and exactly what each status does
    and does not permit.
  - MailEnrollmentStatus gains ACTIVE/COMPLETED/PAUSED/FAILED, and
    MailEnrollment gains `assigned_mailbox_id` -- the sticky sender chosen
    once execution genuinely begins (see MailSendingService).
  - MailEnrollmentStep (new): one durable row per (enrollment, sequence
    step) actually being executed -- see its own docstring for the full
    state machine, the content-snapshot strategy, and the provider-call
    uncertainty boundary this exists specifically to make safe.
  - MailboxSendPolicy (app/models/mailbox.py): per-mailbox sending-safety
    configuration (daily cap, minimum pacing) -- kept separate from
    MailCampaign.daily_lead_start_limit, which is a DIFFERENT concept (see
    that field's own docstring for why the two must never be collapsed).

At the time Phase A shipped, this app still contained no code path capable
of Gmail send, SMTP send, or any real message delivery -- MailSenderPort
(app/services/mail_sending_service.py) was an abstract contract with no
implementation, no background worker existed, and no OAuth scope beyond
`openid email profile` had been requested. Phase C later added a real
implementation (GmailSender), a Railway worker process
(MailExecutionWorker), and the `gmail.send` scope (requested per-mailbox
via MailboxService.begin_gmail_send_upgrade(), see app/models/mailbox.py).
Activating a campaign (MailCampaignService.activate_campaign()) now can
lead to real sends: a QUEUED MailEnrollmentStep row is genuinely polled
and processed by the worker, subject to the mailbox/recipient
controlled-test allowlist gate (app/services/mail_sending_service.py's
controlled_test_send_allowed()) while that gate remains configured.
"""

import re
from datetime import datetime, time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class MailCampaignStatus(str, Enum):
    """
    DRAFT -> READY -> ACTIVE <-> PAUSED, with ARCHIVED reachable from any
    non-archived status (terminal, no un-archive). See
    MailCampaignService.activate_campaign()'s docstring for the full state
    machine and exactly what each status means:

      READY:     configuration validated + audience snapshotted. Does NOT
                 mean execution is allowed -- this is a deliberate,
                 permanent distinction, not a temporary Phase A gap. A
                 READY campaign never sends anything, by construction, same
                 as DRAFT.
      ACTIVE:    execution is allowed. The ONLY way to reach this is the
                 explicit activate_campaign() transition -- mark_ready()
                 itself never changes status past READY, and nothing here
                 auto-activates a campaign. A campaign is a PERSISTENT
                 container, not a one-time batch (Phase 2, 2026-09-03):
                 ACTIVE stays ACTIVE regardless of how much work is
                 currently queued -- an ACTIVE campaign with zero pending
                 enrollments is still ACTIVE, ready for more prospects to
                 be added, not "done." Workload (pending/in-progress/
                 completed/suppressed/failed counts) is tracked
                 independently of this status field, never inferred from
                 it.
      PAUSED:    execution temporarily halted (manual pause, or an assigned
                 mailbox becoming unavailable -- see MailEnrollmentStatus.
                 PAUSED). Configuration remains just as locked as ACTIVE --
                 pausing execution is not the same as unlocking
                 configuration; see activate_campaign()'s docstring for why
                 there is no PAUSED/ACTIVE -> DRAFT path in this phase.
      COMPLETED: LEGACY ONLY as of Phase 2 (2026-09-03) -- no code path
                 writes this anymore (MailSendingService.
                 maybe_complete_campaign(), the only thing that ever did,
                 was removed entirely: exhausting an ACTIVE campaign's
                 current workload no longer auto-transitions it anywhere,
                 by design -- see ACTIVE's own docstring above). Existing
                 campaigns already in this status from before Phase 2 are
                 NOT migrated/rewritten and remain readable exactly as
                 they were; explicitly adding a new prospect batch to one
                 is the only way to reopen it, back to ACTIVE, and once
                 reopened it behaves like any other persistent ACTIVE
                 campaign (never auto-returns here).
    """

    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
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
        MailCampaignService.update_campaign()) -- `all_hours` itself exists
        only so the UI can round-trip the user's actual choice on re-edit,
        distinct from someone literally picking 00:00-23:59 by hand. These
        four legacy schedule fields (plus `sending_days`/`start_time`/
        `end_time`) are superseded, but NOT removed, by the real
        MailSendWindow rows a campaign gets once it's edited through the
        Schedule tab -- see MailSendWindow's docstring below for the exact
        "windows if present, else synthesized from these fields" rule.
      - `start_immediately` is a preference for a future sending engine to
        read ("should a newly-enrolled lead begin immediately, once
        sending exists") -- it never changes `status` and never enables
        any send. MailCampaignStatus still has no ACTIVE member at all.
      - `daily_lead_start_limit` caps how many NEW leads may begin the
        sequence per campaign per day -- specifically, how many Step 1
        messages may successfully SEND per campaign-local calendar day (see
        MailSendingService's runtime safety checks). Follow-up steps
        (Step 2+) never consume this limit, no matter how many send on a
        given day. This is deliberately a DIFFERENT concept from
        MailboxSendPolicy.daily_send_limit (app/models/mailbox.py) -- a
        per-MAILBOX cap on ALL messages that mailbox sends, Step 1 and
        follow-ups alike, across every campaign using it, on a UTC calendar
        day. One protects a campaign's pacing of new starts; the other
        protects a single Gmail account's sending reputation regardless of
        which campaign(s) route through it. They must never be collapsed
        into a single field, and a campaign activating with a generous
        `daily_lead_start_limit` can still be fully throttled by a
        stricter mailbox-level `daily_send_limit` -- both apply
        independently.
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

    # Trigger feature foundation (Stage 5A, 2026-09-04) -- see
    # MailLeadStartTrigger's own docstring for the full feature. Both
    # fields default such that an old, already-persisted campaign (this
    # model is stored as a JSON blob -- see SQLiteMailCampaignStore --so an
    # existing row simply lacks these two keys) deserializes as
    # "immediate"/None: exactly today's behavior, unchanged, with zero
    # migration step required. Nothing in this codebase branches on
    # `lead_start_mode` yet (Stage 5A is deliberately execution-inert --
    # see mail_campaign_service.py's lifecycle methods for the only
    # current writers of `execution_active_since`); it exists now purely
    # so the schema is stable ahead of Stage 5C's actual gating.
    #
    # `lead_start_mode`: "immediate" (today's only real behavior -- every
    # PENDING enrollment starts eagerly at activation / Add Prospects) or
    # "triggered" (a later stage's gate, once at least one real
    # MailLeadStartTrigger exists for this campaign -- see the Trigger
    # design report for why this is a durable, one-way field rather than
    # inferred from "is the trigger list currently non-empty").
    lead_start_mode: Literal["immediate", "triggered"] = "immediate"

    # `execution_active_since`: the start of this campaign's CURRENT,
    # continuous ACTIVE streak -- an authoritative execution-state field, a
    # deliberate alternative to reading Activity Log (audit/observability
    # data, not authoritative execution state -- some logging paths in
    # this system are intentionally best-effort). Maintained ONLY by the
    # campaign lifecycle transition methods below, nowhere else (ordinary
    # edits -- e.g. Channels, which stays editable through ACTIVE/PAUSED --
    # never touch it):
    #   READY -> ACTIVE (activate_campaign):                 set to now
    #   PAUSED -> ACTIVE (resume_campaign):                   set to now
    #   legacy COMPLETED -> ACTIVE (add_prospects() reopen):  set to now
    #   ACTIVE -> PAUSED (pause_campaign):                    cleared to None
    #   ACTIVE/PAUSED -> ARCHIVED (archive_campaign):         cleared to None
    #   DRAFT, READY, and a not-yet-reopened legacy COMPLETED campaign
    #   simply never have it set: None.
    # A future Trigger occurrence scheduler uses this as its floor for
    # "does this scheduled occurrence belong to the campaign's CURRENT
    # active period" -- not implemented in Stage 5A.
    execution_active_since: datetime | None = None

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
    """
    PENDING: snapshotted, not yet begun executing (the campaign hasn't been
      activated yet, or was already suppressed at snapshot time -- see
      below). The only status a brand-new enrollment can start in.
    ACTIVE: currently progressing through the sequence -- set the moment
      MailCampaignService.activate_campaign() creates this enrollment's
      Step 1 MailEnrollmentStep row (PENDING enrollments only; an
      already-SUPPRESSED one is never touched).
    PAUSED: execution blocked for this ONE enrollment specifically (its
      assigned mailbox became disconnected/needs_reauth/no longer selected
      for the campaign) -- never silently reassigned to a different sender;
      see MailEnrollment.assigned_mailbox_id's own docstring. A
      campaign-wide pause is a SEPARATE thing (MailCampaignStatus.PAUSED)
      and does not, by itself, change any individual enrollment's status.
    COMPLETED: every MailEnrollmentStep row for this enrollment reached a
      terminal status (SENT/SKIPPED_SUPPRESSED/FAILED) AND there is no
      further MailSequenceStep left to materialize -- see
      MailSendingService.record_send_success()'s docstring for exactly how
      this is determined; it is never inferred merely from "every
      currently-materialized row is terminal" (a step further along the
      sequence may not have been created yet, since Step N+1's row is only
      created once Step N sends -- see MailEnrollmentStep's own docstring).
    SUPPRESSED: this recipient's email is (or became) suppressed -- checked
      both at mark_ready() snapshot time (the original Phase 1 behavior,
      unchanged) AND live, immediately before every send attempt, once a
      real worker exists (Phase C). Terminal -- no further step is ever
      materialized once set.
    FAILED: reserved for a genuinely unrecoverable enrollment (not wired to
      anything automatic in Phase A -- kept in the enum now so adding real
      logic that sets it later is a one-line change, not an enum migration).
    """

    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


class MailEnrollmentPauseReason(str, Enum):
    """Why a PAUSED MailEnrollment is paused -- see that field's own
    docstring on MailEnrollment for why this exists at all (distinguishing
    automatically-recoverable pauses from ones that aren't, and further
    distinguishing WHICH automatic-recovery signal -- if any -- applies).
    Adding a further reason later is still a one-line change, not a
    redesign.

    MAILBOX_UNAVAILABLE: the enrollment's sticky assigned_mailbox_id is no
      longer selected/CONNECTED/scoped-for-sending (NEEDS_REAUTH,
      DISCONNECTED, removed from the campaign's Channels, or missing the
      gmail.send grant) -- see MailSendingService.prepare_and_send_step()'s
      mailbox-availability checks (both the mid-flow one and the final,
      fresh pre-SENDING recheck). Auto-resumed ONLY by
      MailSendingService.resume_mailbox_paused_enrollments(): a CONCRETE,
      directly-observable state transition (the SAME mailbox_id, never
      reassigned, becoming usable again) makes this safe to lift
      automatically.

    PREPARE_CONFIG_BLOCKED: message preparation (unsubscribe composition)
      could not complete because a known, deterministic operator
      configuration precondition is currently missing (the unsubscribe
      token encryption key, or the public backend origin). Auto-resumed
      ONLY by MailSendingService.resume_prepare_config_blocked_
      enrollments() -- and only when BOTH of those settings currently
      read as configured (a cheap, local, no-network presence check --
      see that function's own docstring). Unlike MAILBOX_UNAVAILABLE,
      this is a GLOBAL application setting, not a per-mailbox one, so the
      sweep resumes every such enrollment together once the prerequisite
      is satisfied -- but critically, it does NOT resume ANY of them
      while the prerequisite remains unsatisfied, unlike the pre-
      hardening-pass behavior this reason replaces.

    PREPARE_TRANSIENT_EXHAUSTED: a PREPARE-phase failure believed
      transient (e.g. a token-endpoint network blip) recurred past its
      bounded retry budget (see MailSendingService.
      PREPARE_TRANSIENT_MAX_ATTEMPTS and MailEnrollmentStep.
      prepare_failure_count). Deliberately has NO automatic recovery
      path: there is no cheap, safe, generic signal that proves a
      transient condition has genuinely stopped recurring, and blindly
      granting a fresh retry budget on every periodic sweep would just
      turn "bounded" into "unbounded, one exhaustion cycle at a time" --
      exactly the bug this reason exists to prevent. Recoverable ONLY via
      MailSendingService.resolve_prepare_blocked_step() -- an explicit,
      authenticated action; see that method's own docstring.

    PREPARE_UNCLASSIFIED_BLOCKED: a PREPARE-phase failure that didn't
      match any recognized category. Treated exactly as conservatively
      as PREPARE_TRANSIENT_EXHAUSTED for the same reason: with no
      specific signal to check, periodic blind resume is not safe.
      Recoverable ONLY via MailSendingService.resolve_prepare_blocked_
      step(), same as PREPARE_TRANSIENT_EXHAUSTED."""

    MAILBOX_UNAVAILABLE = "mailbox_unavailable"
    PREPARE_CONFIG_BLOCKED = "prepare_config_blocked"
    PREPARE_TRANSIENT_EXHAUSTED = "prepare_transient_exhausted"
    PREPARE_UNCLASSIFIED_BLOCKED = "prepare_unclassified_blocked"


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
    reality, not an exclusion from the row's existence. See
    MailEnrollmentStatus's own docstring for the full state machine
    (PENDING -> ACTIVE -> COMPLETED, with PAUSED/SUPPRESSED as the two
    reasons execution can stop early).

    `assigned_mailbox_id` is the STICKY sender for this enrollment's entire
    sequence -- assigned LAZILY, once, the first time this enrollment's
    Step 1 execution is actually claimed for sending (never at mark_ready()
    snapshot time, and never at campaign activation time either -- see
    MailSendingService.assign_mailbox_if_needed()). Every later step for
    this same enrollment reuses it unconditionally, for real thread/sender
    continuity. Never silently reassigned: if this mailbox later becomes
    disconnected/needs_reauth/unselected from the campaign's Channels,
    this enrollment moves to PAUSED instead -- see that status's own
    docstring.

    `paused_reason` (Phase C addition): WHY this enrollment is currently
    PAUSED, if it is -- None whenever status != PAUSED. Added specifically
    so an automatic recovery sweep (MailSendingService.
    resume_mailbox_paused_enrollments()) can resume ONLY the enrollments it
    actually knows how to safely resume (today: exactly
    MAILBOX_UNAVAILABLE, whose same sticky assigned_mailbox_id becoming
    CONNECTED again is a fully understood, safe-to-automate recovery) and
    never touch a PAUSED enrollment for any OTHER (including future,
    not-yet-invented) reason. Without this field, "all PAUSED enrollments"
    would be indistinguishable from "PAUSED enrollments it's safe to
    auto-resume" -- a real correctness gap, not a hypothetical one, since
    a future pause reason could easily need a human decision instead.

    `batch_id` (Phase 2, 2026-09-03): which MailEnrollmentBatch this row
    came from, if any -- None for every enrollment created before this
    field existed (mark_ready()'s original one-time snapshot, still the
    ONLY way a campaign's very first cohort is created) and for any
    enrollment created by a mark_ready() call going forward, which still
    doesn't stamp a batch -- ONLY MailCampaignService.add_prospects()
    (added prospects on an already-persistent campaign) does. A legacy row
    with no batch_id is not an error or a gap to backfill -- see
    MailEnrollmentBatch's own docstring for why no backfill is planned."""

    enrollment_id: str
    mail_campaign_id: str
    crm_contact_id: str
    email_at_enrollment: str
    status: MailEnrollmentStatus
    enrolled_at: datetime
    created_at: datetime
    assigned_mailbox_id: str | None = None
    paused_reason: MailEnrollmentPauseReason | None = None
    batch_id: str | None = None


# --- Prospect batches (Phase 2, 2026-09-03) ----------------------------------


class MailEnrollmentBatchSource(str, Enum):
    """WHERE a MailEnrollmentBatch's contacts came from."""

    CRM_LIST = "crm_list"
    CSV_UPLOAD = "csv_upload"


class MailEnrollmentBatchStatus(str, Enum):
    """PREPARING -> READY, the only two V1 values -- deliberately no
    terminal FAILED state (see MailCampaignService._reconcile_batch()'s
    own docstring): every failure mode this batch lifecycle can hit is
    recoverable by re-running the same idempotent steps, so "stuck in
    PREPARING" IS the recoverable-failure representation, not a gap
    needing a separate state."""

    PREPARING = "preparing"
    READY = "ready"


class MailEnrollmentBatch(BaseModel):
    """
    Provenance record for one call to MailCampaignService.add_prospects()
    -- a campaign is a PERSISTENT container (Phase 2): unlike the one-time
    mark_ready() snapshot, prospects can be added to an already-ACTIVE or
    PAUSED campaign (or a legacy COMPLETED one, reopening it -- see
    add_prospects()'s own docstring) any number of times, and each such
    call gets exactly one of these rows, immutable once created.

    Mirrors CrmImportBatch's existing provenance shape deliberately (batch
    id, source, counts) rather than inventing a new convention.

    Count semantics (see add_prospects()'s docstring for the exact
    algorithm that produces them):
      submitted_count       = unique candidate contacts submitted to
                               campaign enrollment AFTER resolving the
                               source (list membership or CSV commit
                               result) and deduplicating within the batch
                               itself.
      enrolled_count         = genuinely NEW MailEnrollment rows created
                               by this call (enrollment_store.create()
                               returned True).
      already_enrolled_count = candidates skipped because this campaign
                               already contained them -- at ANY status,
                               including a long-COMPLETED enrollment; see
                               add_prospects()'s dedup rules.
      suppressed_count       = the SUBSET of enrolled_count (not an
                               additional, separate population) whose
                               email was already suppressed, so it entered
                               directly as SUPPRESSED rather than PENDING
                               -- same convention as mark_ready()'s own
                               suppressed_at_enrollment counter. Always
                               true: suppressed_count &lt;= enrolled_count,
                               and enrolled_count + already_enrolled_count
                               == submitted_count. enrolled_count counts
                               BOTH ENROLLED_SUPPRESSED and PREPARED
                               members (see MailEnrollmentBatchMemberState)
                               -- every genuinely new MailEnrollment this
                               batch produced, regardless of whether a
                               Step 1 was ever materialized for it.

    submitted_count/enrolled_count/already_enrolled_count/suppressed_count
    are all None while status == PREPARING -- submitted_count IS known the
    instant the cohort is frozen (see MailEnrollmentBatchMember's own
    docstring on why members are written BEFORE this row), but is kept
    None here rather than written early and left to look "final" before
    it truly is; MailCampaignService._reconcile_batch() is the only writer
    of this whole object, and it fills in all four counts atomically,
    together, at the same moment it flips status to READY (never a
    partial write of some counts but not others).

    No backfill: every MailEnrollment created before this model existed
    has batch_id=None and no corresponding MailEnrollmentBatch row --
    deliberately left as valid, permanent legacy data (see
    MailEnrollment.batch_id's own docstring)."""

    batch_id: str
    mail_campaign_id: str
    source: MailEnrollmentBatchSource
    source_list_id: str | None = None
    source_import_batch_id: str | None = None
    idempotency_key: str
    status: MailEnrollmentBatchStatus
    created_at: datetime
    created_by_actor: str | None = None
    submitted_count: int | None = None
    enrolled_count: int | None = None
    already_enrolled_count: int | None = None
    suppressed_count: int | None = None


class MailCampaignCsvProspectLink(BaseModel):
    """Durable, permanent link from one (mail_campaign_id, idempotency_key)
    pair to the CrmImportBatch it is bound to (Stage 4B, 2026-09-03) --
    written by MailCampaignCsvProspectService as the FIRST action of a
    CSV-driven Add Prospects operation, before the campaign eligibility
    preflight, before CrmImportService.commit(), before anything else --
    see that service's own docstring for the full ordering and why.

    Its entire purpose: once created, a retry of the SAME operation that
    happens to (re)supply a DIFFERENT import_batch_id must still resolve
    to THIS row's own recorded id, never the retry's -- so one logical
    "upload this CSV, add these prospects" operation can never fan out
    into more than one CrmImportBatch commit, regardless of how many
    times the client retries. `PRIMARY KEY (mail_campaign_id,
    idempotency_key)` at the store level is the actual mechanism (see
    MailCampaignCsvProspectLinkStore); this model carries no separate id
    of its own because that composite pair already uniquely identifies
    the row.

    Deliberately minimal and PII-free: three ids and a timestamp, nothing
    else. The real import content (raw CSV rows, mapped fields, per-row
    classification) lives exclusively in CrmImportBatch, reachable only
    through the existing, separate, human-session-only /crm/import/*
    routes -- this table is never a second place that data could leak
    into, and is safe to read/log without any of the privacy handling
    CrmImportBatch itself requires."""

    mail_campaign_id: str
    idempotency_key: str
    import_batch_id: str
    created_at: datetime


class MailEnrollmentBatchMemberState(str, Enum):
    """One member's progress through resolution -- a strict, one-way
    progression, never reversed:

        CANDIDATE --> ALREADY_ENROLLED                (terminal)
                  --> ENROLLED_SUPPRESSED              (terminal)
                  --> ENROLLED_PENDING --> PREPARED     (terminal)

    CANDIDATE: frozen at cohort-resolution time, not yet processed by any
      reconciliation pass.
    ALREADY_ENROLLED: this campaign already contained a MailEnrollment for
      this contact (at ANY status, including long-COMPLETED) -- skipped,
      no new row created.
    ENROLLED_SUPPRESSED: a genuinely NEW MailEnrollment was created
      (status=SUPPRESSED, since this contact's email was already
      suppressed at the moment reconciliation processed this member -- see
      _reconcile_batch()'s own docstring on why this is checked fresh,
      never from a caller-supplied snapshot). No Step 1 is ever
      materialized for a suppressed enrollment. Terminal.
    ENROLLED_PENDING: a genuinely new MailEnrollment was created
      (status=PENDING) but its Step 1 has not been materialized yet --
      the one non-terminal "enrolled" state.
    PREPARED: this member's enrollment now has a materialized Step 1 row
      (created via the exact same MailSendingService.
      create_step1_execution() call activate_campaign() itself uses) and
      has been flipped PENDING -> ACTIVE. Terminal."""

    CANDIDATE = "candidate"
    ALREADY_ENROLLED = "already_enrolled"
    ENROLLED_SUPPRESSED = "enrolled_suppressed"
    ENROLLED_PENDING = "enrolled_pending"
    PREPARED = "prepared"


class MailEnrollmentBatchMember(BaseModel):
    """One frozen candidate belonging to exactly one MailEnrollmentBatch --
    the durable representation of "the cohort," deliberately NOT a
    contact-id array on MailEnrollmentBatch itself (a batch of hundreds of
    contacts needs real per-contact state to be resumable after a crash,
    which a flat array can't represent -- see MailEnrollmentBatchMemberState).

    Written ONCE per batch, at cohort-freeze time, for EVERY resolved
    candidate -- see MailCampaignService.add_prospects()'s own docstring
    for why every member row is written BEFORE the owning
    MailEnrollmentBatch row itself: the batch row's existence is the sole
    signal that a freeze is durably committed, so a crash any time before
    it exists is safe to treat as "never happened" (a retry restarts from
    scratch under a fresh batch_id; these orphaned rows are cleaned up by
    MailCampaignService.cleanup_orphan_batch_members() -- see that
    method's own docstring). Once the owning batch row exists, this set of
    members is IMMUTABLE as a set -- reconciliation only ever advances
    each member's own `state`, never adds or removes a member, never
    re-resolves the original CRM List/CSV source.

    PRIMARY KEY (batch_id, crm_contact_id) -- the actual DB-level
    guarantee that a candidate can never appear twice within one batch's
    cohort."""

    batch_id: str
    crm_contact_id: str
    state: MailEnrollmentBatchMemberState
    enrollment_id: str | None = None
    created_at: datetime
    updated_at: datetime


class MailCampaignWorkload(BaseModel):
    """A campaign's current enrollment-status counts -- WORKLOAD, tracked
    completely independently of the campaign's own lifecycle `status`
    field (see MailCampaignStatus's own docstring: an ACTIVE campaign with
    zero pending/in-progress enrollments is still ACTIVE, never inferred
    to be "done" from this). Computed live from MailEnrollmentStore on
    every read -- never cached, never itself persisted.

    Deliberately an explicit, stable field per MailEnrollmentStatus value
    -- never an open-ended/dynamic {status: count} mapping -- so a
    frontend consumer never has to understand enum evolution to render
    this. `total` is always exactly the sum of the six status fields
    (enforced by MailCampaignService.get_workload(), the only place this
    is constructed); adding it explicitly rather than making callers sum
    the six fields themselves."""

    mail_campaign_id: str
    total: int
    pending: int
    active: int
    paused: int
    completed: int
    suppressed: int
    failed: int


# --- Per-step execution (Phase A durable execution model) -------------------


class MailEnrollmentStepStatus(str, Enum):
    """
    PENDING -> QUEUED -> CLAIMED -> SENDING -> SENT
                            |          |
                            v          v
                        (released)  UNKNOWN
                       QUEUED/
                  SKIPPED_SUPPRESSED/
                  (enrollment PAUSED)

    PENDING: row exists, not yet eligible (waiting on the previous step's
      delay to elapse) or eligible but not yet resolved to a legal send
      window.
    QUEUED: eligible, `next_send_at` resolved (may be due now or in the
      future). The only status a future worker's poll query targets
      (`WHERE status='queued' AND next_send_at <= now()`).
    CLAIMED: a worker atomically won this row (conditional UPDATE,
      `WHERE status='queued'`, `rowcount==1` to confirm) and is running
      MailSendingService's runtime safety checks. NO provider call has
      been made in this state -- every check failure here is fully
      reversible: back to QUEUED (window/pacing/limit not satisfied yet,
      `next_send_at` pushed forward), to SKIPPED_SUPPRESSED (live
      suppression hit), or the ENROLLMENT (not this row) moves to PAUSED
      (assigned mailbox unavailable). A row found stuck in CLAIMED past a
      short timeout is safe to reset to QUEUED automatically -- see
      MailSendingService.reap_orphans()'s docstring -- because we know
      with certainty no provider call happened yet.
    SENDING: every runtime check passed; the worker has committed a
      generated `rfc_message_id` (the idempotency marker) and is about to
      call, or is calling, the send provider. THIS IS THE PROVIDER-CALL
      UNCERTAINTY BOUNDARY -- a crash, timeout, or ambiguous error anywhere
      in this state means we can no longer prove the message wasn't sent.
      A row found stuck in SENDING past a timeout is NEVER auto-resolved --
      it moves to UNKNOWN, not back to QUEUED (see reap_orphans()).
    SENT: confirmed success (the provider returned message/thread ids).
      Terminal, success.
    SKIPPED_SUPPRESSED: terminal; always reached from CLAIMED, never from
      SENDING -- live suppression is one of the pre-send runtime checks,
      resolved strictly before the provider-call boundary.
    FAILED: terminal; a confirmed, definitive provider rejection,
      transient-failure retries exhausted (see the retry policy's own
      constants for why these are deliberately NOT hardcoded product
      numbers yet), OR a PREPARE-phase failure proven permanently invalid
      BEFORE any provider call was ever made (e.g. a recipient address
      that cannot support an unsubscribe token, or a MIME header
      injection attempt) -- reached directly from CLAIMED in that case,
      never from SENDING, and never via UNKNOWN.
    UNKNOWN: the row was in SENDING and the outcome could not be confirmed.
      Never auto-resolved by anything in this codebase -- requires manual
      reconciliation (a human, or future Phase-C+ tooling that can query
      the provider directly) before it can move to SENT (verified sent,
      backfill the identifiers) or back to QUEUED (verified NOT sent, safe
      to retry). Duplicate cold emails are worse than a temporarily stuck
      send -- see MailSendingService's module docstring.
    """

    PENDING = "pending"
    QUEUED = "queued"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED_SUPPRESSED = "skipped_suppressed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class MailEnrollmentStep(BaseModel):
    """
    One durable row per (enrollment, sequence step) actually being
    executed -- NOT pre-created for every step of every enrollment at
    Mark Ready. Materialization is LAZY:
      - Step 1's row is created for every currently-PENDING enrollment at
        the exact moment MailCampaignService.activate_campaign() succeeds
        (every active enrollment definitely needs exactly one Step 1
        attempt -- no speculation to avoid there).
      - Step N+1's row is created ONLY once Step N's row reaches SENT --
        see MailSendingService.record_send_success()'s docstring for the
        exact "is there a next MailSequenceStep, and if so materialize it;
        if not, this enrollment may now be COMPLETED" logic. A lead
        suppressed/paused/campaign-archived partway through the sequence
        never gets rows created for steps it will now never reach.

    CONTENT SNAPSHOT: `subject`/`body`/`delay_days`/`reply_in_thread` are
    copied from the originating MailSequenceStep at the exact moment this
    row is created, and are the AUTHORITATIVE content for this specific
    send from then on -- nothing ever re-reads the live MailSequenceStep
    once this row exists. `step_id`/`step_number` are kept too, purely for
    reference/display/joins, never for re-reading content. This is
    deliberate defense-in-depth, not a workaround for a bug that exists
    today: MailSequenceStep is DRAFT-only editable, the only path back to
    DRAFT is unlock_campaign() (only reachable from READY, never from
    ACTIVE/PAUSED in this phase), and unlock_campaign() cascade-deletes
    every MailEnrollmentStep row -- so a *live* row's underlying step is
    provably immutable for that row's entire life under today's state
    machine alone. Snapshotting means that guarantee doesn't have to keep
    holding forever across future, unrelated lifecycle changes (e.g. a
    hypothetical future "edit an active campaign" feature) for this record
    to stay correct -- an execution record must never silently start
    reading a step body that has moved out from under it.

    See MailEnrollmentStepStatus for the full state machine and the
    provider-call uncertainty boundary. `mailbox_id` here is a per-message
    COPY of MailEnrollment.assigned_mailbox_id at send time (that field is
    the sticky, authoritative assignment; this one is a durable record of
    who actually sent THIS specific message, in case the enrollment-level
    field's meaning ever needs to diverge from historical fact later).

    UNIQUE(enrollment_id, step_id) -- defense-in-depth backstop (the
    lazy-materialization logic above already guarantees at most one row per
    pair by construction), matching this codebase's established pattern of
    enforcing at the DB layer what the service layer already computes
    correctly (see e.g. MailSequenceStep's UNIQUE(mail_campaign_id,
    step_number)).
    """

    enrollment_step_id: str
    mail_campaign_id: str
    enrollment_id: str
    crm_contact_id: str
    step_id: str
    step_number: int

    # --- content snapshot, frozen at row-creation time -- see docstring ---
    subject: str
    body: str
    delay_days: int
    reply_in_thread: bool

    status: MailEnrollmentStepStatus = MailEnrollmentStepStatus.PENDING
    eligible_at: datetime | None = None
    next_send_at: datetime | None = None
    sent_at: datetime | None = None

    mailbox_id: str | None = None
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    rfc_message_id: str | None = None

    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    # Sanitized (error class/code), never the raw provider payload or any
    # message content -- same "structural only" discipline as the Activity
    # Log (see ActivityLogService's module docstring).
    last_error: str | None = None

    # Phase C addition -- counts CONSECUTIVE transient PREPARE-phase
    # failures (never a provider/SENDING attempt -- see attempt_count
    # above, whose meaning this deliberately does NOT redefine or share).
    # Incremented by MailSendingService's transient-prepare-failure
    # handling. Once it reaches PREPARE_TRANSIENT_MAX_ATTEMPTS, the
    # enrollment is PAUSED(PREPARE_TRANSIENT_EXHAUSTED) and this counter
    # is deliberately NEVER reset automatically -- not by reap_orphans()
    # (a crash mid-prepare must not silently rearm the budget), and not
    # by any periodic sweep either (see that pause reason's own
    # docstring for why unbounded automatic resets would defeat the
    # entire point of a BOUNDED retry budget). Reset to 0 ONLY as an
    # explicit consequence of MailSendingService.
    # resolve_prepare_blocked_step() -- a human-triggered recovery
    # action, never a background one.
    prepare_failure_count: int = 0

    # Worker claim lock -- see MailEnrollmentStepStatus.CLAIMED's docstring.
    claimed_by: str | None = None
    claimed_at: datetime | None = None

    created_at: datetime
    updated_at: datetime


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
    """Raised by validate_mail_timezone()/validate_send_windows() below -- a
    plain ValueError subclass (matching this codebase's existing convention
    of raising bare ValueError for simple domain validation, e.g.
    CrmService.create_contact) so callers can catch it specifically without
    changing normal ValueError-catching behavior elsewhere."""


def validate_mail_timezone(timezone_name: str) -> None:
    """Isolated so update_campaign() can validate a timezone string in
    isolation (soft, per-field validation) without requiring every other
    schedule field to be present yet."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
        raise MailScheduleValidationError(f"'{timezone_name}' is not a valid IANA timezone.") from e


# --- Send windows (Schedule Phase 2 -- true multiple windows per weekday) --
#
# Superseded the OLD single-global-range scheme (`MailCampaign.sending_days`
# + `start_time`/`end_time`/`all_hours`) -- those four fields are NOT removed
# from MailCampaign (existing campaigns still have real data in them, and
# campaign CREATION still accepts them -- see MailCampaignCreateRequest in
# app/api/mail.py, unchanged), but they are no longer written by the new
# Schedule tab and are no longer read once a campaign has ANY MailSendWindow
# row. See MailCampaignService's schedule methods (_resolve_schedule(),
# get_schedule(), set_schedule()) for the exact "windows if present, else
# synthesized from legacy fields" resolution rule -- that resolver is the
# ONE place a campaign's real schedule is ever read from, so nothing can
# read a different, disagreeing representation than anything else.
#
# End-of-day representation: this model has no way to express "24:00"
# (datetime.time's max is 23:59:59.999999) -- a full day window is
# `00:00`-`23:59`, matching the EXACT sentinel this codebase's legacy
# all_hours flag has always used (see mail_campaign_service.py's
# _ALL_DAY_SENTINEL_START/_END), not a new convention.
class MailSendWindow(BaseModel):
    """One contiguous send window on one weekday for one campaign. Multiple
    rows may share a `day_of_week` (multiple windows the same day) and/or a
    campaign (multiple days) -- there is no uniqueness constraint on
    `day_of_week` alone, unlike MailSequenceStep's step_number.

    `window_id` is a REAL, STABLE identity across ordinary edits -- PUT
    .../schedule is still a full replace of the campaign's window set (see
    MailSendWindowStore.replace_for_campaign()), but the SERVICE layer
    (MailCampaignService.set_schedule()) resolves each request window
    against the campaign's current rows first: a request window that
    carries an existing `window_id` keeps that same row's identity (and
    `created_at`) even if its day/start/end changed, a request window with
    no `window_id` mints a fresh one, and any existing row whose id isn't
    referenced in the request is dropped. This matters once anything
    (execution history, analytics) might reference a specific window later
    -- a drag-to-reschedule shouldn't silently delete and recreate the
    entity it moved."""

    window_id: str
    mail_campaign_id: str
    day_of_week: int  # 0=Monday .. 6=Sunday, same convention as legacy sending_days
    start_time: time
    end_time: time
    created_at: datetime
    updated_at: datetime


class MailScheduleWindowInput(BaseModel):
    """PUT .../schedule's per-window request shape -- `start_time`/`end_time`
    are "HH:MM" strings (matching every other time-of-day input in this API),
    parsed server-side. `window_id` is OPTIONAL: omit it (or pass null) for
    a genuinely new window; pass an existing window's real id to preserve
    its identity across an edit -- see MailSendWindow's docstring. An id
    that doesn't belong to this campaign, or that's repeated within the
    same request, is rejected (MailScheduleValidationError) before any
    write happens."""

    window_id: str | None = None
    day_of_week: int
    start_time: str
    end_time: str


MailScheduleSource = Literal["windows", "legacy", "none"]


class MailCampaignSchedule(BaseModel):
    """GET/PUT .../schedule's response shape. `source` is purely informational
    (never affects behavior) -- "windows" once any MailSendWindow row exists
    for this campaign (the authoritative, real case), "legacy" when nothing
    has been saved through the new Schedule tab yet and these are
    synthesized on the fly from the campaign's old sending_days/start_time/
    end_time/all_hours fields (never persisted just by reading them), or
    "none" when neither exists (a brand new, unconfigured campaign)."""

    mail_campaign_id: str
    timezone: str | None
    source: MailScheduleSource
    windows: list[MailSendWindow]


def validate_send_windows(windows: list[tuple[int, time, time]]) -> None:
    """Structural validation for PUT .../schedule -- day range, start<end,
    and no two windows on the SAME day_of_week overlapping. Deliberately
    does NOT require at least one window (a campaign may intentionally save
    an empty/all-days-off schedule mid-draft) -- "is this schedule complete
    enough to be Ready" is a separate, stricter check in
    mail_campaign_service._compute_readiness_warnings(), matching this
    codebase's existing soft-edit-time vs strict-readiness-time split (see
    MailCampaignService.update_campaign() vs mark_ready()).

    Raises on the FIRST problem found (matching validate_mail_timezone's
    single-raise convention) -- the caller collecting multiple readiness
    reasons wraps this in its own try/except, same pattern already used for
    every other readiness check.

    Touching boundaries are NOT an overlap -- 08:00-12:00 followed by
    12:00-18:00 on the same day is valid (back-to-back), only genuinely
    overlapping time ranges are rejected.
    """
    by_day: dict[int, list[tuple[time, time]]] = {}
    for day, start, end in windows:
        if day < 0 or day > 6:
            raise MailScheduleValidationError("day_of_week must be 0 (Monday) through 6 (Sunday).")
        if start >= end:
            raise MailScheduleValidationError("Each send window's start time must be before its end time.")
        by_day.setdefault(day, []).append((start, end))

    for day, day_windows in by_day.items():
        day_windows.sort()
        for (_prev_start, prev_end), (next_start, _next_end) in zip(day_windows, day_windows[1:]):
            if next_start < prev_end:
                raise MailScheduleValidationError(f"Send windows on {WEEKDAY_NAMES[day]} overlap.")


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


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


# --- Trigger feature: durable foundation only (Stage 5A, 2026-09-04) -------
#
# Modeled on QuickMail Triggers: a standing rule controlling WHEN and HOW
# MANY currently-PENDING (not-yet-started) leads enter the sequence --
# deliberately separate from MailSendWindow (when SENDING is allowed) and
# MailboxSendPolicy (a mailbox's own quota/pacing), neither of which this
# feature changes or replaces. See the approved Trigger design report for
# the full semantics; Stage 5A implements ONLY the schema below -- no
# occurrence execution, no freeze/reconciliation, no CRUD endpoints, no
# worker integration, and nothing anywhere reads MailCampaign.lead_start_mode
# yet. That all comes in later stages.


class MailLeadStartTrigger(BaseModel):
    """One standing lead-start rule for one campaign, e.g. "Mon-Fri at 9:00
    AM, start 20 leads." `weekdays`/`local_time` are in the CAMPAIGN's own
    timezone (MailCampaign.timezone) -- deliberately no per-trigger
    timezone field, matching MailSendWindow's own established convention
    (one timezone per campaign, not one per row) rather than introducing
    new per-row complexity with no product justification.

    A campaign may have more than one trigger (e.g. a second one at 2:00
    PM) -- both draw from the same PENDING pool; nothing here deduplicates
    or merges same-time/overlapping triggers, by design (see the approved
    report's concurrency section).

    Editable/deletable independent of the campaign's own DRAFT-only
    Schedule lock -- a later stage's own decision, not enforced by this
    model."""

    trigger_id: str
    mail_campaign_id: str
    weekdays: list[int]  # 0=Monday .. 6=Sunday, same convention as MailSendWindow.day_of_week
    local_time: time
    leads_to_start: int
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


def validate_lead_start_trigger(weekdays: list[int], leads_to_start: int) -> None:
    """Structural validation for a MailLeadStartTrigger, mirroring
    validate_send_windows()'s exact conventions (day range 0-6, raise on
    the first problem found, a plain ValueError since -- like
    CrmService.create_contact -- there is no dedicated request DTO/service
    consuming this yet in Stage 5A to warrant a bespoke exception type).
    Not wired into any endpoint or service in Stage 5A -- ready for the
    stage that adds Trigger CRUD to call."""
    if not weekdays:
        raise ValueError("At least one weekday must be selected.")
    for day in weekdays:
        if day < 0 or day > 6:
            raise ValueError("weekdays must each be 0 (Monday) through 6 (Sunday).")
    if len(set(weekdays)) != len(weekdays):
        raise ValueError("weekdays must not contain duplicates.")
    if leads_to_start < 1:
        raise ValueError("leads_to_start must be a positive integer.")


class MailTriggerOccurrence(BaseModel):
    """One durable, deterministically-identified firing of one
    MailLeadStartTrigger -- `(trigger_id, scheduled_for)` IS its identity
    (see MailTriggerOccurrenceStore), so the SAME logical occurrence (e.g.
    "this trigger's 9:00 AM slot on 2026-09-07") can only ever exist once,
    regardless of how many times a worker tick discovers it or restarts
    mid-processing.

    `status` starts PREPARING the instant the occurrence row is claimed
    and only reaches COMPLETED once every one of its frozen members has
    been reconciled (see MailTriggerOccurrenceMember) -- existence of this
    row alone does NOT mean the occurrence finished; only `status ==
    COMPLETED` does. `frozen_at` is a SEPARATE marker (set the instant the
    member cohort is committed) specifically so "not yet frozen" (None) is
    distinguishable from "frozen with zero eligible members" (set, zero
    member rows) -- both are real, different states a resumed occurrence
    needs to tell apart. `target_count` freezes this occurrence's own
    request (the trigger's `leads_to_start` AT THE TIME this occurrence was
    claimed) so a later edit to the trigger's own count never retroactively
    changes an already-in-progress or completed occurrence. `started_count`
    is populated only at COMPLETED time, always DERIVED from member
    outcomes (never incremented independently), so it can never drift from
    the durable member rows it summarizes.

    Stage 5A defines this shape and its persistence only -- no code
    anywhere creates, freezes, or completes one yet."""

    trigger_id: str
    mail_campaign_id: str
    scheduled_for: datetime
    status: Literal["PREPARING", "COMPLETED"] = "PREPARING"
    target_count: int
    frozen_at: datetime | None = None
    started_count: int | None = None
    created_at: datetime
    completed_at: datetime | None = None


class MailTriggerOccurrenceMember(BaseModel):
    """One enrollment frozen into one MailTriggerOccurrence's cohort --
    `(trigger_id, scheduled_for, enrollment_id)` is its identity, but
    `enrollment_id` also carries a GLOBAL uniqueness constraint at the
    store level (see MailTriggerOccurrenceStore): once an enrollment has
    been frozen into ANY occurrence's cohort, it can never be frozen into a
    different one, ever again -- even if this member's own outcome ends up
    SKIPPED_INELIGIBLE. This is a deliberate, verified-safe invariant, not
    an oversight: the only way a still-PENDING enrollment can become
    ineligible before reconciliation is SUPPRESSED (confirmed terminal --
    see MailSendingService.suppress_enrollment()'s own docstring) or one of
    the other genuinely-terminal statuses (FAILED, COMPLETED) -- a
    still-PENDING enrollment cannot reach the one PER-ENROLLMENT status
    that IS sometimes recoverable (MailEnrollmentStatus.PAUSED), since that
    status requires an already-assigned mailbox, and mailbox assignment
    only ever happens after activation. So there is no real scenario where
    a frozen member deserves a second occurrence later -- see the approved
    Trigger design report's eligibility analysis for the full walk.

    `outcome` starts PENDING_RECONCILE at freeze time (a frozen member is
    always considered still-PENDING at the moment its row is created --
    freezing never re-checks eligibility itself, only reconciliation does)
    and becomes terminal (STARTED or SKIPPED_INELIGIBLE) exactly once, at
    `reconciled_at`. A cohort is frozen, not resupplied: a member that
    reconciles as SKIPPED_INELIGIBLE is never replaced by a different,
    later-enrolled lead within the SAME occurrence.

    Stage 5A defines this shape and its persistence only -- no code
    anywhere freezes or reconciles a member yet."""

    trigger_id: str
    scheduled_for: datetime
    enrollment_id: str
    outcome: Literal["PENDING_RECONCILE", "STARTED", "SKIPPED_INELIGIBLE"] = "PENDING_RECONCILE"
    reconciled_at: datetime | None = None
