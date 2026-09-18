"""
Reply detection V1 (2026-09-15) -- MailReplyDetectionService.
poll_for_replies(), the ONE place this codebase ever decides "did this
enrollment's recipient reply." Targeted per-thread polling via
GmailThreadReaderClient.get_thread() (users.threads.get, gmail.metadata
scope) -- NOT a mailbox-wide History API sync, NOT push notifications
(see the approved V1 design: launch speed over completeness, revisit at
real volume). Candidates come from MailSendingService.
list_reply_poll_candidates() -- see that method's own docstring for the
full eligibility rule (ACTIVE/PAUSED enrollments only, campaign status
NOT required to be ACTIVE, at least one SENT step with a persisted
gmail_thread_id, no MailReply yet).

V1 reply definition (deliberately narrow, see the approved design):
an inbound message in the enrollment's known Gmail thread whose
normalized `From` address equals exactly `enrollment.email_at_enrollment`
-- nothing else decides REPLIED vs not-yet. No FULL body inspection
(the MIME payload/body genuinely is never returned under gmail.metadata
format), no sentiment classification, no autoresponder/OOO distinction
(a real, accepted V1 simplification). Excluding our own outbound
messages and any other sender (a bounce, a CC'd third party, a system
message) is the SAME positive-match rule in reverse: only a message
whose `From` equals the enrolled recipient's own address ever counts --
never a blocklist of known system/bounce senders, which could never
enumerate every address it doesn't anticipate.

Resilience: EVERY candidate is processed independently inside its own
try/except -- one candidate's mailbox needing reauth, or one Gmail read
failing, must never abort the poll for every other candidate. A skipped
candidate is simply retried on the next poll cycle; nothing here treats
a skip as terminal.

NOT reply-detection's own reply history: MailReply records THAT an
enrollment replied, once, ever (see that model's own docstring) -- this
service does not track every message in a thread, does not re-check an
enrollment once a MailReply exists (excluded from candidates entirely).
The REPLIED decision itself never uses more than the one From-address
match. The ONE additional thing this service reads from the same
already-fetched response (2026-09-18): the qualifying message's own
`snippet` field -- confirmed present even under gmail.metadata format,
a separate, top-level field from the MIME body/payload -- fed through
app/services/mail_reply_preview.py's heuristic to derive a short,
already-quote-trimmed `reply_preview` for Campaign Manager's Inbox list.
Zero new Gmail calls, zero new scope; this is not the "body inspection"
the REPLIED decision itself deliberately avoids.
"""

from datetime import datetime
from email.utils import parseaddr

from loguru import logger

from app.google.gmail_thread_reader_client import GmailReadError, GmailThreadReaderClient, extract_headers
from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.models.crm import normalize_email
from app.services.mail_reply_preview import derive_reply_preview
from app.services.mail_sending_service import MailSendingService, ReplyPollCandidate
from app.services.mailbox_service import MailboxCredentialMissingError, MailboxNotFound, MailboxService


def _from_address(headers: dict[str, str | None]) -> str | None:
    """Extracts just the email address out of a raw `From` header value
    (`"Display Name <a@b.com>"` or a bare `"a@b.com"`), using the
    stdlib's own address-parsing logic -- same "use the email package,
    don't hand-roll header parsing" convention as
    app/google/gmail_mime.py. Returns None if the header is absent or
    unparseable to a usable address."""
    raw = headers.get("From")
    if not raw:
        return None
    _display_name, address = parseaddr(raw)
    return address or None


class MailReplyDetectionService:
    def __init__(
        self,
        *,
        sending_service: MailSendingService,
        mailbox_service: MailboxService,
        gmail_thread_reader_client: GmailThreadReaderClient | None = None,
    ):
        self.sending_service = sending_service
        self.mailbox_service = mailbox_service
        self.gmail_thread_reader_client = gmail_thread_reader_client or GmailThreadReaderClient()

    async def poll_for_replies(self, now: datetime) -> int:
        """One poll cycle. Returns the number of NEW replies detected and
        recorded (a candidate whose thread turns out not to have a
        qualifying reply yet is simply not counted, not an error)."""
        candidates = await self.sending_service.list_reply_poll_candidates()
        detected = 0
        for candidate in candidates:
            try:
                if await self._check_one_candidate(candidate, now):
                    detected += 1
            except Exception:
                # ONE candidate's failure (mailbox needs reauth, a Gmail
                # read error, anything else) must never abort the poll
                # for every other candidate -- logged and retried next
                # cycle, same resilience discipline as the worker's own
                # Trigger-processing isolation (see
                # MailExecutionWorker.tick()'s own docstring).
                logger.exception(
                    f"Reply-poll: candidate enrollment {candidate.enrollment.enrollment_id} failed; will retry next cycle."
                )
        return detected

    async def _check_one_candidate(self, candidate: ReplyPollCandidate, now: datetime) -> bool:
        try:
            access_token = await self.mailbox_service.refresh_mailbox_access_token(candidate.mailbox_id)
        except (GoogleRefreshTokenInvalidError, GoogleTokenRefreshError, MailboxCredentialMissingError, MailboxNotFound):
            # The mailbox itself needs attention (reauth, missing
            # credential, deleted) -- not this candidate's fault, and not
            # something a reply poll should try to fix. Skip; the normal
            # mailbox-health recovery paths (or a human) handle this.
            logger.warning(f"Reply-poll: could not refresh access token for mailbox {candidate.mailbox_id}; skipping this cycle.")
            return False

        try:
            thread = await self.gmail_thread_reader_client.get_thread(
                access_token=access_token, thread_id=candidate.gmail_thread_id
            )
        except GmailReadError:
            logger.warning(f"Reply-poll: Gmail read failed for thread {candidate.gmail_thread_id}; skipping this cycle.")
            return False

        mailbox = await self.sending_service.mailbox_store.get(candidate.mailbox_id)
        our_email_normalized = normalize_email(mailbox.email) if mailbox is not None else None
        expected_from_normalized = normalize_email(candidate.enrollment.email_at_enrollment)

        reply_message = None
        for message in thread.get("messages", []):
            headers = extract_headers(message)
            address = _from_address(headers)
            if not address:
                continue
            address_normalized = normalize_email(address)
            if our_email_normalized is not None and address_normalized == our_email_normalized:
                continue  # our own outbound message in this thread
            if address_normalized == expected_from_normalized:
                reply_message = message
                break  # V1: the first qualifying inbound message is sufficient

        if reply_message is None:
            return False

        # 2026-09-18 -- derived from the SAME metadata-scope get_thread()
        # response already fetched above; zero new Gmail calls, zero new
        # scope. See mail_reply_preview.py's own docstring for why
        # Gmail's `snippet` field is usable here despite this module's
        # own (accurate, for the full body) claim that gmail.metadata
        # can't return body content -- `snippet` is a separate, top-
        # level Message-resource field, confirmed present under metadata
        # format on this app's real production replies.
        reply_preview = derive_reply_preview(
            reply_message.get("snippet"), mailbox.email if mailbox is not None else None
        )

        return await self.sending_service.mark_enrollment_replied(
            candidate.enrollment,
            mailbox_id=candidate.mailbox_id,
            gmail_thread_id=candidate.gmail_thread_id,
            gmail_message_id=reply_message.get("id", ""),
            reply_email_normalized=expected_from_normalized,
            now=now,
            reply_preview=reply_preview,
        )
