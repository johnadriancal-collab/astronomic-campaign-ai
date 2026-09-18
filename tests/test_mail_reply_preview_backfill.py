"""
backfill_reply_previews() -- one-time startup backfill (2026-09-18). See
that module's own docstring for why this runs at startup rather than as
a separate script.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.google.gmail_thread_reader_client import GmailReadProviderError
from app.google.oauth_client import GoogleRefreshTokenInvalidError
from app.models.mail import MailReply
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mail_reply_store import MemoryMailReplyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.mail_reply_preview_backfill import backfill_reply_previews

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def make_reply(enrollment_id="e1", reply_preview: str | None = None, mailbox_id="mbx-1") -> MailReply:
    return MailReply(
        enrollment_id=enrollment_id,
        mail_campaign_id="c1",
        crm_contact_id="contact1",
        mailbox_id=mailbox_id,
        gmail_thread_id="thr-1",
        gmail_message_id="msg-1",
        reply_email_normalized="lead@example.com",
        detected_at=NOW,
        created_at=NOW,
        reply_preview=reply_preview,
    )


def make_mailbox(mailbox_id="mbx-1", email="victoria@useastronomic.com") -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id,
        provider=MailboxProvider.GOOGLE,
        email=email,
        display_name=None,
        status=MailboxStatus.CONNECTED,
        google_user_id="g-1",
        granted_scopes=[],
        connected_at=NOW,
        updated_at=NOW,
    )


class FakeMailboxService:
    def __init__(self, token: str = "tok-1", refresh_error: Exception | None = None):
        self.token = token
        self.refresh_error = refresh_error
        self.refresh_calls: list[str] = []

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        self.refresh_calls.append(mailbox_id)
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.token


class FakeGmailThreadReaderClient:
    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.errors: dict[str, Exception] = {}
        self.get_thread_calls: list[tuple[str, str]] = []

    async def get_thread(self, *, access_token: str, thread_id: str) -> dict:
        self.get_thread_calls.append((access_token, thread_id))
        if thread_id in self.errors:
            raise self.errors[thread_id]
        return self.threads.get(thread_id, {"id": thread_id, "historyId": "1", "messages": []})


@pytest_asyncio.fixture
async def env():
    reply_store = MemoryMailReplyStore()
    mailbox_store = MemoryMailboxStore()
    mailbox_service = FakeMailboxService()
    reader = FakeGmailThreadReaderClient()
    return reply_store, mailbox_store, mailbox_service, reader


async def test_backfills_a_reply_missing_a_preview(env):
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    reader.threads["thr-1"] = {
        "id": "thr-1",
        "historyId": "1",
        "messages": [
            {
                "id": "msg-1",
                "snippet": "got it, thanks. On Thu, Sep 17, 2026 at 12:46 PM &lt;victoria@useastronomic.com&gt; wrote: Hi,",
            }
        ],
    }

    filled = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert filled == 1
    reply = await reply_store.get("e1")
    assert reply.reply_preview == "got it, thanks."


async def test_skips_a_row_that_already_has_a_preview_idempotent(env):
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1", reply_preview="already there"))
    reader.threads["thr-1"] = {"id": "thr-1", "historyId": "1", "messages": [{"id": "msg-1", "snippet": "a different snippet entirely"}]}

    filled = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert filled == 0
    reply = await reply_store.get("e1")
    assert reply.reply_preview == "already there"
    # Never even asked Gmail -- the "already has a preview" check happens
    # before any refresh/thread-fetch is attempted.
    assert reader.get_thread_calls == []
    assert mailbox_service.refresh_calls == []


async def test_running_twice_in_a_row_is_a_no_op_the_second_time(env):
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    reader.threads["thr-1"] = {"id": "thr-1", "historyId": "1", "messages": [{"id": "msg-1", "snippet": "hello there"}]}

    first = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)
    second = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert first == 1
    assert second == 0


async def test_mailbox_needing_reauth_is_skipped_leaving_preview_null_without_breaking_the_batch(env):
    reply_store, mailbox_store, _mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    await reply_store.create(make_reply("e2", mailbox_id="mbx-1"))
    broken_mailbox_service = FakeMailboxService(refresh_error=GoogleRefreshTokenInvalidError("needs reauth"))
    reader.threads["thr-1"] = {"id": "thr-1", "historyId": "1", "messages": [{"id": "msg-1", "snippet": "hello"}]}

    filled = await backfill_reply_previews(reply_store, mailbox_store, broken_mailbox_service, reader)

    assert filled == 0
    reply1 = await reply_store.get("e1")
    reply2 = await reply_store.get("e2")
    assert reply1.reply_preview is None
    assert reply2.reply_preview is None


async def test_gmail_read_failure_is_skipped_leaving_preview_null_without_breaking_the_batch(env):
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    reader.errors["thr-1"] = GmailReadProviderError("boom")

    filled = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert filled == 0
    reply = await reply_store.get("e1")
    assert reply.reply_preview is None


async def test_message_id_not_found_in_thread_is_skipped_gracefully(env):
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    reader.threads["thr-1"] = {"id": "thr-1", "historyId": "1", "messages": [{"id": "some-other-message-id", "snippet": "hi"}]}

    filled = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert filled == 0
    reply = await reply_store.get("e1")
    assert reply.reply_preview is None


async def test_never_scans_beyond_known_mail_reply_rows(env):
    """Only iterates reply_store.list_all() -- no mailbox-wide scanning,
    no attempt to discover replies this store doesn't already know
    about."""
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    # No MailReply rows created at all.

    filled = await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    assert filled == 0
    assert reader.get_thread_calls == []
    assert mailbox_service.refresh_calls == []


async def test_never_persists_the_full_reply_body(env):
    """The backfill only ever writes the derived, length-capped preview
    -- never the raw snippet, never a full body field (which doesn't
    even exist on MailReply)."""
    reply_store, mailbox_store, mailbox_service, reader = env
    await mailbox_store.create(make_mailbox())
    await reply_store.create(make_reply("e1"))
    long_quote = "x" * 5000
    reader.threads["thr-1"] = {
        "id": "thr-1",
        "historyId": "1",
        "messages": [{"id": "msg-1", "snippet": f"got it, thanks. On Thu, Sep 17, 2026 at 12:46 PM &lt;victoria@useastronomic.com&gt; wrote: {long_quote}"}],
    }

    await backfill_reply_previews(reply_store, mailbox_store, mailbox_service, reader)

    reply = await reply_store.get("e1")
    assert reply.reply_preview == "got it, thanks."
    assert len(reply.reply_preview) < 200
