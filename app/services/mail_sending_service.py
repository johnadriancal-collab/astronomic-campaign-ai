"""
MailSendingService -- Phase A's durable execution engine. Owns every
status-gated mutation of MailEnrollmentStep/MailEnrollment rows once a
campaign is ACTIVE: creating Step 1 on activation, claiming a due row,
running the full pre-send safety checklist, recording success, cascading
suppression, and reaping orphaned claims.

This service NEVER sends a real email. `process_one_due_step()` is the one
method that reaches the CLAIMED->SENDING boundary and calls out to a
`MailSenderPort` -- an abstract interface with ZERO concrete
implementation anywhere in app/ (see that class's own docstring). Nothing
here imports gmail/smtp, changes an OAuth scope, or runs as a background
worker: every method is a plain, directly-callable, directly-testable
async function, meant to be driven by tests against a fake sender in this
phase. Actually calling `process_one_due_step()` on a schedule, from a
real worker process, is Phase C's job -- not implemented, and nothing in
this module starts one.

Schedule resolution (send windows + timezone) is deliberately NOT
re-implemented here. MailCampaignService._resolve_schedule() /
get_schedule() is already "the one place a campaign's real schedule is
ever read from" (its own docstring), including the legacy-field fallback
for a campaign that's never touched the Schedule tab -- every method below
that needs windows takes them as an explicit `windows`/`timezone_name`
parameter, resolved by the caller via `campaign_service.get_schedule()`,
rather than this service opening a second, potentially-diverging path to
the same data.

ARCHITECTURAL CONTRACT (production audit, adopted): record_send_success()
cannot make its SENDING->SENT write and its tail write (materializing the
next step, or completing the enrollment) atomic -- no cross-store
transaction exists in this codebase. reconcile_stalled_progression() /
reconcile_stalled_progressions() are the accepted recovery strategy for
that gap, not a stopgap pretending atomicity exists. This codebase commits
to a future Phase C worker running reconcile_stalled_progressions() (or an
equivalent sweep) automatically and periodically across every ACTIVE
campaign -- a human manually invoking it is not an acceptable substitute.
Phase A does not build that worker (no scheduler, no background task, no
route calls either reconcile method) -- but both methods are fully
implemented, idempotent, safe to call repeatedly, and tested now, so
Phase C only needs to schedule them, not design them.
"""

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
)
from app.models.mailbox import Mailbox, MailboxSendPolicy, MailboxStatus
from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_suppression_store import MailSuppressionStore
from app.repositories.mailbox_send_policy_store import MailboxSendPolicyStore
from app.repositories.mailbox_store import MailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_scheduler import compute_eligible_at, is_within_window, resolve_next_send_time
from app.services.rfc_message_id import generate_rfc_message_id

# --- Placeholder safety defaults --------------------------------------------
# None of these numbers are product-approved -- they exist only so the
# capacity/pacing/orphan-reaping logic below has SOMETHING to enforce and
# is fully testable now. Real values are a Phase C/product decision.

DEFAULT_MAILBOX_DAILY_SEND_LIMIT = 100
DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS = 30
CLAIMED_ORPHAN_TIMEOUT_SECONDS = 300
SENDING_ORPHAN_TIMEOUT_SECONDS = 900


class NoUsableMailboxError(Exception):
    """Raised by assign_mailbox_if_needed() when a campaign has no
    currently-selected mailbox that is currently CONNECTED. Distinct from
    "this enrollment's already-assigned mailbox became unusable" (that
    case is handled by process_one_due_step() moving the ENROLLMENT to
    PAUSED, never by raising) -- this exception means no assignment could
    ever have been made in the first place."""

    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(f"No usable (selected + CONNECTED) mailbox for campaign {mail_campaign_id}")


class SendOutcomeCertainty(str, Enum):
    """How certain a MailSenderPort.send() FAILURE (a raised exception)
    leaves us about whether the provider actually accepted the message
    before raising. Owned here, by the execution layer, not by any one
    provider adapter -- every current and future MailSenderPort
    implementation is expected to expose this same two-valued vocabulary
    on its exceptions (see MailSendError below) so a future Phase C
    caller can rely on ONE contract regardless of which provider raised.

    B2 does NOT act on this -- process_one_due_step() still treats every
    raised exception identically (leave the row in SENDING, let
    reap_orphans() move it to UNKNOWN -- see that method's docstring).
    This enum exists so the INFORMATION is available on the exception
    when Phase C is ready to use it for a real retry/requeue policy; it
    is not itself a behavior change.

    DEFINITELY_NOT_SENT: the provider observably rejected the request
      BEFORE it could have created/queued the message -- e.g. a real
      HTTP response confirming an auth/permission/rate-limit/malformed-
      request rejection, or a network failure proven to have occurred
      before any request bytes could have left this process (the
      connection was never established). Safe for a future retry policy
      to requeue without risk of a duplicate send.
    OUTCOME_UNKNOWN: we cannot prove the provider did NOT accept the
      message -- includes every ambiguous network failure (a read/write
      timeout after the request may already have been transmitted),
      every provider-side 5xx (the provider's own server may have
      partially processed the request before erroring), and a malformed/
      unparseable 200-equivalent response (the provider's own success
      signal, just one we failed to read). MUST be treated exactly like
      Phase A's existing SENDING->UNKNOWN path (see
      MailEnrollmentStepStatus.UNKNOWN's docstring) -- never
      auto-retried."""

    DEFINITELY_NOT_SENT = "definitely_not_sent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class MailSendError(Exception):
    """Base class every MailSenderPort implementation's failures SHOULD
    subclass so a future Phase C caller can inspect `.certainty` (see
    SendOutcomeCertainty above) -- NOT enforced by MailSenderPort.send()'s
    type signature (a concrete sender may still raise a bare Exception;
    process_one_due_step() treats that identically to any MailSendError,
    per that method's own contract). Defaults conservatively to
    OUTCOME_UNKNOWN -- a subclass must explicitly opt INTO
    DEFINITELY_NOT_SENT, never the reverse, matching this module's
    established "be conservative around ambiguity" principle (see
    MailEnrollmentStepStatus.UNKNOWN's docstring)."""

    certainty: SendOutcomeCertainty = SendOutcomeCertainty.OUTCOME_UNKNOWN


class MailSendRequestValidationError(ValueError):
    """Raised by MailSendRequest.__post_init__ -- a plain ValueError
    subclass (matching this codebase's existing convention, e.g.
    MailScheduleValidationError in app/models/mail.py) for a structurally
    invalid request. Always raised before any provider code runs (a
    frozen dataclass's __post_init__ runs at construction time, so an
    invalid MailSendRequest can never exist long enough to be passed to
    a sender)."""


@dataclass(frozen=True)
class MailSendRequest:
    """The complete, immutable input to ONE MailSenderPort.send() call --
    the provider boundary's single entry contract. Everything a concrete
    sender needs to construct and transmit a message, and -- most
    importantly -- everything it is NOT allowed to invent on its own.

    `rfc_message_id` is EXECUTION-OWNED, not provider-owned (a B2
    hardening-pass correction). The reason: Phase A's durable execution
    model requires the outbound Message-ID to be knowable BEFORE crossing
    the provider-call uncertainty boundary (see MailEnrollmentStepStatus.
    SENDING's docstring in app/models/mail.py) -- if the ID were instead
    generated INSIDE the provider adapter (as B2's first pass originally
    did), a crash between "Gmail received the message" and "our process
    persists the provider's response" would leave the durable execution
    row with no way to ever learn which Message-ID was actually sent, so
    reconciliation could never confirm or rule out a duplicate. Requiring
    the caller to generate (see app/services/rfc_message_id.py's
    generate_rfc_message_id()) and supply the ID here means a future
    Phase C worker can persist it onto MailEnrollmentStep.rfc_message_id
    BEFORE the CLAIMED->SENDING transition -- at which point it survives
    a crash regardless of what happens next, and a retry/reconciliation
    pass can reuse the SAME persisted value instead of generating a new
    one (this is also what resolves B2's original "deterministic for a
    specific execution attempt" open item: determinism-by-generation was
    never the right mechanism -- determinism-by-PERSISTENCE is).
    MailSenderPort implementations (GmailSender included) must CONSUME
    this value, never generate or replace it -- see
    tests/test_gmail_sender.py's determinism/no-silent-replacement tests.

    NOT YET WIRED: B2 does not add the "persist before SENDING" write to
    MailSendingService.process_one_due_step() -- that would be Phase A
    execution-behavior work, explicitly out of B2's scope. See that
    method's own inline comment at its sender.send() call site for
    exactly what B2 DOES do instead (generate inline, not persisted) and
    why that's a deliberate, temporary, fully-documented gap.

    MANDATORY PHASE C INVARIANT (recorded here now, enforced by nothing
    in this codebase yet -- B2 does not implement Phase C): before any
    provider invocation becomes possible, execution MUST perform this
    exact durable sequence, in this exact order:
      1. Obtain/generate the RFC Message-ID (see app/services/
         rfc_message_id.py's generate_rfc_message_id()).
      2. Persist that EXACT Message-ID onto MailEnrollmentStep.
         rfc_message_id.
      3. Commit that persistence successfully (the write must actually
         land before proceeding -- step 4 must never start on the
         strength of an unconfirmed write).
      4. Transition/commit the row into the provider uncertainty
         boundary (CLAIMED->SENDING -- see MailEnrollmentStepStatus.
         SENDING's docstring in app/models/mail.py).
      5. Invoke a MailSenderPort implementation (e.g. GmailSender) using
         a MailSendRequest built with that exact persisted Message-ID --
         never a newly generated one.
    Recovery rules this sequence exists to enable:
      - A crash after step 2/3 but BEFORE step 5 (provider invocation):
        later execution (a retry or reconciliation pass) MUST re-read
        and reuse the ALREADY-PERSISTED Message-ID from the row, never
        generate a second one for the same attempt.
      - A crash AFTER step 5 with an uncertain outcome (see
        SendOutcomeCertainty.OUTCOME_UNKNOWN above): execution MUST
        follow the existing UNKNOWN/manual-reconciliation safety model
        (MailEnrollmentStepStatus.UNKNOWN, reap_orphans() -- this module,
        unchanged since Phase A) and MUST NOT automatically resend.
    Nothing in B2 violates this invariant (process_one_due_step()'s
    current inline-generate-without-persisting behavior is a KNOWN,
    documented, temporary gap relative to it, not a conflicting design)
    -- but nothing in B2 implements it either. Phase C must not begin
    provider wiring without satisfying steps 1-5 above in full.

    `reply_in_thread` is copied from MailSequenceStep/MailEnrollmentStep's
    own field of the same name (see app/models/mail.py) -- an AUTHORING
    PREFERENCE ("thread this step under its predecessor, if one exists"),
    not a promise that threading context is actually available for THIS
    particular request. Step 1 always has `reply_in_thread` copied from
    its MailSequenceStep (commonly True, per that model's own default)
    even though there is, by definition, no prior message to thread under
    -- so `reply_in_thread=True` with no threading fields at all is a
    NORMAL, valid, expected shape, not an error.

    Threading fields, enforced by __post_init__ below:
      - `reply_in_thread=False`: `in_reply_to_message_id`, `references`,
        and `thread_id` must ALL be unset -- a send explicitly marked
        "not threaded" must never carry threading context.
      - `reply_in_thread=True`: `in_reply_to_message_id` and `thread_id`
        must be given TOGETHER or not at all -- one without the other is
        an incomplete threading request (the RFC parent for MIME
        In-Reply-To/References without Gmail's own thread identifier for
        the API's `threadId`, or vice versa), not a valid partial one.
        Both absent is fine (see above -- e.g. Step 1, or any step whose
        prior message's identifiers aren't available to this caller).
        `references` is optional even when both are present (a first
        reply has only one ancestor -- itself `in_reply_to_message_id`);
        when given, `in_reply_to_message_id` must also be given.
      Gmail's ACTUAL threading behavior with this shape is NOT proven by
      anything in this codebase yet -- see app/google/gmail_sender.py's
      module docstring; this is request-shape validation only."""

    mailbox: Mailbox
    to_email: str
    subject: str
    body: str
    rfc_message_id: str
    reply_in_thread: bool
    in_reply_to_message_id: str | None = None
    references: tuple[str, ...] = ()
    thread_id: str | None = None

    def __post_init__(self) -> None:
        mid = self.rfc_message_id
        if not mid or not mid.strip():
            raise MailSendRequestValidationError("rfc_message_id must be a non-empty string.")
        if any(ch in mid for ch in ("\r", "\n", "<", ">")):
            raise MailSendRequestValidationError(
                "rfc_message_id must be a bare local-part@domain value -- no angle brackets, no line breaks."
            )
        if "@" not in mid:
            raise MailSendRequestValidationError("rfc_message_id must contain '@' (local-part@domain shape).")

        if not self.reply_in_thread:
            if self.in_reply_to_message_id is not None or self.references or self.thread_id is not None:
                raise MailSendRequestValidationError(
                    "reply_in_thread=False (a new-thread send) must not carry any threading context "
                    "(in_reply_to_message_id/references/thread_id) -- set reply_in_thread=True instead "
                    "if this is actually a follow-up."
                )
        else:
            has_in_reply_to = self.in_reply_to_message_id is not None
            has_thread_id = self.thread_id is not None
            if has_in_reply_to != has_thread_id:
                raise MailSendRequestValidationError(
                    "in_reply_to_message_id and thread_id must be supplied TOGETHER or not at all -- "
                    "an incomplete threading request (one without the other) is invalid. Neither being "
                    "set is fine even when reply_in_thread=True (e.g. Step 1, or any step whose prior "
                    "message's identifiers aren't available to this caller) -- the send simply starts "
                    "a new thread despite the authoring preference; see MailSendRequest's docstring."
                )

        if self.references and self.in_reply_to_message_id is None:
            raise MailSendRequestValidationError(
                "references requires in_reply_to_message_id to also be set -- a References chain "
                "with no immediate parent is not a valid reply."
            )


@dataclass(frozen=True)
class SendResult:
    """What a real send would report back -- provider-assigned identity
    for the message, filled onto MailEnrollmentStep on success.
    `rfc_message_id` here MUST echo the value the caller supplied on the
    originating MailSendRequest (see that dataclass's docstring) -- a
    conforming MailSenderPort implementation never returns a different
    one. `provider_message_id`/`provider_thread_id` are the only two
    fields a real implementation actually invents (from the provider's
    own response); nothing here is ever synthesized by this service
    itself."""

    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str


class MailSenderPort(ABC):
    """The ONLY boundary through which a message could ever actually be
    sent. Phase A defined this interface with no concrete implementation
    anywhere under app/. Phase B2 (Gmail Sender Foundation) added the
    first one -- GmailSender (app/google/gmail_sender.py) -- but it is
    NOT wired to anything: no route, worker, or scheduler in this
    codebase ever constructs one and hands it to process_one_due_step()
    (see GmailSender's own module docstring, and tests/
    test_gmail_sending_safety.py, which is what actually enforces that).
    Wiring a real sender into a real, scheduled worker loop is Phase C's
    job, and when it happens it plugs into this exact interface without
    process_one_due_step() changing at all.

    UPDATED by the B2 hardening pass: send() now takes a single immutable
    MailSendRequest rather than five separate keyword arguments -- see
    that dataclass's own docstring for why (short version: `rfc_message_id`
    must be execution-supplied, not provider-invented, and a growing pile
    of optional threading kwargs was the wrong shape for that requirement
    to live in)."""

    @abstractmethod
    async def send(self, request: MailSendRequest) -> SendResult:
        """Must raise on any failure or uncertain outcome -- never return a
        synthetic/partial SendResult. process_one_due_step() treats a raised
        exception here as "provider call outcome unknown" and deliberately
        leaves the row in SENDING for orphan-reaping, never auto-retries
        (see SendOutcomeCertainty above for the richer information a
        MailSendError-subclassing implementation SHOULD expose on top of
        that uniform behavior, for Phase C's future use)."""


class SendBlockReason(str, Enum):
    CAMPAIGN_NOT_ACTIVE = "campaign_not_active"
    ENROLLMENT_NOT_ACTIVE = "enrollment_not_active"
    LOST_CLAIM_RACE = "lost_claim_race"
    NO_USABLE_MAILBOX = "no_usable_mailbox"
    ASSIGNED_MAILBOX_UNAVAILABLE = "assigned_mailbox_unavailable"
    MAILBOX_DAILY_LIMIT_REACHED = "mailbox_daily_limit_reached"
    MAILBOX_PACING_NOT_SATISFIED = "mailbox_pacing_not_satisfied"
    LEAD_START_LIMIT_REACHED = "lead_start_limit_reached"
    RECIPIENT_SUPPRESSED = "recipient_suppressed"
    OUTSIDE_SEND_WINDOW = "outside_send_window"


@dataclass(frozen=True)
class ProcessOutcome:
    """Result of one process_one_due_step() call. `sent=True` iff a
    MailSenderPort.send() call actually succeeded and record_send_success()
    applied. `blocked_reason` is set (and `sent=False`) whenever a safety
    check stopped the send before ever reaching the sender -- one of these
    is exactly what the "safety-check-per-failure-type" test matrix asserts
    on. `sender_error` is set only in the one case where the sender itself
    was actually called and raised -- the row is left in SENDING, provider
    outcome unknown, never auto-resolved by this method."""

    sent: bool
    blocked_reason: SendBlockReason | None = None
    sender_error: str | None = None


@dataclass(frozen=True)
class ResolvedMailboxSendPolicy:
    """Always-concrete (never-null) resolution of a mailbox's send policy --
    see resolve_mailbox_send_policy()'s docstring for the missing-row/
    null-override/explicit-override equivalence this guarantees."""

    daily_send_limit: int
    min_seconds_between_sends: int


@dataclass(frozen=True)
class ReapResult:
    reset_to_queued: int
    marked_unknown: int


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    reconciled: int


def _utc_day_start(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _campaign_local_day_start(at: datetime, timezone_name: str) -> datetime:
    """Midnight, campaign-local, of the calendar day containing `at` --
    the boundary for daily_lead_start_limit (see MailCampaign.
    daily_lead_start_limit's docstring: campaign-local, NOT the UTC
    boundary mailbox-level limits use)."""
    tz = ZoneInfo(timezone_name)
    local_date = at.astimezone(tz).date()
    return datetime.combine(local_date, datetime.min.time(), tzinfo=tz)


def _pick_mailbox_deterministic(enrollment_id: str, candidate_mailbox_ids: list[str]) -> str:
    """Pure function of (enrollment_id, sorted candidate ids) -- two
    concurrent callers computing this for the same enrollment always agree
    on WHICH mailbox to assign (see Correction 4 in the approved Phase A
    spec). Precise about what this buys, per a production audit finding:
    determinism alone does NOT make the persistence race-safe -- it only
    means two racing writers would never disagree about the value if both
    blindly wrote. The actual write safety comes from
    MailEnrollmentStore.try_assign_mailbox()'s real compare-and-swap (see
    that method's docstring), which this function has no knowledge of and
    does not depend on for its own correctness. Sorting the candidates
    first means the result is also insensitive to whatever order the store
    happens to return them in. Not true round-robin load distribution -- a
    stable hash spreads leads across mailboxes reasonably but does not
    guarantee an exactly even count; Phase A explicitly does not need more
    than that."""
    ordered = sorted(candidate_mailbox_ids)
    digest = hashlib.sha256(enrollment_id.encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(ordered)
    return ordered[index]


class MailSendingService:
    def __init__(
        self,
        *,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        step_store: MailEnrollmentStepStore,
        mailbox_store: MailboxStore,
        channel_store: MailCampaignMailboxStore,
        policy_store: MailboxSendPolicyStore,
        suppression_store: MailSuppressionStore,
        activity_log: ActivityLogService,
    ):
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.step_store = step_store
        self.mailbox_store = mailbox_store
        self.channel_store = channel_store
        self.policy_store = policy_store
        self.suppression_store = suppression_store
        self.activity_log = activity_log

    # --- Mailbox send policy -------------------------------------------------

    async def resolve_mailbox_send_policy(self, mailbox_id: str) -> ResolvedMailboxSendPolicy:
        """No MailboxSendPolicy row for this mailbox resolves IDENTICALLY to
        a row whose override fields are both null -- system defaults either
        way (see Correction 3: no production backfill is ever required for
        an already-connected mailbox). Only an explicit, non-null override
        on an existing row changes the result."""
        policy = await self.policy_store.get(mailbox_id)
        daily_limit = DEFAULT_MAILBOX_DAILY_SEND_LIMIT
        pacing = DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS
        if policy is not None:
            if policy.daily_send_limit is not None:
                daily_limit = policy.daily_send_limit
            if policy.min_seconds_between_sends is not None:
                pacing = policy.min_seconds_between_sends
        return ResolvedMailboxSendPolicy(daily_send_limit=daily_limit, min_seconds_between_sends=pacing)

    # --- Mailbox assignment ---------------------------------------------------

    async def assign_mailbox_if_needed(self, enrollment: MailEnrollment) -> MailEnrollment:
        """If `enrollment` already has a sticky assigned_mailbox_id, returns
        it completely unchanged -- assignment is a one-time, never-silently-
        reassigned decision (see MailEnrollment.assigned_mailbox_id's
        docstring); a mailbox that's since become unusable is
        process_one_due_step()'s concern (pause the enrollment), never this
        method's. Otherwise, picks deterministically from every mailbox
        currently selected for this campaign AND currently CONNECTED, and
        persists it via MailEnrollmentStore.try_assign_mailbox() -- a real
        compare-and-swap (`WHERE assigned_mailbox_id IS NULL`), not a blind
        save(). This matters even though the SELECTION is deterministic
        (two concurrent callers always compute the same `chosen` value):
        that only means a race can't produce two DIFFERENT assignments --
        it says nothing about whether the WRITE itself is safe against
        clobbering some unrelated concurrent change to the same row (e.g.
        a suppression cascade flipping `status` in between this method's
        own read and write). try_assign_mailbox() closes that gap
        independently of the selection's determinism. If it reports the
        row was already assigned (lost the race, or a repeat call), this
        re-reads and returns whatever is ACTUALLY persisted, rather than
        assuming its own locally-computed `chosen` value ever applied.
        Raises NoUsableMailboxError if no CONNECTED selected mailbox
        exists at all."""
        if enrollment.assigned_mailbox_id is not None:
            return enrollment

        selected_ids = await self.channel_store.list_mailbox_ids_for_campaign(enrollment.mail_campaign_id)
        candidates: list[str] = []
        for mailbox_id in selected_ids:
            mailbox = await self.mailbox_store.get(mailbox_id)
            if mailbox is not None and mailbox.status == MailboxStatus.CONNECTED:
                candidates.append(mailbox_id)
        if not candidates:
            raise NoUsableMailboxError(enrollment.mail_campaign_id)

        chosen = _pick_mailbox_deterministic(enrollment.enrollment_id, candidates)
        updated = enrollment.model_copy(update={"assigned_mailbox_id": chosen})
        applied = await self.enrollment_store.try_assign_mailbox(enrollment.enrollment_id, updated)
        if applied:
            return updated

        current = await self.enrollment_store.get(enrollment.enrollment_id)
        assert current is not None and current.assigned_mailbox_id is not None
        return current

    async def _selected_and_connected(self, mail_campaign_id: str, mailbox_id: str) -> Mailbox | None:
        selected_ids = await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id)
        if mailbox_id not in selected_ids:
            return None
        mailbox = await self.mailbox_store.get(mailbox_id)
        if mailbox is None or mailbox.status != MailboxStatus.CONNECTED:
            return None
        return mailbox

    # --- Step materialization --------------------------------------------------

    async def create_step1_execution(
        self,
        *,
        enrollment: MailEnrollment,
        step1: MailSequenceStep,
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> MailEnrollmentStep:
        """Idempotent: safely re-callable for the same enrollment (e.g. a
        repeated/racing activate_campaign() call) without ever creating a
        second Step 1 row. `step1` must be the campaign's step_number == 1
        MailSequenceStep -- the caller (MailCampaignService.
        activate_campaign()) is responsible for that invariant already
        holding (see MailSequenceStep's Step-1-delay-is-always-zero
        invariant)."""
        existing = await self.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, step1.step_id)
        if existing is not None:
            return existing

        next_send_at = resolve_next_send_time(windows, timezone_name, now)
        candidate = MailEnrollmentStep(
            enrollment_step_id=str(uuid.uuid4()),
            mail_campaign_id=enrollment.mail_campaign_id,
            enrollment_id=enrollment.enrollment_id,
            crm_contact_id=enrollment.crm_contact_id,
            step_id=step1.step_id,
            step_number=step1.step_number,
            subject=step1.subject,
            body=step1.body,
            delay_days=step1.delay_days,
            reply_in_thread=step1.reply_in_thread,
            status=MailEnrollmentStepStatus.QUEUED,
            eligible_at=now,
            next_send_at=next_send_at,
            created_at=now,
            updated_at=now,
        )
        created = await self.step_store.create(candidate)
        if created:
            return candidate
        existing = await self.step_store.get_by_enrollment_and_step(enrollment.enrollment_id, step1.step_id)
        assert existing is not None  # a failed create() with no existing row would be a store bug
        return existing

    async def _materialize_next_step(
        self,
        *,
        prior: MailEnrollmentStep,
        next_sequence_step: MailSequenceStep,
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> MailEnrollmentStep:
        """Same idempotency contract as create_step1_execution(). `prior`
        must already be SENT (its sent_at anchors the delay calculation)."""
        existing = await self.step_store.get_by_enrollment_and_step(prior.enrollment_id, next_sequence_step.step_id)
        if existing is not None:
            return existing

        assert prior.sent_at is not None
        eligible_at = compute_eligible_at(prior.sent_at, next_sequence_step.delay_days, timezone_name)
        next_send_at = resolve_next_send_time(windows, timezone_name, eligible_at)
        candidate = MailEnrollmentStep(
            enrollment_step_id=str(uuid.uuid4()),
            mail_campaign_id=prior.mail_campaign_id,
            enrollment_id=prior.enrollment_id,
            crm_contact_id=prior.crm_contact_id,
            step_id=next_sequence_step.step_id,
            step_number=next_sequence_step.step_number,
            subject=next_sequence_step.subject,
            body=next_sequence_step.body,
            delay_days=next_sequence_step.delay_days,
            reply_in_thread=next_sequence_step.reply_in_thread,
            status=MailEnrollmentStepStatus.QUEUED,
            eligible_at=eligible_at,
            next_send_at=next_send_at,
            created_at=now,
            updated_at=now,
        )
        created = await self.step_store.create(candidate)
        if created:
            return candidate
        existing = await self.step_store.get_by_enrollment_and_step(prior.enrollment_id, next_sequence_step.step_id)
        assert existing is not None
        return existing

    # --- Completion / suppression cascade --------------------------------------

    async def record_send_success(
        self,
        *,
        step: MailEnrollmentStep,
        send_result: SendResult,
        sequence_steps: list[MailSequenceStep],
        enrollment: MailEnrollment,
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> bool:
        """`step` must currently be SENDING (process_one_due_step()'s own
        prior claim). Applies the SENDING->SENT transition, then decides
        completion EXPLICITLY by sequence membership, never by inferring
        from currently-materialized rows (see Correction 2 in the approved
        spec, and MailEnrollmentStatus.COMPLETED's docstring for the exact
        3-step counter-example this guards against): if a MailSequenceStep
        with step_number == step.step_number + 1 exists, that row is
        materialized and the enrollment stays ACTIVE; otherwise the
        enrollment is marked COMPLETED. Returns False if the SENDING->SENT
        transition itself lost a race (the row's status was no longer
        SENDING) -- under the documented single-worker assumption this
        should never happen, but the caller must not assume anything else
        about the row's state when it does.

        NOT atomic across the SENDING->SENT write and the tail write below
        (enrollment->COMPLETED, or the next step's materialization) -- this
        codebase has no cross-store transaction (see sqlite_txn.py's
        docstring). If the process crashes/raises between them, `step` is
        durably SENT but its consequence hasn't happened yet -- a real,
        production-audit-identified gap, not a hypothetical one. This is
        NEVER silently lost: see reconcile_stalled_progression() below,
        which finds and finishes exactly this half-done state. It is not
        wired to run automatically (no worker exists in Phase A to call it
        on a schedule -- same boundary as reap_orphans()), but it is a
        real, tested, idempotent catch-up pass, not a hypothetical fix."""
        sent_step = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.SENT,
                "sent_at": now,
                "gmail_message_id": send_result.provider_message_id,
                "gmail_thread_id": send_result.provider_thread_id,
                "rfc_message_id": send_result.rfc_message_id,
                "updated_at": now,
            }
        )
        applied = await self.step_store.try_transition(step.enrollment_step_id, MailEnrollmentStepStatus.SENDING, sent_step)
        if not applied:
            return False

        next_sequence_step = next((s for s in sequence_steps if s.step_number == step.step_number + 1), None)
        if next_sequence_step is None:
            updated_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.COMPLETED})
            await self.enrollment_store.save(updated_enrollment)
            await self.activity_log.record(
                event_type="mail_enrollment.completed",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary="A lead completed its mail sequence.",
                entity_type="mail_campaign",
                entity_id=enrollment.mail_campaign_id,
            )
            return True

        await self._materialize_next_step(
            prior=sent_step,
            next_sequence_step=next_sequence_step,
            windows=windows,
            timezone_name=timezone_name,
            now=now,
        )
        return True

    async def reconcile_stalled_progression(
        self,
        *,
        enrollment: MailEnrollment,
        sequence_steps: list[MailSequenceStep],
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> bool:
        """Catches up ONE enrollment whose record_send_success() tail write
        never completed -- see that method's docstring for exactly which
        crash window this covers (SENDING->SENT committed, but the
        following enrollment->COMPLETED save or next-step materialization
        did not). Idempotent and safe to call on an enrollment that needs
        no catch-up at all (a no-op, returns False) -- so this can be
        called speculatively, the same way reap_orphans() is, without
        first proving a failure actually happened.

        Looks only at this enrollment's SENT rows, takes the one with the
        HIGHEST step_number (earlier SENT rows already had their own
        consequence applied when THEY were the highest -- this method
        never revisits an already-resolved row), and either materializes
        the missing next step or completes the enrollment, using the exact
        same logic record_send_success() itself uses -- there is
        deliberately only one implementation of "what happens after a
        step is SENT," never two that could drift apart.

        Returns True iff it actually did something (a real gap was found
        and fixed); False means either this enrollment has no SENT rows at
        all, is not ACTIVE (nothing to reconcile for a terminal/paused
        enrollment), or its most-recent SENT row's consequence had already
        fully applied.

        This is the single-enrollment PRIMITIVE -- see
        reconcile_stalled_progressions() (plural) below for the batch/
        global-recovery entry point a future Phase C worker would
        actually call."""
        if enrollment.status != MailEnrollmentStatus.ACTIVE:
            return False

        rows = await self.step_store.list_for_enrollment(enrollment.enrollment_id)
        sent_rows = [r for r in rows if r.status == MailEnrollmentStepStatus.SENT]
        if not sent_rows:
            return False
        latest_sent = max(sent_rows, key=lambda r: r.step_number)

        next_sequence_step = next((s for s in sequence_steps if s.step_number == latest_sent.step_number + 1), None)
        if next_sequence_step is None:
            updated_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.COMPLETED})
            await self.enrollment_store.save(updated_enrollment)
            await self.activity_log.record(
                event_type="mail_enrollment.completed",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary="A lead completed its mail sequence (reconciled after an interrupted prior attempt).",
                entity_type="mail_campaign",
                entity_id=enrollment.mail_campaign_id,
            )
            return True

        existing_next = await self.step_store.get_by_enrollment_and_step(
            enrollment.enrollment_id, next_sequence_step.step_id
        )
        if existing_next is not None:
            return False  # already materialized -- nothing was actually stalled

        await self._materialize_next_step(
            prior=latest_sent, next_sequence_step=next_sequence_step, windows=windows, timezone_name=timezone_name, now=now
        )
        return True

    async def reconcile_stalled_progressions(
        self,
        *,
        mail_campaign_id: str,
        sequence_steps: list[MailSequenceStep],
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> ReconcileResult:
        """Batch/global-recovery entry point for
        reconcile_stalled_progression() -- the ARCHITECTURAL CONTRACT this
        codebase adopts is that a future Phase C worker MUST run this (or
        an equivalent periodic sweep across every ACTIVE campaign) on a
        schedule; a human manually invoking the single-enrollment
        primitive is not an acceptable substitute for real recovery. Phase
        A deliberately does NOT create that worker (no scheduler, no
        background task, no route calls this) -- but the primitive this
        method is built from is fully implemented, idempotent, and tested
        now, so Phase C's worker has nothing left to design here, only to
        schedule.

        Scans EVERY enrollment currently in this campaign (not merely one
        already-known enrollment) and reconciles each independently via
        reconcile_stalled_progression() -- so this is suitable for a
        worker that has no idea in advance which specific enrollment (if
        any) is stalled; it simply sweeps the whole campaign, the same way
        reap_orphans() sweeps every stale row rather than requiring a
        caller to already know which ones are stale.

        Safe to call back-to-back, repeatedly, on a campaign with nothing
        to reconcile at all -- every call after the first (or after
        nothing was ever actually stalled) is a pure no-op, since
        reconcile_stalled_progression() itself is a no-op whenever an
        enrollment's SENT row's consequence already applied. Incapable of
        ever creating a duplicate next-step row even under a genuine race
        between two overlapping reconciliation sweeps (e.g. a
        misconfigured Phase C worker running two instances): the
        underlying MailEnrollmentStep table enforces UNIQUE(enrollment_id,
        step_id) at the DB layer (see sqlite_mail_enrollment_step_store.py),
        and create() already treats that constraint violation as "already
        exists" (returns False, never raises) -- this is a real, structural
        guarantee, not merely this method's own care."""
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        reconciled = 0
        for enrollment in enrollments:
            healed = await self.reconcile_stalled_progression(
                enrollment=enrollment, sequence_steps=sequence_steps, windows=windows, timezone_name=timezone_name, now=now
            )
            if healed:
                reconciled += 1
        return ReconcileResult(scanned=len(enrollments), reconciled=reconciled)

    async def suppress_enrollment(self, enrollment: MailEnrollment, now: datetime) -> None:
        """Moves every not-yet-sent, not-in-flight row for this enrollment
        (PENDING/QUEUED/CLAIMED) to SKIPPED_SUPPRESSED, and the enrollment
        itself to SUPPRESSED -- terminal, so no further step is ever
        materialized (record_send_success()/​_materialize_next_step() are
        simply never called again for this enrollment). A row already in
        SENDING (provider-call outcome uncertain) is deliberately left
        alone -- it resolves via reap_orphans() to UNKNOWN on its own
        schedule, and by the time that happens this enrollment is already
        terminal, so nothing further gets materialized regardless of how
        that row resolves. A SENT/FAILED/UNKNOWN/already-SKIPPED_SUPPRESSED
        row is history and is never touched."""
        skippable = {
            MailEnrollmentStepStatus.PENDING,
            MailEnrollmentStepStatus.QUEUED,
            MailEnrollmentStepStatus.CLAIMED,
        }
        for row in await self.step_store.list_for_enrollment(enrollment.enrollment_id):
            if row.status in skippable:
                updated = row.model_copy(update={"status": MailEnrollmentStepStatus.SKIPPED_SUPPRESSED, "updated_at": now})
                await self.step_store.try_transition(row.enrollment_step_id, row.status, updated)

        updated_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.SUPPRESSED})
        await self.enrollment_store.save(updated_enrollment)
        await self.activity_log.record(
            event_type="mail_enrollment.suppressed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary="A lead's mail sequence was stopped because their email became suppressed.",
            entity_type="mail_campaign",
            entity_id=enrollment.mail_campaign_id,
        )

    async def maybe_complete_campaign(self, mail_campaign_id: str, now: datetime) -> bool:
        """Whenever an enrollment might have just reached a terminal state,
        checks whether EVERY enrollment for this campaign is now terminal
        (COMPLETED/SUPPRESSED/FAILED -- PENDING/ACTIVE/PAUSED are not) and,
        if so, flips the campaign itself to COMPLETED. A no-op (returns
        False) if the campaign isn't ACTIVE, has zero enrollments, or any
        enrollment is still non-terminal. Safe to call after every
        record_send_success()/suppress_enrollment() -- cheap, and doing so
        is the only way COMPLETED is ever reached (nothing else in Phase A
        sets it)."""
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None or campaign.status != MailCampaignStatus.ACTIVE:
            return False
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        if not enrollments:
            return False
        terminal = {MailEnrollmentStatus.COMPLETED, MailEnrollmentStatus.SUPPRESSED, MailEnrollmentStatus.FAILED}
        if not all(e.status in terminal for e in enrollments):
            return False

        updated = campaign.model_copy(update={"status": MailCampaignStatus.COMPLETED, "updated_at": now})
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.completed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{campaign.name}" completed -- every enrollment reached a terminal state.',
            entity_type="mail_campaign",
            entity_id=campaign.mail_campaign_id,
            entity_name=campaign.name,
        )
        return True

    # --- The runtime safety checklist + one send attempt -----------------------

    async def _release_to_queued(self, step: MailEnrollmentStep, next_send_at: datetime, now: datetime) -> None:
        released = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.QUEUED,
                "claimed_by": None,
                "claimed_at": None,
                "next_send_at": next_send_at,
                "updated_at": now,
            }
        )
        await self.step_store.try_transition(step.enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, released)

    async def process_one_due_step(
        self,
        step: MailEnrollmentStep,
        *,
        sender: MailSenderPort,
        claimed_by: str,
        sequence_steps: list[MailSequenceStep],
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> ProcessOutcome:
        """Runs ONE due MailEnrollmentStep row through the full safety
        checklist and, only if every check passes, calls `sender.send()`.
        `windows`/`timezone_name` must be this step's campaign's CURRENTLY
        resolved schedule (see this module's docstring on why that's the
        caller's job, via MailCampaignService.get_schedule()).
        `sequence_steps` must be every MailSequenceStep currently on this
        step's campaign -- passed straight through to record_send_success()
        on a successful send so it can decide completion vs. materializing
        the next step; only fetched here, not stored, since this service
        has no MailSequenceStepStore dependency of its own.

        Every check below that fails BEFORE the CLAIMED->SENDING boundary
        safely releases the row back to QUEUED (no provider call has
        happened yet -- see MailEnrollmentStepStatus.CLAIMED's docstring).
        Once SENDING is reached, a raised exception from the sender leaves
        the row exactly there, untouched, for reap_orphans() -- this method
        never guesses at an uncertain outcome.

        Checklist order (see the approved Phase A spec's 8-point runtime
        safety list, plus Correction 1's daily_lead_start_limit addition):
        1. campaign ACTIVE
        2. enrollment ACTIVE
        3. claim QUEUED->CLAIMED succeeds (no lost race)
        4. mailbox assigned + still selected + still CONNECTED
        5. mailbox daily_send_limit not exceeded (UTC calendar day)
        6. mailbox pacing (min_seconds_between_sends) satisfied
        7. [step 1 only] daily_lead_start_limit not exceeded (campaign-local
           calendar day)
        8. live suppression check
        9. current instant still inside a legal send window (fresh
           resolver re-check -- a stale next_send_at<=now is never trusted
           alone)
        """
        campaign = await self.campaign_store.get(step.mail_campaign_id)
        if campaign is None or campaign.status != MailCampaignStatus.ACTIVE:
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.CAMPAIGN_NOT_ACTIVE)

        enrollment = await self.enrollment_store.get(step.enrollment_id)
        if enrollment is None or enrollment.status != MailEnrollmentStatus.ACTIVE:
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.ENROLLMENT_NOT_ACTIVE)

        claimed_step = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.CLAIMED,
                "claimed_by": claimed_by,
                "claimed_at": now,
                "updated_at": now,
            }
        )
        claim_applied = await self.step_store.try_transition(
            step.enrollment_step_id, MailEnrollmentStepStatus.QUEUED, claimed_step
        )
        if not claim_applied:
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.LOST_CLAIM_RACE)
        step = claimed_step

        try:
            enrollment = await self.assign_mailbox_if_needed(enrollment)
        except NoUsableMailboxError:
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.NO_USABLE_MAILBOX)

        assert enrollment.assigned_mailbox_id is not None
        mailbox = await self._selected_and_connected(campaign.mail_campaign_id, enrollment.assigned_mailbox_id)
        if mailbox is None:
            paused = enrollment.model_copy(update={"status": MailEnrollmentStatus.PAUSED})
            await self.enrollment_store.save(paused)
            await self.activity_log.record(
                event_type="mail_enrollment.paused",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary="A lead's mail sequence was paused because their assigned mailbox is no longer available.",
                entity_type="mail_campaign",
                entity_id=campaign.mail_campaign_id,
            )
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE)

        policy = await self.resolve_mailbox_send_policy(mailbox.mailbox_id)
        day_start = _utc_day_start(now)
        sent_today = await self.step_store.count_sent_for_mailbox_since(mailbox.mailbox_id, day_start)
        if sent_today >= policy.daily_send_limit:
            await self._release_to_queued(step, day_start + timedelta(days=1), now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.MAILBOX_DAILY_LIMIT_REACHED)

        last_sent = await self.step_store.get_most_recent_sent_for_mailbox(mailbox.mailbox_id)
        if last_sent is not None and last_sent.sent_at is not None:
            earliest_next = last_sent.sent_at + timedelta(seconds=policy.min_seconds_between_sends)
            if now < earliest_next:
                await self._release_to_queued(step, earliest_next, now)
                return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.MAILBOX_PACING_NOT_SATISFIED)

        if step.step_number == 1 and campaign.daily_lead_start_limit is not None:
            local_day_start = _campaign_local_day_start(now, timezone_name)
            started_today = await self.step_store.count_sent_step_for_campaign_since(
                campaign.mail_campaign_id, 1, local_day_start
            )
            if started_today >= campaign.daily_lead_start_limit:
                next_local_day = local_day_start + timedelta(days=1)
                retry_at = resolve_next_send_time(windows, timezone_name, next_local_day)
                await self._release_to_queued(step, retry_at, now)
                return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.LEAD_START_LIMIT_REACHED)

        suppression = await self.suppression_store.get(normalize_email(enrollment.email_at_enrollment))
        if suppression is not None and suppression.active:
            await self.suppress_enrollment(enrollment, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.RECIPIENT_SUPPRESSED)

        if not is_within_window(windows, timezone_name, now):
            retry_at = resolve_next_send_time(windows, timezone_name, now)
            await self._release_to_queued(step, retry_at, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.OUTSIDE_SEND_WINDOW)

        sending_step = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.SENDING,
                "mailbox_id": mailbox.mailbox_id,
                "attempt_count": step.attempt_count + 1,
                "last_attempt_at": now,
                "updated_at": now,
            }
        )
        sending_applied = await self.step_store.try_transition(
            step.enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, sending_step
        )
        if not sending_applied:
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.LOST_CLAIM_RACE)

        # B2 hardening pass: MailSenderPort.send() now requires a
        # MailSendRequest carrying its own rfc_message_id (see that
        # dataclass's docstring for why). Generating it HERE, inline,
        # keeps every existing write and test assertion in this method
        # byte-for-byte unchanged -- it is deliberately NOT added to
        # `sending_step`'s update dict above, so it is NOT persisted
        # before this CLAIMED->SENDING transition. That means the real
        # durability guarantee MailSendRequest exists to enable
        # ("generate + persist BEFORE SENDING, so a crash mid-send is
        # recoverable by re-reading the row") is still NOT built here --
        # this call site only adapts to the new required TYPE, it does
        # not change WHEN anything is written. Reordering this method's
        # own writes to close that gap for real is explicitly Phase C's
        # job (see MailSendRequest's docstring).
        send_request = MailSendRequest(
            mailbox=mailbox,
            to_email=enrollment.email_at_enrollment,
            subject=sending_step.subject,
            body=sending_step.body,
            rfc_message_id=generate_rfc_message_id(mailbox.email.rsplit("@", 1)[-1]),
            reply_in_thread=sending_step.reply_in_thread,
        )
        try:
            result = await sender.send(send_request)
        except Exception as e:
            # Provider call outcome is UNKNOWN -- never guess. The row stays
            # in SENDING; reap_orphans() is the only thing that ever moves
            # it forward from here, and only ever to UNKNOWN, never SENT.
            return ProcessOutcome(sent=False, sender_error=str(e))

        await self.record_send_success(
            step=sending_step,
            send_result=result,
            sequence_steps=sequence_steps,
            enrollment=enrollment,
            windows=windows,
            timezone_name=timezone_name,
            now=now,
        )
        await self.maybe_complete_campaign(campaign.mail_campaign_id, now)
        return ProcessOutcome(sent=True)

    # --- Orphan reaping ----------------------------------------------------

    async def reap_orphans(
        self,
        now: datetime,
        *,
        claimed_timeout_seconds: int = CLAIMED_ORPHAN_TIMEOUT_SECONDS,
        sending_timeout_seconds: int = SENDING_ORPHAN_TIMEOUT_SECONDS,
    ) -> ReapResult:
        """CLAIMED rows older than `claimed_timeout_seconds` are safely
        auto-reset to QUEUED -- no provider call has happened yet in that
        state, so this is never ambiguous (see MailEnrollmentStepStatus.
        CLAIMED's docstring). SENDING rows older than
        `sending_timeout_seconds` are moved to UNKNOWN -- NEVER back to
        QUEUED, and never auto-retried by anything in this codebase; a
        human must reconcile them (see MailEnrollmentStepStatus.
        SENDING/UNKNOWN's docstrings)."""
        reset_to_queued = 0
        for row in await self.step_store.list_stale_claimed(now - timedelta(seconds=claimed_timeout_seconds)):
            updated = row.model_copy(
                update={
                    "status": MailEnrollmentStepStatus.QUEUED,
                    "claimed_by": None,
                    "claimed_at": None,
                    "updated_at": now,
                }
            )
            if await self.step_store.try_transition(row.enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, updated):
                reset_to_queued += 1

        marked_unknown = 0
        for row in await self.step_store.list_stale_sending(now - timedelta(seconds=sending_timeout_seconds)):
            updated = row.model_copy(update={"status": MailEnrollmentStepStatus.UNKNOWN, "updated_at": now})
            if await self.step_store.try_transition(row.enrollment_step_id, MailEnrollmentStepStatus.SENDING, updated):
                marked_unknown += 1

        return ReapResult(reset_to_queued=reset_to_queued, marked_unknown=marked_unknown)
