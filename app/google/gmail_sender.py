"""
GmailSender -- Astronomic Mail Phase B2 (Gmail Sender Foundation), split
into prepare()/send_prepared() by the Phase C provider-boundary
correction. The FIRST concrete MailSenderPort implementation anywhere in
this codebase (see app/services/mail_sending_service.py's MailSenderPort
docstring). Still NOT wired to anything reachable: app/main.py never
constructs a GmailSender, no route imports it, and Phase C's worker
(app/services/mail_execution_worker.py) is itself gated by
mail_sending_engine_enabled/the controlled-test allowlists/the worker
lease before it would ever call this class for real -- see
tests/test_gmail_sending_safety.py, which is what actually enforces the
"dormant implementation, not a reachable send path" guarantee.

PHASE C PROVIDER-BOUNDARY SPLIT: `send()` (the single MailSenderPort
method) used to do everything -- OAuth refresh, MIME construction, AND
the HTTP send call -- in one method. That meant an OAuth refresh failure
looked, to the execution engine, exactly like a genuine post-SENDING
provider-call failure, which is wrong: `invalid_grant` on a token refresh
PROVES Gmail's send endpoint was never invoked. Splitting into two
methods lets the execution engine call `prepare()` BEFORE committing the
CLAIMED->SENDING transition (so a prepare() failure never manufactures an
UNKNOWN row) and `send_prepared()` immediately after (the one thing that
may legitimately happen post-SENDING):
  - `prepare()`: gmail.send scope check (local, no I/O) + OAuth refresh
    (the ONE network call that can still fail for "definitely not sent"
    reasons) + MIME construction + base64url encoding. Never calls
    Gmail's send endpoint.
  - `send_prepared()`: exactly one HTTP call to Gmail's send endpoint.
    Nothing else. Every exception raised here, and only here, is
    genuinely provider-uncertain.
  - `send()` is kept as a convenience wrapper (`prepare()` then
    `send_prepared()`) for MailSenderPort interface compatibility and
    for tests/callers that don't need to straddle the SENDING boundary --
    Phase C's real execution path (MailSendingService.
    prepare_and_send_step()) calls prepare()/send_prepared() SEPARATELY,
    never this combined method.

Composes the same already-independent, already-tested pieces as before:
  - MailboxService.refresh_mailbox_access_token() (Phase B1, unchanged) --
    the ONLY source of a real access token, and the ONLY place a
    GoogleRefreshTokenInvalidError (-> mailbox NEEDS_REAUTH) can be
    raised. This class never talks to GoogleOAuthClient directly and never
    duplicates any part of the refresh/NEEDS_REAUTH decision.
  - app/google/gmail_mime.py -- MIME construction, header-injection
    protection, base64url encoding. Pure functions, zero network/mailbox
    knowledge.
  - app/google/gmail_api_client.py -- the one place the literal Gmail
    send-endpoint URL and HTTP-status-to-exception mapping live.

Never persists an access token anywhere -- `PreparedGmailSend.access_token`
lives only in a local variable, passed directly from prepare() to
send_prepared() within one process_one_due_step()-equivalent call, exactly
matching refresh_mailbox_access_token()'s own "NEVER stored anywhere"
contract. This split does not change that -- it only changes WHEN the
refresh happens, never WHERE the resulting token is kept.

Gmail's ACTUAL threading behavior (whether a supplied `threadId` +
`In-Reply-To`/`References` actually produces a visually-threaded Gmail
conversation the way this module constructs it) is NOT proven by
anything in this codebase -- that remains an open item for the
disposable Gmail test mailbox, per the original B2 scope.
"""

from dataclasses import dataclass

from app.google.gmail_api_client import GmailApiClient
from app.google.gmail_mime import build_mime_message, encode_gmail_raw
from app.google.oauth_client import GMAIL_SEND_SCOPE
from app.services.mail_sending_service import MailSendError, MailSendRequest, MailSenderPort, SendOutcomeCertainty, SendResult
from app.services.mailbox_service import MailboxService


class GmailScopeMissingError(MailSendError):
    """Raised by prepare() when the mailbox's currently-granted scopes
    don't include gmail.send -- a purely local check (Mailbox.
    granted_scopes is already-known data, no network call) performed
    BEFORE the OAuth refresh, so a mailbox that was never upgraded fails
    fast with a clear, specific error rather than an opaque 403 from
    Gmail's API later. DEFINITELY_NOT_SENT: no network call to Gmail's
    send endpoint was ever attempted -- this is the most certain of all
    the definitely-not-sent cases, since it's not even a real HTTP
    response, just a local fact about the mailbox's own record."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT


@dataclass(frozen=True)
class PreparedGmailSend:
    """Everything send_prepared() needs to make exactly one HTTP call --
    nothing more. `access_token` is transient, in-memory only (see this
    module's docstring) -- never persisted by anything that touches this
    object."""

    access_token: str
    raw_message: str
    thread_id: str | None
    rfc_message_id: str


class GmailSender(MailSenderPort):
    def __init__(self, mailbox_service: MailboxService, gmail_api_client: GmailApiClient | None = None):
        self.mailbox_service = mailbox_service
        self.gmail_api_client = gmail_api_client or GmailApiClient()

    async def prepare(self, request: MailSendRequest) -> PreparedGmailSend:
        """Everything that can fail for a PRE-SEND reason. Raises
        GmailScopeMissingError, or whatever refresh_mailbox_access_token()
        itself can raise (GoogleRefreshTokenInvalidError -- already flips
        the mailbox to NEEDS_REAUTH internally, unchanged since B1 --
        GoogleTokenRefreshError, GoogleTokenRefreshMalformedResponseError,
        MailboxCredentialMissingError, MailboxNotFound), or
        HeaderInjectionError from MIME construction. NEVER calls Gmail's
        send endpoint -- see this module's docstring."""
        if GMAIL_SEND_SCOPE not in request.mailbox.granted_scopes:
            raise GmailScopeMissingError(
                f"Mailbox {request.mailbox.mailbox_id} has not granted {GMAIL_SEND_SCOPE}."
            )

        access_token = await self.mailbox_service.refresh_mailbox_access_token(request.mailbox.mailbox_id)

        mime_bytes = build_mime_message(
            from_email=request.mailbox.email,
            to_email=request.to_email,
            subject=request.subject,
            body=request.body,
            rfc_message_id=request.rfc_message_id,
            in_reply_to_message_id=request.in_reply_to_message_id,
            references=list(request.references) or None,
            list_unsubscribe=request.list_unsubscribe_header,
            list_unsubscribe_post=request.list_unsubscribe_post_header,
            html_body=request.html_body,
        )
        raw = encode_gmail_raw(mime_bytes)

        return PreparedGmailSend(
            access_token=access_token,
            raw_message=raw,
            thread_id=request.thread_id,
            rfc_message_id=request.rfc_message_id,
        )

    async def send_prepared(self, prepared: PreparedGmailSend) -> SendResult:
        """The ONLY thing that may happen after the CLAIMED->SENDING
        transition: exactly one HTTP call to Gmail's send endpoint. Every
        exception raised here is genuinely provider-uncertain -- see
        GmailApiClient's exception taxonomy (including each exception's
        `.certainty`) for exactly what can propagate."""
        data = await self.gmail_api_client.send_message(
            access_token=prepared.access_token, raw_message=prepared.raw_message, thread_id=prepared.thread_id
        )
        return SendResult(
            provider_message_id=data["id"],
            provider_thread_id=data["threadId"],
            rfc_message_id=prepared.rfc_message_id,
        )

    async def send(self, request: MailSendRequest) -> SendResult:
        """Convenience wrapper (prepare() then send_prepared()) kept for
        MailSenderPort interface compatibility and for callers/tests that
        don't need to straddle the SENDING boundary. Phase C's real
        execution path (MailSendingService.prepare_and_send_step()) calls
        prepare()/send_prepared() SEPARATELY -- see this module's own
        docstring for why that split matters and this combined method
        does not preserve the provider-boundary guarantee on its own."""
        return await self.send_prepared(await self.prepare(request))
