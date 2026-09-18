"""
backfill_reply_previews() -- one-time startup backfill (2026-09-18) for
every existing MailReply row that predates `reply_preview`.

Runs from app/main.py's lifespan startup, after every store/service is
already wired -- NOT a manual script, NOT a `railway run`/ssh operation
(this app's SQLite database lives on a Railway-attached volume mounted
only inside the running container; neither `railway run` nor a local
script can reach it). Idempotent by construction: iterates ONLY
`MailReplyStore.list_all()`'s existing rows (never a mailbox-wide scan),
skips any row that already has a `reply_preview` (via
`set_reply_preview_if_absent()`'s own contract -- a no-op, not
overwritten), so re-running this on every future deploy costs nothing
once every row has been filled.

For each row still missing a preview: refreshes that row's own mailbox's
access token and fetches the ONE already-known thread
(`reply.gmail_thread_id`, metadata scope -- the exact same call shape
reply detection itself makes, no new scope), finds the specific message
matching `reply.gmail_message_id`, and derives a preview from its
`snippet` via the SAME app/services/mail_reply_preview.py heuristic
reply detection uses for every future reply. Any single row's failure
(mailbox needs reauth, thread/message vanished, a transient Gmail error)
is caught, logged, and skipped -- `reply_preview` stays None and the
next candidate is still attempted; a failure here must never block
startup or half-run the batch.
"""

from loguru import logger

from app.google.gmail_thread_reader_client import GmailReadError, GmailThreadReaderClient
from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.repositories.mail_reply_store import MailReplyStore
from app.repositories.mailbox_store import MailboxStore
from app.services.mail_reply_preview import derive_reply_preview
from app.services.mailbox_service import MailboxCredentialMissingError, MailboxNotFound, MailboxService


async def backfill_reply_previews(
    reply_store: MailReplyStore,
    mailbox_store: MailboxStore,
    mailbox_service: MailboxService,
    thread_reader_client: GmailThreadReaderClient | None = None,
) -> int:
    """Returns the number of rows this call actually filled in (0 on a
    fully-backfilled system, which is the expected steady state after
    the first successful run)."""
    client = thread_reader_client or GmailThreadReaderClient()
    filled = 0

    for reply in await reply_store.list_all():
        if reply.reply_preview:
            continue

        try:
            access_token = await mailbox_service.refresh_mailbox_access_token(reply.mailbox_id)
        except (GoogleRefreshTokenInvalidError, GoogleTokenRefreshError, MailboxCredentialMissingError, MailboxNotFound):
            logger.warning(f"Reply-preview backfill: could not refresh access token for mailbox {reply.mailbox_id}; skipping enrollment {reply.enrollment_id}.")
            continue

        try:
            thread = await client.get_thread(access_token=access_token, thread_id=reply.gmail_thread_id)
        except GmailReadError:
            logger.warning(f"Reply-preview backfill: Gmail read failed for thread {reply.gmail_thread_id}; skipping enrollment {reply.enrollment_id}.")
            continue

        message = next((m for m in thread.get("messages", []) if m.get("id") == reply.gmail_message_id), None)
        if message is None:
            logger.warning(f"Reply-preview backfill: message {reply.gmail_message_id} not found in thread {reply.gmail_thread_id}; skipping enrollment {reply.enrollment_id}.")
            continue

        mailbox = await mailbox_store.get(reply.mailbox_id)
        preview = derive_reply_preview(message.get("snippet"), mailbox.email if mailbox is not None else None)
        if not preview:
            continue

        if await reply_store.set_reply_preview_if_absent(reply.enrollment_id, preview):
            filled += 1

    if filled:
        logger.info(f"Reply-preview backfill: filled {filled} existing MailReply row(s).")
    return filled
