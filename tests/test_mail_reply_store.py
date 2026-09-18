"""
MailReplyStore.set_reply_preview_if_absent() -- the ONE narrow exception
to "a MailReply row is never mutated after creation" (2026-09-18). Same
tests run against both the Memory and SQLite implementations via a
fixture parametrized by store class, matching this codebase's usual
per-store-pair test convention.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailReply
from app.repositories.mail_reply_store import MemoryMailReplyStore
from app.repositories.sqlite_mail_reply_store import SQLiteMailReplyStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def make_reply(enrollment_id="e1", reply_preview: str | None = None) -> MailReply:
    return MailReply(
        enrollment_id=enrollment_id,
        mail_campaign_id="c1",
        crm_contact_id="contact1",
        mailbox_id="mbx-1",
        gmail_thread_id="thr-1",
        gmail_message_id="msg-1",
        reply_email_normalized="lead@example.com",
        detected_at=NOW,
        created_at=NOW,
        reply_preview=reply_preview,
    )


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryMailReplyStore()
    else:
        s = SQLiteMailReplyStore(str(tmp_path / "test.db"))
        await s.connect()
        yield s
        await s.close()


async def test_sets_preview_on_a_row_with_none(store):
    await store.create(make_reply("e1"))

    result = await store.set_reply_preview_if_absent("e1", "got it, thanks.")

    assert result is True
    reply = await store.get("e1")
    assert reply.reply_preview == "got it, thanks."


async def test_does_not_overwrite_an_existing_preview(store):
    await store.create(make_reply("e1", reply_preview="original preview"))

    result = await store.set_reply_preview_if_absent("e1", "a different preview")

    assert result is False
    reply = await store.get("e1")
    assert reply.reply_preview == "original preview"


async def test_returns_false_for_a_nonexistent_enrollment(store):
    result = await store.set_reply_preview_if_absent("does-not-exist", "some preview")
    assert result is False


async def test_setting_the_preview_touches_no_other_field(store):
    original = make_reply("e1")
    await store.create(original)

    await store.set_reply_preview_if_absent("e1", "got it, thanks.")

    reply = await store.get("e1")
    assert reply.enrollment_id == original.enrollment_id
    assert reply.mail_campaign_id == original.mail_campaign_id
    assert reply.gmail_thread_id == original.gmail_thread_id
    assert reply.gmail_message_id == original.gmail_message_id
    assert reply.reply_email_normalized == original.reply_email_normalized
    assert reply.detected_at == original.detected_at


async def test_idempotent_across_repeated_calls(store):
    await store.create(make_reply("e1"))

    first = await store.set_reply_preview_if_absent("e1", "got it, thanks.")
    second = await store.set_reply_preview_if_absent("e1", "a totally different string")

    assert first is True
    assert second is False
    reply = await store.get("e1")
    assert reply.reply_preview == "got it, thanks."
