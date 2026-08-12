"""
Persistence tests for SQLiteEmailIntakeStore -- unique gmail_message_id
enforcement, data surviving a fresh connection to the same file, newest-
first ordering, and graceful handling of a corrupted row.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.email_intake import EmailIntakeItem, EmailIntakeStatus
from app.repositories.email_intake_store import EmailIntakeDuplicateError
from app.repositories.sqlite_email_intake_store import SQLiteEmailIntakeStore


def make_item(intake_id: str, gmail_message_id: str | None = None, **overrides) -> EmailIntakeItem:
    now = datetime.now(timezone.utc)
    defaults = dict(
        intake_id=intake_id,
        gmail_message_id=gmail_message_id or f"msg-{intake_id}",
        received_at=now,
        sender="amos@example.com",
        subject="Update",
        body_text="body",
        status=EmailIntakeStatus.PENDING_REVIEW,
        created_at=now,
    )
    defaults.update(overrides)
    return EmailIntakeItem(**defaults)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteEmailIntakeStore(str(tmp_path / "email_intake.db"))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(store):
    item = make_item("i1")
    await store.create(item)
    fetched = await store.get("i1")
    assert fetched == item


@pytest.mark.asyncio
async def test_get_by_gmail_message_id(store):
    item = make_item("i1", gmail_message_id="gmail-abc")
    await store.create(item)
    fetched = await store.get_by_gmail_message_id("gmail-abc")
    assert fetched is not None
    assert fetched.intake_id == "i1"


@pytest.mark.asyncio
async def test_duplicate_gmail_message_id_raises(store):
    await store.create(make_item("i1", gmail_message_id="gmail-abc"))
    with pytest.raises(EmailIntakeDuplicateError):
        await store.create(make_item("i2", gmail_message_id="gmail-abc"))


@pytest.mark.asyncio
async def test_list_newest_first(store):
    now = datetime.now(timezone.utc)
    await store.create(make_item("older", created_at=now - timedelta(hours=1)))
    await store.create(make_item("newer", created_at=now))
    items = await store.list()
    assert [i.intake_id for i in items] == ["newer", "older"]


@pytest.mark.asyncio
async def test_save_updates_status(store):
    item = make_item("i1")
    await store.create(item)
    item.status = EmailIntakeStatus.APPROVED
    item.reviewed_at = datetime.now(timezone.utc)
    await store.save(item)
    fetched = await store.get("i1")
    assert fetched.status == EmailIntakeStatus.APPROVED
    assert fetched.reviewed_at is not None


@pytest.mark.asyncio
async def test_persists_across_reconnect(tmp_path):
    db_path = str(tmp_path / "email_intake.db")
    store1 = SQLiteEmailIntakeStore(db_path)
    await store1.connect()
    await store1.create(make_item("i1"))
    await store1.close()

    store2 = SQLiteEmailIntakeStore(db_path)
    await store2.connect()
    fetched = await store2.get("i1")
    assert fetched is not None
    assert fetched.intake_id == "i1"
    await store2.close()


@pytest.mark.asyncio
async def test_corrupted_row_does_not_break_list(store):
    await store.create(make_item("good"))
    # Insert a row with unparseable JSON directly, bypassing the model.
    await store._connection.execute(
        "INSERT INTO email_intake_items (intake_id, gmail_message_id, status, created_at, data) VALUES (?, ?, ?, ?, ?)",
        ("bad", "gmail-bad", "pending_review", datetime.now(timezone.utc).isoformat(), "{not json"),
    )
    await store._connection.commit()
    items = await store.list()
    assert [i.intake_id for i in items] == ["good"]
