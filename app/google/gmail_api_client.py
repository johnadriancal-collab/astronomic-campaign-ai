"""
Thin HTTP client for Gmail's `users.messages.send` endpoint -- Astronomic
Mail Phase B2 (Gmail Sender Foundation). Same philosophy as
app/luma/client.py and app/google/oauth_client.py: a small, direct httpx
call rather than google-api-python-client (see this repo's B2 completion
report for the explicit dependency-choice writeup) -- one endpoint, one
job, fully auditable.

THE LITERAL GMAIL SEND-ENDPOINT URL BELONGS ONLY IN THIS FILE. See
tests/test_gmail_sending_safety.py's
test_gmail_send_endpoint_url_appears_only_in_the_gmail_api_client_module --
a real, capable Gmail sender now legitimately exists in this codebase for
the first time (app/google/gmail_sender.py), but nothing outside this one
module may hold the literal request shape that reaches it.

This module makes ZERO decision about mailbox/campaign state, OAuth
token refresh, or MIME construction -- it accepts a bearer access token and
an already-built base64url `raw` MIME string, and does exactly one thing:
POST it, then classify the response. See app/google/gmail_sender.py for the
caller that assembles those inputs.

Never exercised against the real Gmail API in any test -- see
tests/test_gmail_api_client.py, which redirects httpx through
httpx.MockTransport (the same pattern tests/test_luma_client.py already
established for app/luma/client.py). Never logs `access_token` or the raw
MIME payload -- only HTTP status codes and Gmail's own (non-secret) error
`status`/`reason` strings on failure.

B2 HARDENING PASS -- OUTCOME-CERTAINTY CONTRACT: every exception below
sets `.certainty` (SendOutcomeCertainty, app/services/mail_sending_service.py)
answering "could Gmail possibly have accepted the message before this
error occurred?" This is informational only in B2 -- GmailApiClient.
send_message() itself does not retry, and MailSendingService.
process_one_due_step() still treats every raised exception uniformly (see
that method's own docstring); the point is that Phase C can later inspect
`.certainty` without this module changing. The classification, worked out
against Gmail's and httpx's actual documented semantics rather than
assumed:

  DEFINITELY_NOT_SENT (an ACTUAL HTTP response confirms rejection BEFORE
  Gmail could have created the message, or the network failure is proven
  to have happened before any request bytes could have left this
  process):
    - 401 (unauthenticated), 403 (permission/scope), 429 (rate limited),
      and every other non-5xx rejection (400/other 4xx) -- each is a real
      HTTP response, meaning Gmail's server definitely received and
      definitely rejected the request outright, before ever attempting to
      create/queue a message.
    - httpx.ConnectError / httpx.ConnectTimeout / httpx.PoolTimeout -- the
      TCP connection was never established (ConnectError/ConnectTimeout)
      or never even attempted because no connection could be checked out
      of the pool in time (PoolTimeout) -- in every case, no HTTP request
      bytes could possibly have reached Gmail.

  OUTCOME_UNKNOWN (Gmail's own processing may have already happened, or
  we cannot prove otherwise):
    - 5xx -- deliberately NOT treated as DEFINITELY_NOT_SENT: a 5xx means
      Gmail's OWN SERVER errored, which does not rule out partial
      server-side processing (e.g. the message was created and the error
      happened while building the response). Conservative by design.
    - httpx.ReadTimeout / httpx.ReadError / httpx.WriteTimeout /
      httpx.WriteError / httpx.ProtocolError / any other httpx.HTTPError
      not explicitly classified above -- once the client may have started
      or finished WRITING the request, httpx gives this module no
      reliable way to prove Gmail never received it (confirmed by
      inspecting httpx's actual exception hierarchy -- ConnectTimeout
      does NOT subclass ConnectError, so both are matched explicitly
      below; nothing here is guessed from exception naming alone).
    - A 200 response whose body is unparseable or missing `id`/`threadId`
      -- 200 is Gmail's own documented success signal; failing to PARSE
      that response is a reason to distrust our own bookkeeping, not
      Gmail's. The message may well have been created.
"""

import httpx
from loguru import logger

from app.services.mail_sending_service import MailSendError, SendOutcomeCertainty

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Transport-level failures proven to occur before any request bytes could
# have reached Gmail -- see this module's docstring for why each belongs
# here specifically (verified against httpx's actual exception hierarchy,
# not assumed: ConnectTimeout is NOT a subclass of ConnectError, so both
# must be listed explicitly).
_CONNECTION_NEVER_ESTABLISHED_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class GmailSendError(MailSendError):
    """Base class for every failure this client can raise. Astronomic
    Mail's execution engine (MailSendingService.process_one_due_step(),
    app/services/mail_sending_service.py) already treats ANY exception
    raised from MailSenderPort.send() identically -- "provider call
    outcome unknown, leave the row in SENDING for reap_orphans()" -- see
    that method's own docstring. The `.certainty` this taxonomy sets
    (inherited from MailSendError, defaulting conservatively to
    OUTCOME_UNKNOWN unless a subclass below overrides it) exists for
    future observability/retry-policy decisions (Phase C+), NOT to
    change that uniform, conservative behavior; B2 does not wire any
    certainty-specific handling into the execution engine, deliberately."""


class GmailAuthRequiredError(GmailSendError):
    """The send call itself returned 401 -- distinct from
    GoogleRefreshTokenInvalidError (app/google/oauth_client.py), which is
    raised by the SEPARATE token-refresh call this client never makes
    (see app/google/gmail_sender.py, which always refreshes via
    MailboxService.refresh_mailbox_access_token() immediately before
    calling this client). A 401 here means an access token that was valid
    moments ago was rejected by the send call itself -- a provider-side
    timing/propagation hiccup, not confirmation the stored refresh token
    (the thing NEEDS_REAUTH is about) is bad. Must NEVER be treated as a
    reason to move a mailbox to NEEDS_REAUTH -- only
    GoogleRefreshTokenInvalidError may do that (see MailboxService.
    refresh_mailbox_access_token()'s docstring, and the B1 architecture
    decision this preserves). DEFINITELY_NOT_SENT: a real 401 response
    means Gmail rejected the request before ever processing it.
    RETRYABLE (Phase C): a 401 immediately after a fresh refresh is
    anomalous but still provably not-sent -- safe for the general
    retryable-DEFINITELY_NOT_SENT mechanism to requeue rather than a
    special in-attempt retry (see the Phase C design report's explicit
    reasoning for why a second, special-cased retry path was rejected in
    favor of reusing the one general mechanism)."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class GmailPermissionError(GmailSendError):
    """403 -- Gmail's documented signal for a permission/scope problem
    (e.g. the stored grant does not actually carry gmail.send). Per
    Gmail API's own error semantics this is never a transient condition,
    unlike 429/5xx below. DEFINITELY_NOT_SENT: a real 403 response means
    Gmail rejected the request before ever processing it. NOT retryable
    (default) -- a missing scope/permission does not fix itself on
    retry; this goes straight to FAILED."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT


class GmailRateLimitedError(GmailSendError):
    """429 -- transient. DEFINITELY_NOT_SENT: rate-limit rejection
    happens before Gmail processes the request body at all. RETRYABLE:
    the canonical case a retry-with-backoff policy exists for."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class GmailTemporaryProviderError(GmailSendError):
    """5xx -- a Gmail-side failure. OUTCOME_UNKNOWN, deliberately NOT
    DEFINITELY_NOT_SENT: Gmail's own server erroring does not prove the
    message wasn't created before the error happened -- see this
    module's docstring."""


class GmailPermanentRejectionError(GmailSendError):
    """A definitive, non-auth, non-rate-limit rejection (400, or any
    other 4xx not otherwise classified above) -- Gmail rejected the
    request shape itself (malformed raw MIME, invalid recipient, etc.).
    Retrying the identical request would fail identically.
    DEFINITELY_NOT_SENT: a real 4xx response means Gmail rejected the
    request before ever creating a message from it. NOT retryable
    (default) -- an identical request would be rejected identically."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT


class GmailConnectionNeverEstablishedError(GmailSendError):
    """The TCP connection to Gmail was never established (connection
    refused/DNS failure/timed out connecting) or never even attempted
    (no connection available from the pool in time) -- see this module's
    docstring for exactly which httpx exceptions map here and why.
    DEFINITELY_NOT_SENT: no HTTP request bytes could possibly have
    reached Gmail. RETRYABLE: a transient network condition."""

    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class GmailRequestOutcomeUnknownError(GmailSendError):
    """The request may have been partially or fully transmitted before
    the failure -- a read timeout/error, write timeout/error, protocol
    error, or any other transport failure not proven to have occurred
    before transmission began. OUTCOME_UNKNOWN: httpx gives this module
    no reliable way to prove Gmail never received the request -- see
    this module's docstring."""


class GmailMalformedResponseError(GmailSendError):
    """Gmail returned HTTP 200 but the body wasn't valid JSON, or was
    missing `id`/`threadId` -- a protocol-level problem, distinct from
    every status-code-driven case above. OUTCOME_UNKNOWN: 200 is Gmail's
    own success signal -- the message may well have been created even
    though this module failed to parse the confirmation."""


class GmailApiClient:
    """Real network calls to Gmail. Tests must substitute an
    httpx.MockTransport (see tests/test_gmail_api_client.py) or a fake
    implementing this same interface (see tests/test_gmail_sender.py's
    FakeGmailApiClient) -- never exercise this against the real Gmail API
    in a test."""

    async def send_message(self, *, access_token: str, raw_message: str, thread_id: str | None = None) -> dict:
        """POSTs to users.messages.send. `raw_message` must already be a
        base64url-encoded full MIME message (see app/google/gmail_mime.py's
        encode_gmail_raw()) -- this method does no MIME construction of its
        own. `thread_id`, when given, is Gmail's own thread identifier from
        a PRIOR message in the same conversation (see
        app/google/gmail_sender.py for how a follow-up would supply this) --
        omitted entirely (not sent as null) for a first message, since
        Gmail's own documented behavior is that a message with no
        `threadId` starts a new thread.

        Returns the parsed JSON body on success (guaranteed to have both
        `id` and `threadId`). Raises one of the GmailSendError subclasses
        above for every other outcome -- see this module's docstring for
        the full `.certainty` classification. Never logs `access_token`
        or `raw_message`."""
        payload: dict = {"raw": raw_message}
        if thread_id is not None:
            payload["threadId"] = thread_id

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    GMAIL_SEND_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=payload,
                )
        except _CONNECTION_NEVER_ESTABLISHED_ERRORS as e:
            logger.warning(f"Gmail send request never reached a connection ({type(e).__name__}) -- definitely not sent.")
            raise GmailConnectionNeverEstablishedError(
                f"Could not connect to Gmail: {type(e).__name__}. No request was transmitted."
            ) from e
        except httpx.HTTPError as e:
            logger.warning(f"Gmail send request failed after connecting ({type(e).__name__}) -- outcome unknown.")
            raise GmailRequestOutcomeUnknownError(
                f"Gmail send request failed: {type(e).__name__}. Outcome unknown."
            ) from e

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as e:
                raise GmailMalformedResponseError("Gmail's send response was not valid JSON.") from e
            if not data.get("id") or not data.get("threadId"):
                raise GmailMalformedResponseError("Gmail's send response had no id/threadId.")
            return data

        logger.warning(f"Gmail send request failed with status {resp.status_code}.")
        if resp.status_code == 401:
            raise GmailAuthRequiredError("Gmail rejected the send request as unauthenticated.")
        if resp.status_code == 403:
            raise GmailPermissionError("Gmail rejected the send request as forbidden (permission/scope).")
        if resp.status_code == 429:
            raise GmailRateLimitedError("Gmail rate-limited the send request.")
        if resp.status_code >= 500:
            raise GmailTemporaryProviderError(f"Gmail returned a server error (status {resp.status_code}).")
        raise GmailPermanentRejectionError(f"Gmail rejected the send request (status {resp.status_code}).")
