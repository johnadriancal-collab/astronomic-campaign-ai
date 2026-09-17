"""
Thin, read-only HTTP client for Gmail's `users.messages.get?format=full` --
Inbox V2 (2026-09-17), reply-body reading. Same "one thin httpx client per
Gmail endpoint" philosophy as gmail_thread_reader_client.py (see that
module's own docstring) -- this one is authorized under `gmail.readonly`
specifically (see GMAIL_READONLY_SCOPE's own docstring in
app/google/oauth_client.py for why that's the narrowest scope that can
answer "what did this specific reply message actually say").

Callers pass an exact `message_id` they already know about (from a
MailReply row) -- this client has no listing/search method at all, by
construction, so it cannot be used to browse a mailbox. It fetches
EXACTLY one message, full MIME structure included.

Never logs `access_token`, any header value, or any message body/part
content -- only HTTP status codes and Gmail's own (non-secret)
error `status`/`reason` strings on failure, matching
gmail_thread_reader_client.py's own discipline.
"""

import base64
import binascii

import httpx
from loguru import logger

from app.google.gmail_thread_reader_client import (
    GmailReadAuthError,
    GmailReadConnectionError,
    GmailReadError,
    GmailReadMalformedResponseError,
    GmailReadNotFoundError,
    GmailReadProviderError,
    GmailReadRateLimitedError,
)

GMAIL_MESSAGES_GET_FULL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"


def _classify_and_raise(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        logger.warning(f"Gmail message-body request rejected with status {resp.status_code}.")
        raise GmailReadAuthError(f"Gmail rejected the message-body request with status {resp.status_code}.")
    if resp.status_code == 404:
        raise GmailReadNotFoundError("Gmail reports no such message in this mailbox.")
    if resp.status_code == 429:
        raise GmailReadRateLimitedError("Gmail rate-limited this read request.")
    logger.warning(f"Gmail message-body request failed with status {resp.status_code}.")
    raise GmailReadProviderError(f"Gmail message-body request failed with status {resp.status_code}.")


class GmailMessageBodyClient:
    """Real network calls to Gmail. Tests must substitute an
    httpx.MockTransport -- never exercise this against the real Gmail
    API in a test."""

    async def get_message_full(self, *, access_token: str, message_id: str) -> dict:
        """GET users.messages.get?format=full -- returns Gmail's full
        parsed response for exactly this message: `{"id", "threadId",
        "payload": {"mimeType", "headers": [...], "body": {...}, "parts":
        [...]}}`. `format=full` is the documented minimum format that
        includes body content (format=metadata never does; format=raw
        would hand back the entire undecoded RFC 2822 blob instead of
        Gmail's already-parsed MIME tree, which this client has no
        reason to prefer)."""
        url = GMAIL_MESSAGES_GET_FULL_URL.format(message_id=message_id)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {access_token}"}, params={"format": "full"}
                )
        except httpx.HTTPError as e:
            logger.warning(f"Gmail messages.get (full) request failed ({type(e).__name__}).")
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


def _decode_base64url(data: str) -> str:
    """Gmail's `body.data` is URL-safe base64, commonly without padding.
    Decoded as UTF-8 with `errors="replace"` -- a malformed/truncated
    part must never crash the Inbox; a handful of replacement
    characters in an edge case is an acceptable, honest degradation."""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _strip_html_to_text(html: str) -> str:
    """Minimal, dependency-free HTML->text for V1 -- deliberately never
    rendered as HTML in the frontend (no dangerouslySetInnerHTML
    anywhere in this codebase), so this only needs to produce readable
    plain text, not preserve formatting. Signatures/quoted history are
    NOT stripped (see this feature's own scope: "can remain for V1 if
    reliable stripping is not trivial" -- HTML quote-block detection
    across arbitrary mail clients is exactly that kind of unreliable)."""
    import re

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [line.strip() for line in text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def extract_best_body(message: dict) -> tuple[str, str] | None:
    """Walks a Gmail `format=full` message's MIME tree (payload, possibly
    recursively nested `parts`) and returns `(text, source)` where
    `source` is "plain" or "html_converted" -- plain text always
    preferred over HTML, matching this feature's own priority. Returns
    None if no text/plain or text/html leaf part carries any body data
    at all (e.g. an attachment-only or fully empty message).

    Collects EVERY matching leaf across the whole tree rather than
    stopping at the first (a multipart/alternative may list text/plain
    before text/html, but a multipart/mixed wrapping an
    multipart/alternative plus attachments must still be walked fully),
    then joins same-type leaves in document order -- correct for the
    common "one plain-text part" case and still reasonable for a rare
    multi-part-plain-text message."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(node: dict) -> None:
        mime_type = node.get("mimeType", "")
        body = node.get("body") or {}
        data = body.get("data")
        if data and mime_type == "text/plain":
            plain_parts.append(_decode_base64url(data))
        elif data and mime_type == "text/html":
            html_parts.append(_decode_base64url(data))
        for child in node.get("parts") or []:
            walk(child)

    walk(message.get("payload") or {})

    if plain_parts:
        return "\n\n".join(p for p in plain_parts if p.strip()), "plain"
    if html_parts:
        converted = "\n\n".join(_strip_html_to_text(p) for p in html_parts if p.strip())
        if converted.strip():
            return converted, "html_converted"
    return None


__all__ = [
    "GmailMessageBodyClient",
    "GmailReadError",
    "extract_best_body",
]
