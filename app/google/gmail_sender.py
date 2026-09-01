"""
GmailSender -- Astronomic Mail Phase B2 (Gmail Sender Foundation). The
FIRST concrete MailSenderPort implementation anywhere in this codebase
(see app/services/mail_sending_service.py's MailSenderPort docstring: "no
concrete implementation exists anywhere under app/" -- that sentence stops
being literally true as of this file, though the "capable of actually
being reached" guarantee it exists to protect is unchanged -- see
tests/test_gmail_sending_safety.py).

NOT WIRED TO ANYTHING: app/main.py never constructs a GmailSender, no
route under app/api/ imports it, and no worker/scheduler exists to drive
process_one_due_step() with it. It is a real, fully-tested, directly
INSTANTIATABLE class -- reachable only by writing a new test or a new
call site, neither of which this phase adds. See this repo's B2
completion report for the exact "dormant implementation vs. reachable
send path" distinction this satisfies.

Composes three already-independent, already-tested pieces rather than
duplicating any of their logic:
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

Never persists an access token anywhere -- the value returned by
refresh_mailbox_access_token() lives only in this method's local stack
frame for the duration of one send() call, exactly matching that method's
own "NEVER stored anywhere" contract.

B2 HARDENING PASS: this class no longer generates its own RFC Message-ID
(app/services/rfc_message_id.py's generate_rfc_message_id() moved out of
the old gmail_mime.py and is now called only by the EXECUTION layer --
see MailSendRequest's docstring in app/services/mail_sending_service.py
for the durability rationale). GmailSender.send() CONSUMES
`request.rfc_message_id` exactly as given -- it never generates, mutates,
or substitutes it. Threading is likewise driven entirely by the request's
own fields rather than a growing set of optional send() kwargs.

Gmail's ACTUAL threading behavior (whether a supplied `threadId` +
`In-Reply-To`/`References` actually produces a visually-threaded Gmail
conversation the way this module constructs it) is NOT proven by
anything in this codebase -- that remains an open item for the
disposable Gmail test mailbox, per the original B2 scope.
"""

from app.google.gmail_api_client import GmailApiClient
from app.google.gmail_mime import build_mime_message, encode_gmail_raw
from app.services.mail_sending_service import MailSendRequest, MailSenderPort, SendResult
from app.services.mailbox_service import MailboxService


class GmailSender(MailSenderPort):
    def __init__(self, mailbox_service: MailboxService, gmail_api_client: GmailApiClient | None = None):
        self.mailbox_service = mailbox_service
        self.gmail_api_client = gmail_api_client or GmailApiClient()

    async def send(self, request: MailSendRequest) -> SendResult:
        """Implements MailSenderPort's committed signature exactly (one
        MailSendRequest in, one SendResult out) -- no extra kwargs, no
        superset. Every field this method needs -- including
        `rfc_message_id` and all threading context -- comes from
        `request`; this method invents nothing except by delegating to
        Gmail itself (`provider_message_id`/`provider_thread_id`, read
        back from Gmail's own response).

        Raises on any failure or ambiguous outcome, per MailSenderPort's
        contract -- see GmailApiClient's exception taxonomy (including
        each exception's `.certainty` -- SendOutcomeCertainty, app/
        services/mail_sending_service.py) for exactly what can propagate,
        and GmailSendError's own docstring for why this class does not
        attempt to type-switch on them itself.
        """
        access_token = await self.mailbox_service.refresh_mailbox_access_token(request.mailbox.mailbox_id)

        mime_bytes = build_mime_message(
            from_email=request.mailbox.email,
            to_email=request.to_email,
            subject=request.subject,
            body=request.body,
            rfc_message_id=request.rfc_message_id,
            in_reply_to_message_id=request.in_reply_to_message_id,
            references=list(request.references) or None,
        )
        raw = encode_gmail_raw(mime_bytes)

        data = await self.gmail_api_client.send_message(
            access_token=access_token, raw_message=raw, thread_id=request.thread_id
        )

        return SendResult(
            provider_message_id=data["id"],
            provider_thread_id=data["threadId"],
            rfc_message_id=request.rfc_message_id,
        )
