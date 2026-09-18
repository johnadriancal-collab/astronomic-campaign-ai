"""
Thin, read-only HTTP client for Gmail's `users.getProfile` and
`users.history.list` -- bounce detection (2026-09-18). Same "one thin
httpx client per Gmail endpoint" philosophy as gmail_thread_reader_client.py/
gmail_message_body_client.py (see those modules' own docstrings).

`users.history.list` is the discovery mechanism this codebase uses to
find NEW inbound mail without ever scanning a mailbox -- it returns only
what changed since a given `historyId` cursor (see
MailboxHistoryCheckpoint's own docstring for why that's an opaque Gmail
cursor, never a timestamp). `historyTypes=messageAdded&labelId=INBOX` is
requested explicitly so Gmail itself filters to "a new message landed in
the inbox" -- excluding label changes, deletions, and anything in
SENT/DRAFT -- before this client ever has to look at a single message.

Reuses gmail_thread_reader_client.py's own GmailReadError hierarchy for
every failure EXCEPT one: a 404 from history.list means something
structurally different from "no such thread" (a stale/out-of-range
`startHistoryId`, per Gmail's own documented behavior) and gets its own
GmailHistoryExpiredError so callers can't accidentally treat it like an
ordinary not-found.

`get_message_metadata_light()` is the CHEAP pre-filter this module also
provides: From/Subject/Content-Type headers only (never a body/MIME-part
fetch) -- MailBounceDetectionService calls this FIRST for every
candidate message id from history.list, and only escalates to
GmailMessageBodyClient.get_message_full() (a real full-MIME fetch) for
the rare candidate whose Content-Type actually looks like a delivery
report. This is what "filter aggressively before downloading full MIME"
means in practice.

Never logs `access_token` or any header VALUE -- only HTTP status codes
and Gmail's own (non-secret) error `status`/`reason` strings on failure,
matching this codebase's other Gmail clients.
"""

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

GMAIL_PROFILE_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_HISTORY_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_MESSAGES_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"

# The cheap pre-filter's own, narrow header allowlist -- Content-Type is
# what actually distinguishes a delivery-status report from ordinary
# mail; From/Subject are supporting signals only (see
# mail_bounce_dsn_parser.py's own docstring: never sufficient by
# themselves). Deliberately a SEPARATE constant from
# gmail_thread_reader_client.METADATA_HEADERS -- that one is reply-
# detection's own fixed allowlist; widening it for an unrelated feature
# would be an unnecessary shared-behavior change.
_LIGHT_METADATA_HEADERS = ("From", "Subject", "Content-Type")

# Gmail's documented cap; also this client's own per-call safety bound on
# how many pages a single list_history() call will ever walk -- a
# pathologically large backlog (e.g. a very stale checkpoint) must not
# spin this call forever in one poll cycle. MailBounceDetectionService's
# per-mailbox cadence means a capped call simply picks up the remainder
# on the NEXT cycle -- no event is lost, only deferred.
_MAX_HISTORY_PAGES = 20


class GmailHistoryExpiredError(GmailReadError):
    """A 404 from users.history.list specifically -- Gmail's documented
    behavior when `startHistoryId` is stale/out of the retained history
    window (typically ~1 week). Distinct from GmailReadNotFoundError
    (this codebase's OTHER Gmail clients raise that for a genuinely
    missing thread/message) precisely so a caller can't accidentally
    treat "this cursor expired" the same as "that id doesn't exist" --
    see MailBounceDetectionService's own recovery-strategy docstring for
    what it does with this specifically."""


def _classify_and_raise(resp: httpx.Response, *, is_history_list: bool) -> None:
    if resp.status_code in (401, 403):
        logger.warning(f"Gmail history/profile request rejected with status {resp.status_code}.")
        raise GmailReadAuthError(f"Gmail rejected the request with status {resp.status_code}.")
    if resp.status_code == 404:
        if is_history_list:
            raise GmailHistoryExpiredError("Gmail reports startHistoryId is stale or out of range.")
        raise GmailReadNotFoundError("Gmail reports no such resource.")
    if resp.status_code == 429:
        raise GmailReadRateLimitedError("Gmail rate-limited this request.")
    logger.warning(f"Gmail history/profile request failed with status {resp.status_code}.")
    raise GmailReadProviderError(f"Gmail request failed with status {resp.status_code}.")


class GmailHistoryClient:
    """Real network calls to Gmail. Tests must substitute an
    httpx.MockTransport -- never exercise this against the real Gmail
    API in a test."""

    async def get_current_history_id(self, *, access_token: str) -> str:
        """GET users.getProfile -- the ONE call used to bootstrap a
        brand-new mailbox's checkpoint to the mailbox's CURRENT cursor
        position, so the very first bounce-poll cycle never scans
        backward into pre-existing mail (see
        MailBounceDetectionService's own bootstrap docstring)."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(GMAIL_PROFILE_GET_URL, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as e:
            logger.warning(f"Gmail getProfile request failed ({type(e).__name__}).")
            raise GmailReadConnectionError(f"Could not reach Gmail: {type(e).__name__}.") from e

        if resp.status_code != 200:
            _classify_and_raise(resp, is_history_list=False)
        try:
            data = resp.json()
        except ValueError as e:
            raise GmailReadMalformedResponseError("Gmail's getProfile response was not valid JSON.") from e
        history_id = data.get("historyId")
        if not history_id:
            raise GmailReadMalformedResponseError("Gmail's getProfile response had no historyId.")
        return history_id

    async def list_history(self, *, access_token: str, start_history_id: str, label_id: str = "INBOX") -> dict:
        """GET users.history.list?startHistoryId=...&historyTypes=
        messageAdded&labelId=INBOX -- walks every page (capped at
        _MAX_HISTORY_PAGES) and returns `{"message_ids": [...],
        "history_id": "..."}`: a deduplicated, order-preserved list of
        every NEW inbox message id added since `start_history_id`, plus
        Gmail's own new cursor value to persist as the next checkpoint.
        Raises GmailHistoryExpiredError on a 404 (see that exception's
        own docstring) -- callers must NOT retry with the same
        start_history_id."""
        message_ids: list[str] = []
        seen: set[str] = set()
        page_token: str | None = None
        latest_history_id: str | None = None

        for _ in range(_MAX_HISTORY_PAGES):
            params: list[tuple[str, str]] = [
                ("startHistoryId", start_history_id),
                ("historyTypes", "messageAdded"),
                ("labelId", label_id),
            ]
            if page_token:
                params.append(("pageToken", page_token))

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(
                        GMAIL_HISTORY_LIST_URL, headers={"Authorization": f"Bearer {access_token}"}, params=params
                    )
            except httpx.HTTPError as e:
                logger.warning(f"Gmail history.list request failed ({type(e).__name__}).")
                raise GmailReadConnectionError(f"Could not reach Gmail: {type(e).__name__}.") from e

            if resp.status_code != 200:
                _classify_and_raise(resp, is_history_list=True)
            try:
                data = resp.json()
            except ValueError as e:
                raise GmailReadMalformedResponseError("Gmail's history.list response was not valid JSON.") from e

            for entry in data.get("history") or []:
                for added in entry.get("messagesAdded") or []:
                    message = added.get("message") or {}
                    message_id = message.get("id")
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        message_ids.append(message_id)

            if data.get("historyId"):
                latest_history_id = data["historyId"]

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        if latest_history_id is None:
            # No history entries at all since start_history_id (nothing
            # changed) -- the checkpoint doesn't move, which is correct:
            # re-polling with the SAME start_history_id next cycle is
            # exactly right when nothing happened.
            latest_history_id = start_history_id

        return {"message_ids": message_ids, "history_id": latest_history_id}

    async def get_message_metadata_light(self, *, access_token: str, message_id: str) -> dict:
        """GET users.messages.get?format=metadata, restricted to
        From/Subject/Content-Type -- the cheap pre-filter call. Returns
        Gmail's parsed response; use gmail_thread_reader_client.
        extract_headers() to pull a flat header dict back out of it
        (same response shape, same helper)."""
        url = GMAIL_MESSAGES_GET_URL.format(message_id=message_id)
        params: list[tuple[str, str]] = [("format", "metadata")]
        params.extend(("metadataHeaders", h) for h in _LIGHT_METADATA_HEADERS)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=params)
        except httpx.HTTPError as e:
            logger.warning(f"Gmail messages.get (light) request failed ({type(e).__name__}).")
            raise GmailReadConnectionError(f"Could not reach Gmail: {type(e).__name__}.") from e

        if resp.status_code != 200:
            _classify_and_raise(resp, is_history_list=False)
        try:
            data = resp.json()
        except ValueError as e:
            raise GmailReadMalformedResponseError("Gmail's messages.get response was not valid JSON.") from e
        if not data.get("id"):
            raise GmailReadMalformedResponseError("Gmail's messages.get response had no id.")
        return data


__all__ = ["GmailHistoryClient", "GmailHistoryExpiredError", "GmailReadError"]
