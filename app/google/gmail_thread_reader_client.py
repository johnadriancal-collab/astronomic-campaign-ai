"""
Thin, read-only HTTP client for Gmail's `users.threads.get` and
`users.messages.get` endpoints -- added 2026-09-15 specifically to settle
a real threading-verification contradiction: AstroHub's own outbound
pipeline is provably correct (matching subject, RFC-compliant Message-ID/
In-Reply-To/References, `threadId` genuinely present in the send request,
Gmail's own send response echoing a matching `threadId` back) -- yet the
Gmail UI persistently shows two separate conversations. The only way to
resolve that contradiction is to ask Gmail's server directly what it
actually persisted, which is what this module exists to do.

Same "one thin httpx client per Gmail endpoint" philosophy as
app/google/gmail_api_client.py (see that module's own docstring) --
requested `format=metadata` with an explicit `metadataHeaders` allowlist
throughout, matching the `gmail.metadata` scope this client is
authorized to use (headers/labels only -- NEVER message body or
attachments; see GMAIL_METADATA_SCOPE's own docstring in
app/google/oauth_client.py for why that scope specifically). This module
makes ZERO decision about mailbox/OAuth state (that's
MailboxService.refresh_mailbox_access_token(), the same as
GmailSender) -- it accepts a bearer access token and returns Gmail's
parsed response.

DIAGNOSTIC USE ONLY as of this writing -- nothing in this codebase calls
this module automatically; it exists to be invoked deliberately, once,
by a human-triggered read (see app/api/mailboxes.py's diagnostic route).
This is NOT reply detection: no polling, no MailReply model, no
enrollment/campaign state is ever touched by anything in this file.

Never logs `access_token` or any header VALUE (From/To/Subject/
Message-ID/etc. can contain real recipient data) -- only HTTP status
codes and Gmail's own (non-secret) error `status`/`reason` strings on
failure, matching gmail_api_client.py's own discipline.
"""

import httpx
from loguru import logger

GMAIL_THREADS_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}"
GMAIL_MESSAGES_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"

# The complete, fixed allowlist of headers ever requested -- exactly the
# ones needed to diagnose threading (Subject/Message-ID/In-Reply-To/
# References) plus From/To for participant sanity-checking. Never
# expanded to request body-adjacent data; that would need gmail.readonly,
# a scope this codebase is not authorized to request (see
# GMAIL_METADATA_SCOPE's own docstring).
METADATA_HEADERS = ("From", "To", "Subject", "Message-ID", "In-Reply-To", "References")


class GmailReadError(Exception):
    """Base class for every failure this client can raise. Never carries
    the raw access token or any header value in its message."""


class GmailReadAuthError(GmailReadError):
    """401/403 -- the access token was rejected, or the mailbox's grant
    doesn't actually include gmail.metadata. Distinct from
    GoogleRefreshTokenInvalidError (app/google/oauth_client.py), which is
    raised by the separate token-refresh call this client never makes."""


class GmailReadNotFoundError(GmailReadError):
    """404 -- no thread/message with that id exists in this mailbox (a
    typo'd id, or one genuinely belonging to a different account)."""


class GmailReadRateLimitedError(GmailReadError):
    """429 -- transient, safe to retry later. This module does not retry
    automatically."""


class GmailReadProviderError(GmailReadError):
    """5xx -- a Gmail-side failure, or any other non-2xx/401/403/404/429
    response not otherwise classified."""


class GmailReadConnectionError(GmailReadError):
    """The request never reached Gmail, or the connection failed before
    a response could be read (DNS/connect/timeout/protocol-level httpx
    failure)."""


class GmailReadMalformedResponseError(GmailReadError):
    """Gmail returned 200 but the body wasn't valid JSON, or was missing
    the fields this client depends on (`id`, and for a thread, its own
    `messages` list)."""


def _metadata_params() -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("format", "metadata")]
    params.extend(("metadataHeaders", h) for h in METADATA_HEADERS)
    return params


def _classify_and_raise(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        logger.warning(f"Gmail read request rejected with status {resp.status_code}.")
        raise GmailReadAuthError(f"Gmail rejected the read request with status {resp.status_code}.")
    if resp.status_code == 404:
        raise GmailReadNotFoundError("Gmail reports no such thread/message in this mailbox.")
    if resp.status_code == 429:
        raise GmailReadRateLimitedError("Gmail rate-limited this read request.")
    logger.warning(f"Gmail read request failed with status {resp.status_code}.")
    raise GmailReadProviderError(f"Gmail read request failed with status {resp.status_code}.")


class GmailThreadReaderClient:
    """Real network calls to Gmail. Tests must substitute an
    httpx.MockTransport (see tests/test_gmail_thread_reader_client.py) --
    never exercise this against the real Gmail API in a test."""

    async def get_thread(self, *, access_token: str, thread_id: str) -> dict:
        """GET users.threads.get?format=metadata -- returns Gmail's
        parsed response: `{"id", "historyId", "messages": [...]}`, each
        message carrying `id`, `threadId`, and a `payload.headers` list
        restricted to METADATA_HEADERS. Never the message body/snippet
        (gmail.metadata's own restriction -- Gmail simply omits `body`/
        `snippet` content under this scope, this client does not need to
        filter anything out itself)."""
        url = GMAIL_THREADS_GET_URL.format(thread_id=thread_id)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=_metadata_params())
        except httpx.HTTPError as e:
            logger.warning(f"Gmail threads.get request failed ({type(e).__name__}).")
            raise GmailReadConnectionError(f"Could not reach Gmail: {type(e).__name__}.") from e

        if resp.status_code != 200:
            _classify_and_raise(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise GmailReadMalformedResponseError("Gmail's threads.get response was not valid JSON.") from e
        if not data.get("id") or "messages" not in data:
            raise GmailReadMalformedResponseError("Gmail's threads.get response had no id/messages.")
        return data

    async def get_message(self, *, access_token: str, message_id: str) -> dict:
        """GET users.messages.get?format=metadata -- returns Gmail's
        parsed response: `{"id", "threadId", "payload": {"headers": [...]}}`,
        headers restricted to METADATA_HEADERS, same scope restriction as
        get_thread()."""
        url = GMAIL_MESSAGES_GET_URL.format(message_id=message_id)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=_metadata_params())
        except httpx.HTTPError as e:
            logger.warning(f"Gmail messages.get request failed ({type(e).__name__}).")
            raise GmailReadConnectionError(f"Could not reach Gmail: {type(e).__name__}.") from e

        if resp.status_code != 200:
            _classify_and_raise(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise GmailReadMalformedResponseError("Gmail's messages.get response was not valid JSON.") from e
        if not data.get("id") or not data.get("threadId"):
            raise GmailReadMalformedResponseError("Gmail's messages.get response had no id/threadId.")
        return data


def extract_headers(message_or_thread_message: dict) -> dict[str, str | None]:
    """Pulls a flat {header_name: value} dict out of one Gmail message's
    `payload.headers` list (the shape both get_thread()'s `messages[i]`
    and get_message() itself return) -- restricted to METADATA_HEADERS,
    matching what Gmail actually returns under gmail.metadata. Any header
    genuinely absent on the message (e.g. a first message has no
    In-Reply-To/References) is simply missing from the result, not set
    to None -- callers use .get() as usual."""
    headers = (message_or_thread_message.get("payload") or {}).get("headers") or []
    return {h["name"]: h["value"] for h in headers if h.get("name") in METADATA_HEADERS}
