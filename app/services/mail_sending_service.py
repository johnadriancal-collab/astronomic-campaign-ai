"""
MailSendingService -- Astronomic Mail's durable execution engine. Owns
every status-gated mutation of MailEnrollmentStep/MailEnrollment rows
once a campaign is ACTIVE: creating Step 1 on activation, claiming a due
row, running the full pre-send safety checklist, recording success,
cascading suppression, and reaping orphaned claims.

`prepare_and_send_step()` is the ONE canonical execution path -- see its
own docstring -- and the only method that reaches the CLAIMED->SENDING
boundary and calls out to a `MailSenderPort`. `process_one_due_step()` is
a thin delegator to it, kept only for its simpler call signature (no
worker-lease callback); there is no second, independently-maintained
execution algorithm anywhere in this module. This service itself still
NEVER sends a real email and NEVER imports gmail/smtp/oauth-shaped code
(see tests/test_mail_sending_safety.py's static enforcement of that
boundary) -- it only calls the abstract `MailSenderPort` it's given.
MailExecutionWorker (app/services/mail_execution_worker.py) is the real
Phase C worker that drives prepare_and_send_step() on a schedule, gated
by mail_sending_engine_enabled, the controlled-test allowlists, and a
database worker lease -- see that module's own docstring.

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
that gap, not a stopgap pretending atomicity exists. MailExecutionWorker
runs reconcile_stalled_progressions() automatically and periodically
across every ACTIVE campaign as part of its own recovery sweep -- a human
manually invoking it is not an acceptable substitute.
"""

import hashlib
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from app.config import settings
from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentPauseReason,
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
from app.services.mail_unsubscribe_composition import PublicOriginNotConfiguredError, compose_outbound_email
from app.services.mailbox_service import MailboxCredentialMissingError, MailboxNotFound
from app.services.rfc_message_id import generate_rfc_message_id
from app.services.unsubscribe_token import UnsubscribeTokenNotConfiguredError

# --- Placeholder safety defaults --------------------------------------------
# None of these numbers are product-approved -- they exist only so the
# capacity/pacing/orphan-reaping logic below has SOMETHING to enforce and
# is fully testable now. Real values are a Phase C/product decision.

DEFAULT_MAILBOX_DAILY_SEND_LIMIT = 100
DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS = 30
CLAIMED_ORPHAN_TIMEOUT_SECONDS = 300
SENDING_ORPHAN_TIMEOUT_SECONDS = 900

# --- Phase C retry/backoff for DEFINITELY_NOT_SENT, retryable failures -----
# PROVISIONAL OPERATIONAL DEFAULTS -- explicitly NOT derived from
# measurement or product sign-off, matching the exact same caveat as the
# mailbox-policy defaults above. A step gets at most
# DEFINITELY_NOT_SENT_MAX_ATTEMPTS real send attempts (counted via the
# existing `attempt_count` field, incremented once per CLAIMED->SENDING
# crossing -- unchanged since Phase A) before a retryable
# DEFINITELY_NOT_SENT failure gives up and moves the step to FAILED
# instead of retrying again. DEFINITELY_NOT_SENT_BACKOFF_SECONDS is
# indexed by `attempt_count - 1` (the attempt that just failed) and
# capped at the last entry if attempt_count ever exceeds its length --
# 1 minute, then 5 minutes, matching common "a few quick retries with
# growing backoff" defaults, not a measured value for THIS app's traffic.
DEFINITELY_NOT_SENT_MAX_ATTEMPTS = 3
DEFINITELY_NOT_SENT_BACKOFF_SECONDS = (60, 300, 1800)  # 1m, 5m, 30m

# --- Phase C PREPARE-failure taxonomy defaults ------------------------------
# Also PROVISIONAL -- same disclaimer as above. Bounds how many CONSECUTIVE
# transient PREPARE-phase failures (see MailEnrollmentStep.
# prepare_failure_count -- a field distinct from attempt_count, which only
# ever counts provider/SENDING attempts) a step will absorb, with the same
# growing-backoff shape as the post-SENDING retry policy, before the
# enrollment is moved to PAUSED(PREPARE_TRANSIENT_EXHAUSTED) -- a state
# with NO automatic recovery path (see that pause reason's own docstring
# for why granting a fresh budget on every periodic sweep would defeat
# the entire point of "bounded") -- rather than retried on every single
# worker poll tick indefinitely. See MailSendingService.
# _handle_prepare_failure()'s own docstring for the full PREPARE failure
# taxonomy this belongs to.
PREPARE_TRANSIENT_MAX_ATTEMPTS = 3
PREPARE_TRANSIENT_BACKOFF_SECONDS = (60, 300, 1800)  # 1m, 5m, 30m

# --- Local duplicate of GMAIL_SEND_SCOPE (app/google/oauth_client.py) ------
# This module is statically forbidden from importing anything gmail/oauth-
# shaped (see tests/test_mail_sending_safety.py's AST-level enforcement) --
# the SAME deliberate workaround already used for GoogleRefreshTokenInvalid
# Error/GmailScopeMissingError below (matching by exception class NAME, a
# string, never an isinstance/import). The scope string itself is not a
# secret and does not change without a code change on the oauth_client.py
# side too, so a literal duplicate here is the smallest safe alternative to
# violating the import boundary.
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# --- Phase C worker polling/recovery defaults -------------------------------
# Also PROVISIONAL -- see MailExecutionWorker's own module docstring
# (app/services/mail_execution_worker.py) for where these are actually
# consumed. Documented here, next to the other placeholder constants, so
# every "not yet product-approved" number in this phase lives under the
# same explicit disclaimer rather than being buried in a different file.
WORKER_POLL_INTERVAL_SECONDS = 45
WORKER_DUE_ROW_BATCH_SIZE = 25
WORKER_RECOVERY_INTERVAL_SECONDS = 300
WORKER_LEASE_DURATION_SECONDS = 90


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


class UnknownStepNotFoundError(Exception):
    def __init__(self, enrollment_step_id: str):
        self.enrollment_step_id = enrollment_step_id
        super().__init__(f"MailEnrollmentStep not found (or its enrollment is missing): {enrollment_step_id}")


class UnknownStepWrongStatusError(Exception):
    """Raised when resolve_unknown_step_confirmed_sent()/
    _confirmed_not_sent() is called against a row that is NOT currently
    UNKNOWN -- resolution is only ever permitted FROM UNKNOWN, never from
    any other status (there's nothing to manually resolve about a row
    that's already SENT/FAILED/QUEUED/etc., and allowing it would risk
    silently overwriting a status some OTHER process already resolved)."""

    def __init__(self, enrollment_step_id: str, actual_status: MailEnrollmentStepStatus):
        self.enrollment_step_id = enrollment_step_id
        self.actual_status = actual_status
        super().__init__(f"MailEnrollmentStep {enrollment_step_id} is {actual_status.value}, not unknown.")


class PrepareBlockedWrongStateError(Exception):
    """Raised when resolve_prepare_blocked_step() is called against an
    enrollment that is NOT currently PAUSED(PREPARE_TRANSIENT_EXHAUSTED)
    or PAUSED(PREPARE_UNCLASSIFIED_BLOCKED) -- this manual recovery
    action is only ever permitted from one of those two specific states,
    matching UnknownStepWrongStatusError's own "no silent overwrite of
    some other process's outcome" principle."""

    def __init__(
        self,
        enrollment_step_id: str,
        actual_status: MailEnrollmentStatus,
        actual_paused_reason: "MailEnrollmentPauseReason | None",
    ):
        self.enrollment_step_id = enrollment_step_id
        self.actual_status = actual_status
        self.actual_paused_reason = actual_paused_reason
        super().__init__(
            f"MailEnrollmentStep {enrollment_step_id}'s enrollment is {actual_status.value}"
            f"{f'({actual_paused_reason.value})' if actual_paused_reason else ''}, not blocked on preparation."
        )


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
    subclass so a Phase C caller can inspect `.certainty` (see
    SendOutcomeCertainty above) -- NOT enforced by MailSenderPort.send()'s
    type signature (a concrete sender may still raise a bare Exception;
    prepare_and_send_step() treats that identically to any MailSendError,
    per that method's own contract). Defaults conservatively to
    OUTCOME_UNKNOWN -- a subclass must explicitly opt INTO
    DEFINITELY_NOT_SENT, never the reverse, matching this module's
    established "be conservative around ambiguity" principle (see
    MailEnrollmentStepStatus.UNKNOWN's docstring).

    `retryable` (Phase C addition): ONLY consulted when `.certainty` is
    DEFINITELY_NOT_SENT -- meaningless otherwise (an OUTCOME_UNKNOWN
    failure is never auto-retried regardless of this flag; see
    prepare_and_send_step()'s post-SENDING error handling). Defaults
    conservatively to False -- a subclass must explicitly opt INTO being
    safely retryable (e.g. a rate limit, a transient connection failure),
    never the reverse. A DEFINITELY_NOT_SENT failure that's also
    `retryable=False` (e.g. a missing permission/scope, a malformed
    request Gmail rejected outright) goes straight to FAILED -- retrying
    an identical request would fail identically."""

    certainty: SendOutcomeCertainty = SendOutcomeCertainty.OUTCOME_UNKNOWN
    retryable: bool = False


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

    NOW WIRED (hardening pass): MailSendingService.prepare_and_send_step()
    -- the ONE canonical execution path, see that method's own docstring
    -- implements the "persist before SENDING" write for real, and
    process_one_due_step() is a thin delegator to it (see that method's
    own docstring), so this invariant applies through either name.

    MANDATORY PHASE C INVARIANT (now enforced by prepare_and_send_step()
    -- see that method's own docstring for exactly where each step
    happens): before any provider invocation becomes possible, execution
    MUST perform this exact durable sequence, in this exact order:
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
    Verified by tests/test_prepare_and_send_step.py's Message-ID-
    durability section: the persisted ID survives every PREPARE failure/
    retry/recovery path (a transient failure, an exhausted-transient
    escalation, a configuration block, a stale-CLAIMED reap), never
    silently regenerated.

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
    # Phase C (B3 wiring): both set together or neither -- see
    # app/services/mail_unsubscribe_composition.py's compose_outbound_email(),
    # the ONLY intended source of these two values. `body` above is expected
    # to ALREADY be the footer-appended composed body by the time a real
    # MailSendRequest reaches a sender -- this dataclass has no way to
    # enforce that itself (it has no knowledge of "the original snapshot"),
    # so that discipline lives entirely in the caller
    # (MailSendingService.prepare_and_send_step()).
    list_unsubscribe_header: str | None = None
    list_unsubscribe_post_header: str | None = None
    # Phase C/D: the HTML alternative of `body` above -- same snapshot,
    # same unsubscribe token/URL, see app/services/
    # mail_unsubscribe_composition.py's compose_outbound_email() (the only
    # intended source). None (the default) means "plain-text only,"
    # producing byte-for-byte the same single-part message as before this
    # field existed -- see app/google/gmail_mime.py's build_mime_message().
    html_body: str | None = None

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

        if (self.list_unsubscribe_header is None) != (self.list_unsubscribe_post_header is None):
            raise MailSendRequestValidationError(
                "list_unsubscribe_header and list_unsubscribe_post_header must be given together or not "
                "at all -- RFC 8058 one-click support is a single header pair, never a valid partial one "
                "(matches build_mime_message()'s own rule in app/google/gmail_mime.py)."
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
    first one -- GmailSender (app/google/gmail_sender.py). Still NOT
    reachable in production: mail_sending_engine_enabled defaults False,
    and Phase C's worker (app/services/mail_execution_worker.py) is
    itself gated by the controlled-test allowlists and the worker lease
    before it would ever invoke a real sender (see GmailSender's own
    module docstring, and tests/test_gmail_sending_safety.py, which is
    what actually enforces that).

    PHASE C PROVIDER-BOUNDARY SPLIT: `prepare()`/`send_prepared()` are the
    real contract now -- see PreparedGmailSend's docstring precedent in
    app/google/gmail_sender.py for the full rationale (short version: an
    OAuth-refresh failure inside a combined send() looked identical to a
    genuine post-SENDING provider failure, which is wrong -- invalid_grant
    PROVES Gmail's send endpoint was never invoked). `send()` remains a
    concrete convenience method on this ABC (prepare() then
    send_prepared()) for callers/tests that don't need to straddle the
    CLAIMED->SENDING transition -- MailSendingService.
    prepare_and_send_step() (Phase C's real execution path) calls
    prepare()/send_prepared() SEPARATELY, never this combined method."""

    @abstractmethod
    async def prepare(self, request: MailSendRequest) -> object:
        """Everything that can fail for a definitely-pre-send reason:
        OAuth/credential refresh, scope validation, MIME/request
        construction. Must NEVER call the actual provider send endpoint.
        Returns an opaque, implementation-specific "prepared" object
        (e.g. GmailSender's PreparedGmailSend) that send_prepared() below
        can act on without doing any more preparation work. May raise
        anything -- a prepare() failure is, by definition, provably
        pre-send, so callers release the row back to QUEUED rather than
        treating it as provider-uncertain."""

    @abstractmethod
    async def send_prepared(self, prepared: object) -> SendResult:
        """The ONLY method that may be called after the CLAIMED->SENDING
        transition. Must do exactly one thing: the actual provider call.
        Must raise on any failure or uncertain outcome -- never return a
        synthetic/partial SendResult. prepare_and_send_step() treats a
        raised exception here as "provider call outcome unknown" UNLESS
        it exposes `.certainty == DEFINITELY_NOT_SENT` (see
        SendOutcomeCertainty above), in which case a retry/FAILED policy
        applies instead of leaving the row in SENDING for orphan-reaping."""

    async def send(self, request: MailSendRequest) -> SendResult:
        """Concrete convenience wrapper -- prepare() then send_prepared().
        NOT used by Phase C's real execution path (see this class's own
        docstring); kept for simpler callers/tests and for MailSenderPort
        implementations that don't need the split (e.g. a trivial fake)."""
        return await self.send_prepared(await self.prepare(request))


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
    # --- Phase C additions ---
    NOT_LEADER = "not_leader"
    CONTROLLED_TEST_NOT_ALLOWED = "controlled_test_not_allowed"
    DEFINITELY_NOT_SENT_RETRY = "definitely_not_sent_retry"
    PROVIDER_PERMANENTLY_REJECTED = "provider_permanently_rejected"
    # --- PREPARE-failure taxonomy (hardening pass) -- see
    # MailSendingService._handle_prepare_failure()'s own docstring for
    # the full classification these correspond to. Mailbox-related
    # PREPARE failures (invalid_grant, missing gmail.send scope, a
    # missing mailbox/credential row) deliberately reuse
    # ASSIGNED_MAILBOX_UNAVAILABLE above, not a value here -- they are,
    # structurally, the exact same "this mailbox needs attention" outcome
    # as the mid-flow and final mailbox-availability checks.
    PREPARE_TRANSIENT_RETRY = "prepare_transient_retry"
    # Second hardening pass: PREPARE_BLOCKED split into three distinct
    # reasons -- a single value could not represent "has an automatic
    # recovery signal, and what it is" without collapsing genuinely
    # different recovery semantics together (see MailEnrollmentPauseReason's
    # own docstring, which mirrors this same split one-to-one).
    PREPARE_CONFIG_BLOCKED = "prepare_config_blocked"
    PREPARE_TRANSIENT_EXHAUSTED = "prepare_transient_exhausted"
    PREPARE_UNCLASSIFIED_BLOCKED = "prepare_unclassified_blocked"
    PREPARE_PERMANENTLY_INVALID = "prepare_permanently_invalid"


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


async def _always_leader() -> bool:
    """The `confirm_leadership` process_one_due_step() supplies to
    prepare_and_send_step() -- matches Phase A's original single-process,
    no-lease assumption exactly (there was never a worker-lease concept
    in scope for that call signature). A real Phase C worker binds its
    own WorkerLeaseService-backed callback instead (see
    MailExecutionWorker.tick())."""
    return True


def resolve_rfc_message_id(step: MailEnrollmentStep, mailbox: Mailbox) -> str:
    """Reuse the persisted ID if this row already has one (a retry/
    recovery pass reusing the SAME row) -- NEVER regenerate merely
    because a previous attempt was released back to QUEUED, hit a stale-
    CLAIMED reap, or is being retried after a DEFINITELY_NOT_SENT
    failure. This is the ONE place that decision is made -- see
    MailSendRequest's MANDATORY PHASE C INVARIANT docstring for why a
    single, un-duplicated call site matters."""
    return step.rfc_message_id or generate_rfc_message_id(mailbox.email.rsplit("@", 1)[-1])


def _parse_allowlist(raw: str | None, *, normalize: bool = False) -> set[str]:
    if not raw:
        return set()
    values = {v.strip() for v in raw.split(",") if v.strip()}
    if not normalize:
        return values
    normalized = {normalize_email(v) for v in values}
    return {v for v in normalized if v}


def controlled_test_send_allowed(mailbox_id: str, recipient_email: str) -> bool:
    """Phase C controlled-test gate -- see app/config.py's
    mail_sending_mailbox_allowlist/mail_sending_recipient_allowlist
    docstrings. FAIL CLOSED: either allowlist missing/empty means this
    returns False unconditionally -- an accidentally-enabled
    mail_sending_engine_enabled can NEVER, by itself, permit a real
    provider invocation. No wildcards. Exact match only (mailbox_id
    as-is; recipient_email normalized the same way as everywhere else in
    this codebase -- see app.models.crm.normalize_email)."""
    mailbox_allowlist = _parse_allowlist(settings.mail_sending_mailbox_allowlist)
    recipient_allowlist = _parse_allowlist(settings.mail_sending_recipient_allowlist, normalize=True)
    if not mailbox_allowlist or not recipient_allowlist:
        return False
    if mailbox_id not in mailbox_allowlist:
        return False
    return normalize_email(recipient_email) in recipient_allowlist


def _prepare_configuration_satisfied() -> bool:
    """The known, deterministic operator-configuration preconditions
    compose_outbound_email() depends on (see _handle_prepare_failure()'s
    category-C docstring: PublicOriginNotConfiguredError,
    UnsubscribeTokenNotConfiguredError) -- both MUST currently read as
    present for resume_prepare_config_blocked_enrollments() to resume
    anything. A cheap, local, no-network PRESENCE check, deliberately NOT
    a full dry run of composition (that would mean actually constructing
    a MultiFernet from the configured keys, reaching into unsubscribe_
    token.py's private internals for no real safety gain). A value that
    is present but malformed (e.g. a syntactically-invalid Fernet key)
    would still read as "satisfied" here and would immediately re-block
    on the very next real attempt -- a single wasted resume, never a
    repeating blind loop, which is the actual property this function
    exists to guarantee (see the pause reason it gates for the full
    reasoning)."""
    return bool(settings.public_backend_origin) and bool(settings.unsubscribe_token_encryption_keys)


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
        expected_status: MailEnrollmentStepStatus = MailEnrollmentStepStatus.SENDING,
    ) -> bool:
        """`step` must currently be `expected_status` (SENDING by default
        -- process_one_due_step()/prepare_and_send_step()'s own prior
        claim). `expected_status` is a Phase C addition so
        resolve_unknown_step_confirmed_sent() can reuse this EXACT
        progression/completion logic for the UNKNOWN->SENT path
        (`expected_status=UNKNOWN`) instead of duplicating it -- this
        codebase's established principle that there is only ONE
        implementation of "what happens after a step is confirmed sent,"
        never two that could drift apart (see reconcile_stalled_
        progression()'s own docstring for the same principle applied
        elsewhere). Applies the transition to SENT, then decides
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
        applied = await self.step_store.try_transition(step.enrollment_step_id, expected_status, sent_step)
        if not applied:
            return False

        # Structural "a step was sent" event -- separate from and in
        # addition to the enrollment/campaign-completion events below,
        # per the approved observability requirements. No recipient PII
        # (see this module's own privacy note): only IDs.
        await self.activity_log.record(
            event_type="mail_enrollment_step.sent",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f"Sequence step {step.step_number} sent.",
            entity_type="mail_enrollment_step",
            entity_id=sent_step.enrollment_step_id,
            metadata={"mail_campaign_id": step.mail_campaign_id, "step_number": step.step_number},
        )

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

    # --- The runtime safety checklist + one send attempt -----------------------

    async def _pause_enrollment_for_mailbox(self, enrollment: MailEnrollment, mail_campaign_id: str, now: datetime) -> None:
        """The ONE place an enrollment is paused for MAILBOX_UNAVAILABLE --
        used by every mailbox-related PREPARE failure (invalid_grant,
        missing gmail.send scope, a missing mailbox/credential row -- see
        _handle_prepare_failure()) and every mailbox-availability check in
        prepare_and_send_step() (the mid-flow one and the final, fresh
        pre-SENDING recheck). Setting `paused_reason` here is what lets
        resume_mailbox_paused_enrollments() safely distinguish this
        specific, auto-recoverable pause from any other reason an
        enrollment might be PAUSED for -- see MailEnrollmentPauseReason's
        own docstring."""
        paused = enrollment.model_copy(
            update={"status": MailEnrollmentStatus.PAUSED, "paused_reason": MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE}
        )
        await self.enrollment_store.save(paused)
        await self.activity_log.record(
            event_type="mail_enrollment.paused",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary="A lead's mail sequence was paused because their assigned mailbox is no longer available.",
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
        )

    async def _pause_enrollment_for_prepare_blocked(
        self,
        enrollment: MailEnrollment,
        mail_campaign_id: str,
        now: datetime,
        *,
        reason: MailEnrollmentPauseReason,
        summary: str,
    ) -> None:
        """The PREPARE-failure counterpart to _pause_enrollment_for_mailbox()
        -- used for all three PREPARE-blocked pause reasons (see
        MailEnrollmentPauseReason's own docstring and
        _handle_prepare_failure() for exactly which failures route to
        which reason). `reason` and `summary` are supplied by the caller
        rather than hardcoded here, because -- unlike
        _pause_enrollment_for_mailbox()'s single always-auto-recoverable
        case -- these three reasons have genuinely different recovery
        semantics (see resume_prepare_config_blocked_enrollments() vs.
        resolve_prepare_blocked_step()) and must not be collapsed back
        into one generic message."""
        paused = enrollment.model_copy(update={"status": MailEnrollmentStatus.PAUSED, "paused_reason": reason})
        await self.enrollment_store.save(paused)
        await self.activity_log.record(
            event_type="mail_enrollment.paused",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=summary,
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
            metadata={"paused_reason": reason.value},
        )

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

    async def _fail_step_and_enrollment(
        self,
        step: MailEnrollmentStep,
        from_status: MailEnrollmentStepStatus,
        mail_campaign_id: str,
        error: Exception,
        now: datetime,
        *,
        blocked_reason: SendBlockReason,
        activity_summary: str,
    ) -> ProcessOutcome:
        """Shared terminal-failure tail: `step` (currently `from_status`,
        either SENDING for a confirmed post-SENDING provider rejection or
        CLAIMED for a PREPARE-phase failure proven permanently invalid
        before any provider call) -> FAILED, and -- iff the enrollment is
        still ACTIVE -- the whole enrollment -> FAILED too, so no later
        sequence step is ever materialized for a step that's permanently
        undeliverable. Used by both _handle_definitely_not_sent() (from
        SENDING) and _handle_prepare_failure()'s permanent-invalid branch
        (from CLAIMED) -- there is only ONE implementation of "what
        permanently failing a step/enrollment means," never two that
        could drift apart."""
        failed_step = step.model_copy(update={"status": MailEnrollmentStepStatus.FAILED, "last_error": str(error)[:500], "updated_at": now})
        await self.step_store.try_transition(step.enrollment_step_id, from_status, failed_step)

        enrollment = await self.enrollment_store.get(step.enrollment_id)
        if enrollment is not None and enrollment.status == MailEnrollmentStatus.ACTIVE:
            failed_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.FAILED})
            await self.enrollment_store.save(failed_enrollment)
            await self.activity_log.record(
                event_type="mail_enrollment.failed",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary=activity_summary,
                entity_type="mail_campaign",
                entity_id=mail_campaign_id,
            )
        return ProcessOutcome(sent=False, blocked_reason=blocked_reason, sender_error=str(error))

    async def _handle_transient_prepare_failure(
        self, step: MailEnrollmentStep, enrollment: MailEnrollment, mail_campaign_id: str, error: Exception, now: datetime
    ) -> ProcessOutcome:
        """Category B of _handle_prepare_failure()'s taxonomy: a PREPARE-
        phase failure believed to be transient (a token-endpoint network
        blip, a malformed-but-probably-retriable response) -- bounded by
        MailEnrollmentStep.prepare_failure_count (deliberately NOT
        attempt_count, which only ever counts provider/SENDING attempts --
        see that field's own docstring) and PREPARE_TRANSIENT_MAX_ATTEMPTS,
        with the same growing-backoff shape as the post-SENDING retry
        policy. Exhausting the budget does NOT fail the step/enrollment --
        but it does NOT quietly retry forever either: it moves to
        PAUSED(PREPARE_TRANSIENT_EXHAUSTED), a state with deliberately NO
        automatic recovery path (see that reason's own docstring for why
        -- in short, there is no generic way to confirm a transient
        condition has actually stopped recurring, so granting a fresh
        budget on every periodic sweep would silently turn "bounded"
        into "unbounded, one exhaustion cycle at a time"). Recoverable
        ONLY via resolve_prepare_blocked_step() -- an explicit,
        authenticated action."""
        new_count = step.prepare_failure_count + 1
        if new_count < PREPARE_TRANSIENT_MAX_ATTEMPTS:
            backoff_index = min(new_count - 1, len(PREPARE_TRANSIENT_BACKOFF_SECONDS) - 1)
            retry_at = now + timedelta(seconds=PREPARE_TRANSIENT_BACKOFF_SECONDS[backoff_index])
            released = step.model_copy(
                update={
                    "status": MailEnrollmentStepStatus.QUEUED,
                    "claimed_by": None,
                    "claimed_at": None,
                    "next_send_at": retry_at,
                    "prepare_failure_count": new_count,
                    "last_error": str(error)[:500],
                    "updated_at": now,
                }
            )
            await self.step_store.try_transition(step.enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, released)
            await self.activity_log.record(
                event_type="mail_enrollment_step.prepare_retry_scheduled",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary=f"Message preparation failed in a way that's likely transient (attempt {new_count}); "
                f"retrying at {retry_at.isoformat()}.",
                entity_type="mail_enrollment_step",
                entity_id=step.enrollment_step_id,
                metadata={"mail_campaign_id": mail_campaign_id, "prepare_failure_count": new_count},
            )
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.PREPARE_TRANSIENT_RETRY, sender_error=str(error))

        released = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.QUEUED,
                "claimed_by": None,
                "claimed_at": None,
                "next_send_at": step.next_send_at or now,
                "prepare_failure_count": new_count,
                "last_error": str(error)[:500],
                "updated_at": now,
            }
        )
        await self.step_store.try_transition(step.enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, released)
        await self._pause_enrollment_for_prepare_blocked(
            enrollment,
            mail_campaign_id,
            now,
            reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED,
            summary=f"A lead's mail sequence was paused after {new_count} consecutive transient message-"
            "preparation failures. This requires an explicit recovery action -- it will NOT be retried "
            "automatically.",
        )
        return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.PREPARE_TRANSIENT_EXHAUSTED, sender_error=str(error))

    async def _handle_prepare_failure(
        self, step: MailEnrollmentStep, enrollment: MailEnrollment, mail_campaign_id: str, error: Exception, now: datetime
    ) -> ProcessOutcome:
        """The ONE PREPARE-failure classifier -- called from BOTH
        compose_outbound_email()'s except block and sender.prepare()'s
        except block in prepare_and_send_step(), so there is exactly one
        place this taxonomy is decided, never two that could drift apart.
        `step` must currently be CLAIMED; no provider call has been made
        by construction (this only ever runs before the CLAIMED->SENDING
        transition) -- so no branch below may ever produce UNKNOWN.

        A. Mailbox reauthorization (recoverable via the SAME mailbox
           reconnecting/upgrading): GoogleRefreshTokenInvalidError,
           GmailScopeMissingError (the mailbox's CURRENT granted_scopes
           lack gmail.send -- structurally the same "this mailbox needs
           attention" outcome as a lapsed connection, recoverable via the
           Gmail upgrade flow), MailboxNotFound/MailboxCredentialMissing
           Error (the assigned mailbox's row/credential vanished after
           this attempt's own earlier availability check -- an
           extraordinarily narrow race in a single-worker system with no
           mailbox-delete feature, but routed the same way rather than
           either failing the enrollment or retrying forever). ->
           PAUSED(MAILBOX_UNAVAILABLE), step released to QUEUED,
           `rfc_message_id` untouched (already persisted before this
           method is ever called -- see prepare_and_send_step()).
        B. Transient (GoogleTokenRefreshError and its subclasses other
           than GoogleRefreshTokenInvalidError -- e.g. a token-endpoint
           network blip or a malformed-but-likely-retriable response) ->
           _handle_transient_prepare_failure() (bounded retry, then
           PAUSED(PREPARE_TRANSIENT_EXHAUSTED) -- no automatic recovery).
        C. Recoverable configuration/precondition failure
           (PublicOriginNotConfiguredError, UnsubscribeTokenNotConfigured
           Error -- a deterministic operator-configuration gap that will
           not resolve by retrying quickly) -> PAUSED(PREPARE_CONFIG_
           BLOCKED) immediately, no bounded-retry-first (retrying a value
           we already know is absent serves no purpose). Auto-recoverable
           ONLY once the missing setting(s) are actually present again --
           see resume_prepare_config_blocked_enrollments().
        D. Permanently invalid preparation (any ValueError -- covers both
           HeaderInjectionError, app/google/gmail_mime.py's own
           ValueError subclass for a MIME header-injection attempt, and
           the bare ValueError generate_unsubscribe_token() raises for a
           recipient address that cannot support a token) -> step FAILED,
           enrollment FAILED (via _fail_step_and_enrollment()) -- retrying
           an identical request against an identical recipient would fail
           identically.
        Unclassified (anything not matching A-D): the SAME conservative
           treatment as B's exhaustion -- PAUSED(PREPARE_UNCLASSIFIED_
           BLOCKED), no automatic recovery, since there is no safe
           generic signal that an unrecognized failure has been fixed.
           Recoverable ONLY via resolve_prepare_blocked_step()."""
        names = {cls.__name__ for cls in type(error).__mro__}

        if "GoogleRefreshTokenInvalidError" in names or "GmailScopeMissingError" in names or isinstance(
            error, (MailboxNotFound, MailboxCredentialMissingError)
        ):
            await self._pause_enrollment_for_mailbox(enrollment, mail_campaign_id, now)
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE, sender_error=str(error))

        if isinstance(error, ValueError):
            return await self._fail_step_and_enrollment(
                step,
                MailEnrollmentStepStatus.CLAIMED,
                mail_campaign_id,
                error,
                now,
                blocked_reason=SendBlockReason.PREPARE_PERMANENTLY_INVALID,
                activity_summary="A lead's mail sequence permanently failed -- the message could not be prepared "
                "for a reason that will not change on retry (before any provider call was made). No further "
                "steps will be sent.",
            )

        if isinstance(error, (PublicOriginNotConfiguredError, UnsubscribeTokenNotConfiguredError)):
            await self._pause_enrollment_for_prepare_blocked(
                enrollment,
                mail_campaign_id,
                now,
                reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED,
                summary="A lead's mail sequence was paused because a required application configuration value "
                "is currently missing. It will resume automatically once that configuration is restored.",
            )
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.PREPARE_CONFIG_BLOCKED, sender_error=str(error))

        if "GoogleTokenRefreshError" in names:
            return await self._handle_transient_prepare_failure(step, enrollment, mail_campaign_id, error, now)

        # Unclassified -- conservative default, see this method's own
        # docstring: never fast-retry forever, never immediately FAILED,
        # and (per the second hardening pass) never blindly auto-resumed
        # either -- treated exactly like an exhausted transient budget.
        await self._pause_enrollment_for_prepare_blocked(
            enrollment,
            mail_campaign_id,
            now,
            reason=MailEnrollmentPauseReason.PREPARE_UNCLASSIFIED_BLOCKED,
            summary="A lead's mail sequence was paused because message preparation failed for an unrecognized "
            "reason. This requires an explicit recovery action -- it will NOT be retried automatically.",
        )
        await self._release_to_queued(step, step.next_send_at or now, now)
        return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.PREPARE_UNCLASSIFIED_BLOCKED, sender_error=str(error))

    async def resume_prepare_config_blocked_enrollments(self, now: datetime) -> int:
        """Phase C recovery sweep, PREPARE_CONFIG_BLOCKED-only counterpart
        to resume_mailbox_paused_enrollments() -- see MailEnrollmentPause
        Reason.PREPARE_CONFIG_BLOCKED's own docstring. Unlike the second
        hardening pass's PREPARE_TRANSIENT_EXHAUSTED/PREPARE_UNCLASSIFIED_
        BLOCKED (which this method never touches -- see
        resolve_prepare_blocked_step() for their only recovery path),
        there IS a cheap, local, no-network signal for this one specific
        reason: whether the known configuration prerequisites
        (_prepare_configuration_satisfied()) currently read as present.

        If that check is False, this is a complete no-op -- returns 0
        without reading or touching a single enrollment. This is the
        actual fix for the original bug: a genuinely-missing config value
        no longer gets "resumed" every WORKER_RECOVERY_INTERVAL_SECONDS
        only to immediately re-block; it stays paused until the
        prerequisite is truly satisfied, at which point EVERY enrollment
        blocked on it resumes together (this is a global application
        setting, not a per-mailbox one, so there is no finer-grained
        signal to check per enrollment).

        NEVER touches assigned_mailbox_id. Resets prepare_failure_count
        back to 0 on resume for symmetry/safety, even though category C
        failures don't themselves increment it. Returns the number of
        enrollments resumed. Idempotent -- safe to call repeatedly."""
        if not _prepare_configuration_satisfied():
            return 0

        campaigns = await self.campaign_store.list()
        resumed = 0
        for campaign in campaigns:
            if campaign.status != MailCampaignStatus.ACTIVE:
                continue
            enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
            for enrollment in enrollments:
                if enrollment.status != MailEnrollmentStatus.PAUSED:
                    continue
                if enrollment.paused_reason != MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED:
                    continue
                resumed_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE, "paused_reason": None})
                await self.enrollment_store.save(resumed_enrollment)
                for row in await self.step_store.list_for_enrollment(enrollment.enrollment_id):
                    if row.status == MailEnrollmentStepStatus.QUEUED and row.prepare_failure_count:
                        reset = row.model_copy(update={"prepare_failure_count": 0, "updated_at": now})
                        await self.step_store.try_transition(row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED, reset)
                await self.activity_log.record(
                    event_type="mail_enrollment.resumed",
                    category=ActivityCategory.MAIL,
                    source=ActivitySource.MAIL_SYSTEM,
                    summary="A lead's mail sequence resumed -- the application configuration it was blocked on "
                    "is now present.",
                    entity_type="mail_campaign",
                    entity_id=campaign.mail_campaign_id,
                )
                resumed += 1
        return resumed

    async def resolve_prepare_blocked_step(self, enrollment_step_id: str, *, now: datetime) -> bool:
        """Explicit, authenticated recovery for a step whose enrollment is
        PAUSED(PREPARE_TRANSIENT_EXHAUSTED) or PAUSED(PREPARE_UNCLASSIFIED_
        BLOCKED) -- see those two reasons' own docstrings for why they
        have NO automatic recovery path at all: there is no cheap, safe,
        generic signal that proves either condition has genuinely
        cleared. An operator who has independently confirmed the
        underlying issue is fixed (verified the upstream dependency
        recovered, diagnosed and fixed an unrecognized failure, or is
        deliberately giving a lead another chance) calls this explicitly
        -- e.g. via the session-gated POST /mail/execution/
        {enrollment_step_id}/resolve-prepare-blocked route.

        Resumes the enrollment to ACTIVE and resets prepare_failure_count
        to 0 -- but ONLY as a direct, explicit consequence of THIS call,
        never automatically. NEVER touches assigned_mailbox_id or
        rfc_message_id -- both survive completely untouched, matching
        every other recovery path in this module. Raises
        UnknownStepNotFoundError if the step/enrollment doesn't exist, or
        PrepareBlockedWrongStateError if the enrollment isn't currently
        PAUSED for one of these two specific reasons (resolution is only
        ever permitted FROM one of them, never from any other state --
        same "no silent overwrite of some other process's outcome"
        principle as resolve_unknown_step_confirmed_sent()'s own status
        guard)."""
        step = await self.step_store.get(enrollment_step_id)
        if step is None:
            raise UnknownStepNotFoundError(enrollment_step_id)
        enrollment = await self.enrollment_store.get(step.enrollment_id)
        if enrollment is None:
            raise UnknownStepNotFoundError(enrollment_step_id)

        recoverable_reasons = (
            MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED,
            MailEnrollmentPauseReason.PREPARE_UNCLASSIFIED_BLOCKED,
        )
        if enrollment.status != MailEnrollmentStatus.PAUSED or enrollment.paused_reason not in recoverable_reasons:
            raise PrepareBlockedWrongStateError(enrollment_step_id, enrollment.status, enrollment.paused_reason)

        resumed_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE, "paused_reason": None})
        await self.enrollment_store.save(resumed_enrollment)
        if step.status == MailEnrollmentStepStatus.QUEUED and step.prepare_failure_count:
            reset = step.model_copy(update={"prepare_failure_count": 0, "updated_at": now})
            await self.step_store.try_transition(step.enrollment_step_id, MailEnrollmentStepStatus.QUEUED, reset)

        await self.activity_log.record(
            event_type="mail_enrollment.prepare_manually_resumed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary="A lead's mail sequence was manually resumed after being blocked on message preparation.",
            entity_type="mail_campaign",
            entity_id=step.mail_campaign_id,
        )
        return True

    async def _fresh_mailbox_still_valid_for_sending(
        self, *, mail_campaign_id: str, enrollment_id: str, expected_mailbox_id: str
    ) -> Mailbox | None:
        """The final, load-bearing mailbox recheck, immediately before
        CLAIMED->SENDING -- see prepare_and_send_step()'s own docstring.
        Deliberately does NOT trust the `mailbox` object captured earlier
        in the SAME call (before sender.prepare() ran) as proof of
        current validity -- everything here is read fresh. Returns the
        mailbox iff EVERY one of the following still holds, else None:
        the enrollment's assigned_mailbox_id has not changed since this
        attempt started; the mailbox row still exists; its status is
        still CONNECTED; it is still selected on this campaign; it still
        carries the gmail.send grant. Does NOT re-check the controlled-
        test allowlists -- that is a separate, fresh
        controlled_test_send_allowed() call in the final safety cluster
        (see prepare_and_send_step()), kept distinct because it needs the
        recipient email too and has a different (release-to-queued, not
        pause) failure outcome."""
        fresh_enrollment = await self.enrollment_store.get(enrollment_id)
        if fresh_enrollment is None or fresh_enrollment.assigned_mailbox_id != expected_mailbox_id:
            return None
        mailbox = await self._selected_and_connected(mail_campaign_id, expected_mailbox_id)
        if mailbox is None:
            return None
        if _GMAIL_SEND_SCOPE not in mailbox.granted_scopes:
            return None
        return mailbox

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
        """Thin delegator to the ONE canonical execution path,
        prepare_and_send_step() -- kept only for this exact simpler
        call signature (no `confirm_leadership` parameter), which every
        existing Phase A test/call site already uses. There is no longer
        a second, independently-maintained execution algorithm behind
        this name: every safety behavior prepare_and_send_step()
        implements -- the controlled-test gate, Message-ID persistence,
        the PREPARE-failure taxonomy, the final mailbox recheck, the
        certainty-aware post-SENDING error handling -- applies identically
        through this entry point too. Supplies an always-true leadership
        check, matching the single-process, no-lease assumption this
        method's original callers were built under (see _always_leader()
        below); a real Phase C worker calls prepare_and_send_step()
        directly with its own WorkerLeaseService-bound callback instead.
        See tests/test_mail_sending_safety.py's
        test_process_one_due_step_delegates_to_the_canonical_path() for
        the static proof this stays true."""
        return await self.prepare_and_send_step(
            step,
            sender=sender,
            claimed_by=claimed_by,
            sequence_steps=sequence_steps,
            windows=windows,
            timezone_name=timezone_name,
            now=now,
            confirm_leadership=_always_leader,
        )

    # --- Phase C: the real execution entry point --------------------------------

    async def _handle_definitely_not_sent(
        self, sending_step: MailEnrollmentStep, error: Exception, now: datetime
    ) -> ProcessOutcome:
        """Applies the retry-or-FAILED policy for a post-SENDING error
        whose `.certainty` is DEFINITELY_NOT_SENT (see
        prepare_and_send_step()'s own certainty branch). `sending_step`
        must currently be SENDING. Retries (release back to QUEUED,
        `rfc_message_id` untouched -- see resolve_rfc_message_id()'s own
        docstring) iff the error opts into `.retryable` AND attempt_count
        hasn't yet reached DEFINITELY_NOT_SENT_MAX_ATTEMPTS; otherwise the
        step moves to FAILED and, per the approved design, so does the
        whole enrollment -- no later sequence step is ever materialized
        for a step that's permanently, confirmedly not deliverable."""
        retryable = getattr(error, "retryable", False)
        if retryable and sending_step.attempt_count < DEFINITELY_NOT_SENT_MAX_ATTEMPTS:
            backoff_index = min(sending_step.attempt_count - 1, len(DEFINITELY_NOT_SENT_BACKOFF_SECONDS) - 1)
            retry_at = now + timedelta(seconds=DEFINITELY_NOT_SENT_BACKOFF_SECONDS[backoff_index])
            released = sending_step.model_copy(
                update={
                    "status": MailEnrollmentStepStatus.QUEUED,
                    "claimed_by": None,
                    "claimed_at": None,
                    "next_send_at": retry_at,
                    "last_error": str(error)[:500],
                    "updated_at": now,
                }
            )
            await self.step_store.try_transition(sending_step.enrollment_step_id, MailEnrollmentStepStatus.SENDING, released)
            await self.activity_log.record(
                event_type="mail_enrollment_step.retry_scheduled",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary=f"Send attempt {sending_step.attempt_count} failed in a way that's safe to retry "
                f"(nothing was sent); retrying at {retry_at.isoformat()}.",
                entity_type="mail_enrollment_step",
                entity_id=sending_step.enrollment_step_id,
                metadata={"mail_campaign_id": sending_step.mail_campaign_id, "attempt_count": sending_step.attempt_count},
            )
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.DEFINITELY_NOT_SENT_RETRY, sender_error=str(error))

        return await self._fail_step_and_enrollment(
            sending_step,
            MailEnrollmentStepStatus.SENDING,
            sending_step.mail_campaign_id,
            error,
            now,
            blocked_reason=SendBlockReason.PROVIDER_PERMANENTLY_REJECTED,
            activity_summary="A lead's mail sequence permanently failed -- the provider confirmed it was not "
            "delivered and retries (if any) were exhausted. No further steps will be sent.",
        )

    async def prepare_and_send_step(
        self,
        step: MailEnrollmentStep,
        *,
        sender: MailSenderPort,
        claimed_by: str,
        sequence_steps: list[MailSequenceStep],
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
        confirm_leadership: Callable[[], Awaitable[bool]],
    ) -> ProcessOutcome:
        """The ONE canonical execution path for a due MailEnrollmentStep --
        see this module's MailSendRequest docstring (the MANDATORY PHASE C
        INVARIANT) and this method's own PREPARE-failure taxonomy (see
        _handle_prepare_failure()) for the full corrected sequence this
        implements. process_one_due_step() is a thin delegator to this
        exact method (see that method's own docstring) -- there is no
        second, independently-maintained execution algorithm anywhere in
        this codebase; every safety behavior below applies identically
        regardless of which name a caller used to reach it.

        Everything that can fail for a definitely-pre-send reason (OAuth
        refresh, gmail.send scope check, unsubscribe composition, MIME
        construction, the controlled-test gates, leadership) happens
        BEFORE the CLAIMED->SENDING transition -- see sender.prepare()'s
        own contract. The ONLY thing that happens after it is
        sender.send_prepared() -- one call.

        `confirm_leadership` is an async callback (injected by the
        caller -- normally MailExecutionWorker, bound to its own
        WorkerLeaseService, or _always_leader() via process_one_due_step())
        returning True iff this process CURRENTLY holds a valid execution
        lease. Checked once early (so a non-leader never claims a row at
        all) and RECHECKED immediately before the SENDING transition (so
        leadership lost mid-preparation is caught before any provider
        call, never after).

        Message-ID durability (hardening pass): the resolved
        `rfc_message_id` is persisted (+ `mailbox_id`) while STILL
        CLAIMED immediately after the controlled-test gate passes --
        BEFORE unsubscribe composition and sender.prepare() are ever
        attempted, not after. This is what makes "persisted Message-ID
        survives every PREPARE failure/retry/recovery path" actually
        true: a composition or prepare() failure can no longer cause a
        later retry to generate a fresh, different Message-ID for the
        same attempt."""
        if not await confirm_leadership():
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.NOT_LEADER)

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
            await self._pause_enrollment_for_mailbox(enrollment, campaign.mail_campaign_id, now)
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE)

        # Early suppression check -- an OPTIMIZATION only (avoids wasted
        # OAuth-refresh/composition work on an obviously-already-
        # suppressed recipient). NOT load-bearing -- see the fresh,
        # load-bearing recheck in the final safety cluster below.
        early_suppression = await self.suppression_store.get(normalize_email(enrollment.email_at_enrollment))
        if early_suppression is not None and early_suppression.active:
            await self.suppress_enrollment(enrollment, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.RECIPIENT_SUPPRESSED)

        # Controlled-test gate -- fail closed. See controlled_test_send_allowed()'s own docstring.
        if not controlled_test_send_allowed(mailbox.mailbox_id, enrollment.email_at_enrollment):
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED)

        # Resolve (never regenerate on retry) and PERSIST the RFC
        # Message-ID (+ mailbox_id) while STILL CLAIMED -- a real CAS,
        # independent of and well BEFORE the eventual CLAIMED->SENDING
        # transition, and BEFORE unsubscribe composition / sender.
        # prepare() are ever attempted -- see this method's own docstring
        # and MailEnrollmentStepStore.persist_prepared_fields()'s own
        # docstring.
        rfc_message_id = resolve_rfc_message_id(step, mailbox)
        prepared_step = step.model_copy(
            update={"mailbox_id": mailbox.mailbox_id, "rfc_message_id": rfc_message_id, "updated_at": now}
        )
        persisted = await self.step_store.persist_prepared_fields(step.enrollment_step_id, prepared_step)
        if not persisted:
            # The row moved out from under this attempt (e.g. a concurrent
            # stale-CLAIMED reap already reset it to QUEUED). Abort
            # cleanly -- nothing was ever sent, nothing is corrupted.
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.LOST_CLAIM_RACE)
        step = prepared_step

        try:
            composed = compose_outbound_email(snapshot_body=step.body, recipient_email=enrollment.email_at_enrollment)
        except Exception as e:
            return await self._handle_prepare_failure(step, enrollment, campaign.mail_campaign_id, e, now)

        prep_request = MailSendRequest(
            mailbox=mailbox,
            to_email=enrollment.email_at_enrollment,
            subject=step.subject,
            body=composed.body,
            html_body=composed.html_body,
            rfc_message_id=rfc_message_id,
            reply_in_thread=step.reply_in_thread,
            list_unsubscribe_header=composed.list_unsubscribe_header,
            list_unsubscribe_post_header=composed.list_unsubscribe_post_header,
        )
        try:
            prepared = await sender.prepare(prep_request)
        except Exception as e:
            # Every prepare() failure is provably pre-send (see
            # MailSenderPort.prepare()'s own contract) -- classified and
            # routed by _handle_prepare_failure(); never SENDING, never
            # UNKNOWN, regardless of which branch it takes.
            return await self._handle_prepare_failure(step, enrollment, campaign.mail_campaign_id, e, now)

        # --- FINAL safety cluster: everything freshly re-checked, immediately before SENDING ---
        fresh_campaign = await self.campaign_store.get(campaign.mail_campaign_id)
        if fresh_campaign is None or fresh_campaign.status != MailCampaignStatus.ACTIVE:
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.CAMPAIGN_NOT_ACTIVE)

        # Fresh mailbox recheck (hardening pass) -- the `mailbox` object
        # captured above was fetched BEFORE sender.prepare() ran; it is
        # never trusted as proof of CURRENT validity. See
        # _fresh_mailbox_still_valid_for_sending()'s own docstring for
        # exactly what's re-verified.
        revalidated_mailbox = await self._fresh_mailbox_still_valid_for_sending(
            mail_campaign_id=campaign.mail_campaign_id,
            enrollment_id=enrollment.enrollment_id,
            expected_mailbox_id=mailbox.mailbox_id,
        )
        if revalidated_mailbox is None:
            await self._pause_enrollment_for_mailbox(enrollment, campaign.mail_campaign_id, now)
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.ASSIGNED_MAILBOX_UNAVAILABLE)
        mailbox = revalidated_mailbox

        # Fresh controlled-test-gate recheck -- the SAME gate checked
        # early, re-verified here in case allowlist configuration changed
        # mid-preparation. Released to QUEUED (not paused) on failure --
        # this is a policy decision, not a mailbox-health problem.
        if not controlled_test_send_allowed(mailbox.mailbox_id, enrollment.email_at_enrollment):
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.CONTROLLED_TEST_NOT_ALLOWED)

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

        # Stage 5B (2026-09-04): daily_lead_start_limit is IMMEDIATE-mode-only
        # legacy behavior -- a campaign that has opted into
        # lead_start_mode == "triggered" gets its lead-start pacing from a
        # real MailLeadStartTrigger occurrence instead (Stage 5D), and must
        # never ALSO be throttled by this older, coarser, whole-day cap --
        # that would be exactly the "two competing controls the user can't
        # reason about" outcome the approved Trigger design explicitly
        # rejected (see the migration section of that report). A stale
        # non-null daily_lead_start_limit value left over from before a
        # campaign switched modes is deliberately inert once
        # lead_start_mode is "triggered", never zeroed out -- see
        # MailCampaign.lead_start_mode's own docstring.
        if (
            step.step_number == 1
            and fresh_campaign.lead_start_mode == "immediate"
            and fresh_campaign.daily_lead_start_limit is not None
        ):
            local_day_start = _campaign_local_day_start(now, timezone_name)
            started_today = await self.step_store.count_sent_step_for_campaign_since(
                fresh_campaign.mail_campaign_id, 1, local_day_start
            )
            if started_today >= fresh_campaign.daily_lead_start_limit:
                next_local_day = local_day_start + timedelta(days=1)
                retry_at = resolve_next_send_time(windows, timezone_name, next_local_day)
                await self._release_to_queued(step, retry_at, now)
                return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.LEAD_START_LIMIT_REACHED)

        if not is_within_window(windows, timezone_name, now):
            retry_at = resolve_next_send_time(windows, timezone_name, now)
            await self._release_to_queued(step, retry_at, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.OUTSIDE_SEND_WINDOW)

        # Load-bearing suppression check -- the LAST content-relevant check before SENDING.
        suppression = await self.suppression_store.get(normalize_email(enrollment.email_at_enrollment))
        if suppression is not None and suppression.active:
            await self.suppress_enrollment(enrollment, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.RECIPIENT_SUPPRESSED)

        if not await confirm_leadership():
            await self._release_to_queued(step, step.next_send_at or now, now)
            return ProcessOutcome(sent=False, blocked_reason=SendBlockReason.NOT_LEADER)

        # --- THE PROVIDER-UNCERTAINTY BOUNDARY ---
        sending_step = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.SENDING,
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

        try:
            result = await sender.send_prepared(prepared)
        except Exception as e:
            certainty = getattr(e, "certainty", SendOutcomeCertainty.OUTCOME_UNKNOWN)
            if certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT:
                return await self._handle_definitely_not_sent(sending_step, e, now)
            # OUTCOME_UNKNOWN (or any unclassified exception): leave in
            # SENDING, never guess. reap_orphans() -> UNKNOWN later.
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
        return ProcessOutcome(sent=True)

    # --- Phase C: paused-enrollment recovery ------------------------------------

    async def resume_mailbox_paused_enrollments(self, now: datetime) -> int:
        """Phase C recovery sweep. For every ACTIVE campaign, finds every
        enrollment PAUSED with paused_reason == MAILBOX_UNAVAILABLE whose
        STICKY assigned_mailbox_id (NEVER changed by this method, or any
        method) is now CONNECTED again and still selected in that
        campaign's channels, and resumes it to ACTIVE. NEVER touches
        assigned_mailbox_id -- the same sender the enrollment was always
        assigned to is the one it resumes with. NEVER resumes a PAUSED
        enrollment for any OTHER (including future, not-yet-invented)
        pause reason -- see MailEnrollmentPauseReason's own docstring for
        why that distinction is structural, not incidental. Returns the
        number of enrollments resumed. Idempotent -- safe to call
        repeatedly/periodically; an enrollment that's already ACTIVE, or
        still genuinely unavailable, is simply skipped."""
        campaigns = await self.campaign_store.list()
        resumed = 0
        for campaign in campaigns:
            if campaign.status != MailCampaignStatus.ACTIVE:
                continue
            enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
            for enrollment in enrollments:
                if enrollment.status != MailEnrollmentStatus.PAUSED:
                    continue
                if enrollment.paused_reason != MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE:
                    continue
                if enrollment.assigned_mailbox_id is None:
                    continue
                mailbox = await self._selected_and_connected(campaign.mail_campaign_id, enrollment.assigned_mailbox_id)
                if mailbox is None:
                    continue
                resumed_enrollment = enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE, "paused_reason": None})
                await self.enrollment_store.save(resumed_enrollment)
                await self.activity_log.record(
                    event_type="mail_enrollment.resumed",
                    category=ActivityCategory.MAIL,
                    source=ActivitySource.MAIL_SYSTEM,
                    summary="A lead's mail sequence resumed -- its assigned mailbox is connected again.",
                    entity_type="mail_campaign",
                    entity_id=campaign.mail_campaign_id,
                )
                resumed += 1
        return resumed

    # --- Phase C: UNKNOWN reconciliation (backend capability only) --------------

    async def resolve_unknown_step_confirmed_sent(
        self,
        enrollment_step_id: str,
        *,
        provider_message_id: str,
        provider_thread_id: str,
        sequence_steps: list[MailSequenceStep],
        windows: list[MailSendWindow],
        timezone_name: str,
        now: datetime,
    ) -> bool:
        """A human has independently verified (via Gmail directly,
        searching by this step's persisted rfc_message_id) that the
        message WAS actually delivered. Transitions UNKNOWN->SENT,
        backfilling the provider identifiers the human supplies, and
        reuses record_send_success()'s EXACT progression/completion logic
        (expected_status=UNKNOWN) rather than duplicating it. Raises
        UnknownStepNotFoundError / UnknownStepWrongStatusError -- see
        those classes' own docstrings. No inference, no automatic
        resend -- this only ever runs on an explicit human assertion."""
        step = await self.step_store.get(enrollment_step_id)
        if step is None:
            raise UnknownStepNotFoundError(enrollment_step_id)
        if step.status != MailEnrollmentStepStatus.UNKNOWN:
            raise UnknownStepWrongStatusError(enrollment_step_id, step.status)

        enrollment = await self.enrollment_store.get(step.enrollment_id)
        if enrollment is None:
            raise UnknownStepNotFoundError(enrollment_step_id)

        send_result = SendResult(
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            rfc_message_id=step.rfc_message_id or "",
        )
        applied = await self.record_send_success(
            step=step,
            send_result=send_result,
            sequence_steps=sequence_steps,
            enrollment=enrollment,
            windows=windows,
            timezone_name=timezone_name,
            now=now,
            expected_status=MailEnrollmentStepStatus.UNKNOWN,
        )
        if applied:
            await self.activity_log.record(
                event_type="mail_enrollment_step.manually_resolved_sent",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary="An UNKNOWN send outcome was manually confirmed as delivered.",
                entity_type="mail_campaign",
                entity_id=step.mail_campaign_id,
            )
        return applied

    async def resolve_unknown_step_confirmed_not_sent(self, enrollment_step_id: str, *, now: datetime) -> bool:
        """A human has independently verified that the message was NOT
        delivered. Transitions UNKNOWN->QUEUED, preserving the exact
        persisted `rfc_message_id` -- NEVER regenerated (see
        resolve_rfc_message_id()'s own docstring) -- so the next normal
        pickup retries using the identical Message-ID. No inference, no
        automatic resend -- only ever runs on an explicit human
        assertion."""
        step = await self.step_store.get(enrollment_step_id)
        if step is None:
            raise UnknownStepNotFoundError(enrollment_step_id)
        if step.status != MailEnrollmentStepStatus.UNKNOWN:
            raise UnknownStepWrongStatusError(enrollment_step_id, step.status)

        released = step.model_copy(
            update={
                "status": MailEnrollmentStepStatus.QUEUED,
                "claimed_by": None,
                "claimed_at": None,
                "next_send_at": now,
                "updated_at": now,
            }
        )
        applied = await self.step_store.try_transition(enrollment_step_id, MailEnrollmentStepStatus.UNKNOWN, released)
        if applied:
            await self.activity_log.record(
                event_type="mail_enrollment_step.manually_resolved_not_sent",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary="An UNKNOWN send outcome was manually confirmed as not delivered -- requeued for retry.",
                entity_type="mail_campaign",
                entity_id=step.mail_campaign_id,
            )
        return applied

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
