"""
MailBounceDetectionService -- bounce detection (2026-09-18). Automatic
delivery-state infrastructure for every Astronomic Mail campaign, NO
campaign-level opt-in (contrast MailOpenTrackingService/
open_tracking_enabled) -- it only ever reads a connected mailbox's OWN
inbound mail, never anything requiring a per-campaign choice.

DISCOVERY: Gmail's `users.history.list` (GmailHistoryClient), NOT a
mailbox-wide scan and NOT per-thread polling (contrast
MailReplyDetectionService, which re-checks KNOWN threads -- a DSN has no
known prior thread to re-check, since it's a genuinely NEW inbound
message). One `MailboxHistoryCheckpoint` per mailbox tracks how far this
service has already looked.

BOOTSTRAP: a mailbox with no checkpoint yet gets ONE bootstrap cycle
that only calls `GmailHistoryClient.get_current_history_id()` (via
`users.getProfile`) and saves that as the starting checkpoint -- it
processes ZERO history on this cycle. This is deliberate: it establishes
"start counting from right now" without ever scanning backward into a
mailbox's pre-existing mail (see this module's own "historical coverage"
docstring point below).

PER-CYCLE FLOW (poll_one_mailbox): refresh the mailbox's access token;
call `list_history()` from the saved checkpoint; for each new message id
returned, run the CHEAP `get_message_metadata_light()` pre-filter
(`is_likely_dsn()` on From/Subject/Content-Type alone); only for a
message that passes that filter, fetch the FULL MIME body
(`GmailMessageBodyClient.get_message_full()`) and run the real
`parse_dsn()` structural check; if a DSN is confirmed, attempt
attribution (see below); advance the checkpoint to the NEW historyId
ONLY after the entire batch is processed without a Gmail READ error
(a message that simply isn't a DSN, or fails attribution, is a normal,
successful outcome for checkpoint purposes -- only a Gmail-side read
failure aborts the WHOLE batch for this mailbox this cycle, leaving the
checkpoint untouched so the SAME batch is retried next cycle. This is
what "API error does not advance checkpoint" means in practice, and why
partial progress within one cycle is never partially committed).

STALE/EXPIRED HISTORY: `GmailHistoryExpiredError` (Gmail's documented
404 for a `startHistoryId` outside its retained window, typically ~1
week) is NEVER silently swallowed as "nothing happened" -- this service
logs a clear WARNING naming the mailbox and the fact that a coverage gap
now exists between the old checkpoint and now, then re-bootstraps
(exactly the same one-cycle, zero-history-processed bootstrap as a
brand-new mailbox) so the NEXT cycle resumes forward safely. The missed
window itself is NOT recoverable (Gmail has already discarded that
history) -- this is reported, never pretended away.

ATTRIBUTION (the one place a bounce is actually decided real):
`ParsedDsn.original_message_id` (stripped of angle brackets) is looked
up via `MailEnrollmentStepStore.get_by_rfc_message_id()`. A miss means
this DSN is either unrelated to Astronomic Mail (a personal email in the
same inbox) or its original-message reference is missing/unattributable
-- EITHER WAY, it is never persisted, never guessed at via
recipient+timestamp or any other fallback (the approved design's
"do NOT mark a bounce if attribution is uncertain," taken literally).

RESILIENCE: `poll_all_mailboxes()` wraps each mailbox in its own
try/except (matching MailReplyDetectionService's own per-candidate
isolation) -- one mailbox's Gmail-auth/network problem never blocks
another mailbox's bounce detection, and (via MailExecutionWorker's own
tick()-level try/except around this whole call) never blocks outbound
sending either.

NEVER TOUCHES: MailReply/REPLIED status (a DSN's `From` is never the
enrolled recipient's own address, so MailReplyDetectionService's
positive-match rule already excludes it structurally -- this service
adds no cross-suppression logic of its own, by design), MailSuppression
(Phase 1 is detect/persist/surface only -- see MailBounce's own
docstring), and never mutates MailEnrollmentStep/MailEnrollment status.
"""

from datetime import datetime

from loguru import logger

from app.google.gmail_history_client import GmailHistoryClient, GmailHistoryExpiredError
from app.google.gmail_message_body_client import GmailMessageBodyClient
from app.google.gmail_thread_reader_client import GmailReadError
from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.models.mail import MailBounce, MailboxHistoryCheckpoint
from app.models.mailbox import MailboxStatus
from app.repositories.mail_bounce_store import MailBounceStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mailbox_history_checkpoint_store import MailboxHistoryCheckpointStore
from app.repositories.mailbox_store import MailboxStore
from app.services.mail_bounce_dsn_parser import classify_bounce_type, is_likely_dsn, parse_dsn
from app.services.mailbox_service import MailboxCredentialMissingError, MailboxNotFound, MailboxService


def _extract_light_headers(light_message: dict) -> dict[str, str | None]:
    """Pulls a flat {header_name: value} dict out of a
    GmailHistoryClient.get_message_metadata_light() response.
    Deliberately its OWN extraction, not gmail_thread_reader_client.
    extract_headers() -- that helper filters to REPLY DETECTION's fixed
    METADATA_HEADERS allowlist (From/To/Subject/Message-ID/In-Reply-To/
    References), which does not include Content-Type -- the ONE header
    is_likely_dsn() actually needs. Gmail's own `metadataHeaders` query
    param already restricts what the server returns, so no client-side
    filtering is needed (or correct) here at all."""
    headers = ((light_message.get("payload") or {}).get("headers")) or []
    return {h["name"]: h.get("value") for h in headers if h.get("name")}


class MailBounceDetectionService:
    def __init__(
        self,
        *,
        mailbox_store: MailboxStore,
        mailbox_service: MailboxService,
        enrollment_step_store: MailEnrollmentStepStore,
        checkpoint_store: MailboxHistoryCheckpointStore,
        bounce_store: MailBounceStore,
        history_client: GmailHistoryClient | None = None,
        message_body_client: GmailMessageBodyClient | None = None,
    ):
        self.mailbox_store = mailbox_store
        self.mailbox_service = mailbox_service
        self.enrollment_step_store = enrollment_step_store
        self.checkpoint_store = checkpoint_store
        self.bounce_store = bounce_store
        self.history_client = history_client or GmailHistoryClient()
        self.message_body_client = message_body_client or GmailMessageBodyClient()

    async def poll_all_mailboxes(self, now: datetime) -> int:
        """One poll cycle across every CONNECTED mailbox. Returns the
        total number of NEW bounce rows recorded this cycle. One
        mailbox's failure never aborts another's -- see this module's
        own module docstring."""
        mailboxes = await self.mailbox_store.list()
        total = 0
        for mailbox in mailboxes:
            if mailbox.status != MailboxStatus.CONNECTED:
                continue
            try:
                total += await self.poll_one_mailbox(mailbox.mailbox_id, now)
            except Exception:
                logger.exception(f"Bounce-poll: mailbox {mailbox.mailbox_id} failed; will retry next cycle.")
        return total

    async def poll_one_mailbox(self, mailbox_id: str, now: datetime) -> int:
        checkpoint = await self.checkpoint_store.get(mailbox_id)

        try:
            access_token = await self.mailbox_service.refresh_mailbox_access_token(mailbox_id)
        except (GoogleRefreshTokenInvalidError, GoogleTokenRefreshError, MailboxCredentialMissingError, MailboxNotFound):
            logger.warning(f"Bounce-poll: could not refresh access token for mailbox {mailbox_id}; skipping this cycle.")
            return 0

        if checkpoint is None:
            # Bootstrap ONLY -- establish "start counting from right
            # now," process zero history this cycle. See this module's
            # own docstring for why this never scans backward.
            try:
                current_history_id = await self.history_client.get_current_history_id(access_token=access_token)
            except GmailReadError:
                logger.warning(f"Bounce-poll: could not bootstrap history checkpoint for mailbox {mailbox_id}; will retry next cycle.")
                return 0
            await self.checkpoint_store.save(
                MailboxHistoryCheckpoint(mailbox_id=mailbox_id, history_id=current_history_id, updated_at=now)
            )
            logger.info(f"Bounce-poll: bootstrapped history checkpoint for mailbox {mailbox_id} at historyId={current_history_id}.")
            return 0

        try:
            result = await self.history_client.list_history(access_token=access_token, start_history_id=checkpoint.history_id)
        except GmailHistoryExpiredError:
            # Gmail's own retained-history window (typically ~1 week)
            # no longer covers this checkpoint -- NOT silently reset.
            # Re-bootstrap forward from the mailbox's CURRENT position;
            # the missed window itself is genuinely unrecoverable (Gmail
            # already discarded that history), reported here, never
            # pretended away.
            logger.warning(
                f"Bounce-poll: historyId for mailbox {mailbox_id} (checkpoint from {checkpoint.updated_at.isoformat()}) "
                "is expired/out of range. Re-bootstrapping to the mailbox's current position -- any bounce that "
                "arrived during the gap between the old checkpoint and now cannot be recovered."
            )
            try:
                current_history_id = await self.history_client.get_current_history_id(access_token=access_token)
            except GmailReadError:
                logger.warning(f"Bounce-poll: could not re-bootstrap mailbox {mailbox_id} after expired history; will retry next cycle.")
                return 0
            await self.checkpoint_store.save(
                MailboxHistoryCheckpoint(mailbox_id=mailbox_id, history_id=current_history_id, updated_at=now)
            )
            return 0
        except GmailReadError:
            # Any other Gmail failure (auth, rate limit, connection,
            # provider error) -- checkpoint NOT advanced, whole batch
            # retried next cycle.
            logger.warning(f"Bounce-poll: Gmail history.list failed for mailbox {mailbox_id}; checkpoint not advanced, will retry.")
            return 0

        new_bounces = 0
        try:
            for message_id in result["message_ids"]:
                if await self._process_candidate_message(mailbox_id, access_token, message_id, now):
                    new_bounces += 1
        except GmailReadError:
            # A Gmail read failure partway through THIS batch -- abort
            # the whole batch for this mailbox this cycle without
            # advancing the checkpoint, so the SAME batch (including
            # whatever was already found) is retried next cycle.
            # Re-processing an already-recorded bounce is a safe no-op
            # (create() is idempotent on gmail_message_id).
            logger.warning(f"Bounce-poll: a Gmail read failed mid-batch for mailbox {mailbox_id}; checkpoint not advanced, will retry.")
            return new_bounces

        await self.checkpoint_store.save(
            MailboxHistoryCheckpoint(mailbox_id=mailbox_id, history_id=result["history_id"], updated_at=now)
        )
        return new_bounces

    async def _process_candidate_message(
        self, mailbox_id: str, access_token: str, message_id: str, now: datetime
    ) -> bool:
        """Returns True iff a NEW bounce row was recorded for this
        message. Raises GmailReadError upward (uncaught here
        deliberately) so poll_one_mailbox() can abort the whole batch
        without advancing the checkpoint -- see that method's own
        docstring. A message that is fetched successfully but simply
        isn't a DSN, or fails attribution, returns False normally (a
        real, successful "nothing to do here" outcome, not an error)."""
        light = await self.history_client.get_message_metadata_light(access_token=access_token, message_id=message_id)
        headers = _extract_light_headers(light)
        if not is_likely_dsn(headers):
            return False

        full_message = await self.message_body_client.get_message_full(access_token=access_token, message_id=message_id)
        parsed = parse_dsn(full_message)
        if parsed is None:
            # The cheap Content-Type pre-filter was a false positive --
            # no real message/delivery-status part was actually found.
            return False
        if parsed.original_message_id is None:
            logger.info(f"Bounce-poll: DSN {message_id} in mailbox {mailbox_id} has no recoverable Original-Message-ID; skipping (uncertain attribution).")
            return False

        step = await self.enrollment_step_store.get_by_rfc_message_id(parsed.original_message_id)
        if step is None:
            # Unrelated to Astronomic Mail -- never counted.
            logger.info(f"Bounce-poll: DSN {message_id} in mailbox {mailbox_id} references an unrecognized Message-ID; not an Astronomic Mail send, skipping.")
            return False

        recipient = parsed.recipients[0]
        bounce = MailBounce(
            gmail_message_id=message_id,
            mailbox_id=mailbox_id,
            mail_campaign_id=step.mail_campaign_id,
            enrollment_id=step.enrollment_id,
            enrollment_step_id=step.enrollment_step_id,
            original_message_id=parsed.original_message_id,
            recipient_email=recipient.final_recipient or recipient.original_recipient or "",
            bounce_type=classify_bounce_type(recipient.status),
            action=recipient.action,
            status_code=recipient.status,
            diagnostic_code=recipient.diagnostic_code,
            bounced_at=now,
            created_at=now,
        )
        created = await self.bounce_store.create(bounce)
        if created:
            logger.info(
                f"Bounce-poll: recorded a {bounce.bounce_type.value} bounce for campaign {step.mail_campaign_id}, "
                f"enrollment {step.enrollment_id} (mailbox {mailbox_id})."
            )
        return created
