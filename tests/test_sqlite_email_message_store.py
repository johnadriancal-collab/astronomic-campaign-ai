"""
Persistence tests for SQLiteEmailMessageStore and SQLiteEmailMessageEventStore
-- same contract the Memory variants satisfy, plus data surviving a fresh
connection to the same file.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.email_message import EmailMessage, EmailMessageEvent, EmailMessageSource
from app.repositories.email_message_store import EmailMessageNotFoundError
from app.repositories.sqlite_email_message_event_store import SQLiteEmailMessageEventStore
from app.repositories.sqlite_email_message_store import SQLiteEmailMessageStore


def make_message(email_message_id: str, apollo_message_id: str | None, email_sequence_id: str = "seq-1") -> EmailMessage:
    return EmailMessage(
        email_message_id=email_message_id,
        apollo_message_id=apollo_message_id,
        email_sequence_id=email_sequence_id,
        lead_id="lead-1",
        status="completed",
        source=EmailMessageSource.APOLLO_SYNC if apollo_message_id else EmailMessageSource.TEST_FIXTURE,
    )


def make_event(email_message_event_id: str, email_message_id: str, apollo_event_id: str | None) -> EmailMessageEvent:
    return EmailMessageEvent(
        email_message_event_id=email_message_event_id,
        email_message_id=email_message_id,
        apollo_event_id=apollo_event_id,
        event_type="open",
        occurred_at=datetime.now(timezone.utc),
        source=EmailMessageSource.APOLLO_SYNC if apollo_event_id else EmailMessageSource.TEST_FIXTURE,
    )


@pytest_asyncio.fixture
async def message_store(tmp_path):
    store = SQLiteEmailMessageStore(str(tmp_path / "messages.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def event_store(tmp_path):
    store = SQLiteEmailMessageEventStore(str(tmp_path / "events.db"))
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(message_store):
    await message_store.create(make_message("m1", "apollo-m1"))

    fetched = await message_store.get("m1")
    assert fetched is not None
    assert fetched.apollo_message_id == "apollo-m1"
    assert fetched.status == "completed"
    assert fetched.source == EmailMessageSource.APOLLO_SYNC


@pytest.mark.asyncio
async def test_get_by_apollo_message_id(message_store):
    await message_store.create(make_message("m1", "apollo-m1"))

    found = await message_store.get_by_apollo_message_id("apollo-m1")
    assert found is not None
    assert found.email_message_id == "m1"
    assert await message_store.get_by_apollo_message_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_multiple_fixture_messages_with_null_apollo_id_coexist(message_store):
    """apollo_message_id is UNIQUE but nullable -- fixtures (None) must not collide."""
    await message_store.create(make_message("m1", None))
    await message_store.create(make_message("m2", None))

    assert await message_store.get("m1") is not None
    assert await message_store.get("m2") is not None


@pytest.mark.asyncio
async def test_duplicate_apollo_message_id_raises(message_store):
    await message_store.create(make_message("m1", "apollo-m1"))
    with pytest.raises(ValueError):
        await message_store.create(make_message("m2", "apollo-m1"))


@pytest.mark.asyncio
async def test_save_missing_message_raises_not_found(message_store):
    with pytest.raises(EmailMessageNotFoundError):
        await message_store.save(make_message("does-not-exist", "apollo-x"))


@pytest.mark.asyncio
async def test_save_persists_mutations(message_store):
    message = make_message("m1", "apollo-m1")
    await message_store.create(message)

    message.status = "failed"
    message.bounce = True
    await message_store.save(message)

    fetched = await message_store.get("m1")
    assert fetched.status == "failed"
    assert fetched.bounce is True


@pytest.mark.asyncio
async def test_list_for_sequence_scopes_correctly(message_store):
    await message_store.create(make_message("m1", "apollo-m1", email_sequence_id="seq-a"))
    await message_store.create(make_message("m2", "apollo-m2", email_sequence_id="seq-b"))

    seq_a = await message_store.list_for_sequence("seq-a")
    assert [m.email_message_id for m in seq_a] == ["m1"]


@pytest.mark.asyncio
async def test_messages_survive_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist_test.db")

    first = SQLiteEmailMessageStore(db_path)
    await first.connect()
    await first.create(make_message("m1", "apollo-m1"))
    await first.close()

    second = SQLiteEmailMessageStore(db_path)
    await second.connect()
    fetched = await second.get("m1")
    await second.close()

    assert fetched is not None
    assert fetched.apollo_message_id == "apollo-m1"


@pytest.mark.asyncio
async def test_event_create_and_list_for_message(event_store):
    await event_store.create(make_event("e1", "m1", "apollo-e1"))
    await event_store.create(make_event("e2", "m1", "apollo-e2"))
    await event_store.create(make_event("e3", "m2", "apollo-e3"))

    events = await event_store.list_for_message("m1")
    assert {e.email_message_event_id for e in events} == {"e1", "e2"}


@pytest.mark.asyncio
async def test_event_get_by_apollo_event_id(event_store):
    await event_store.create(make_event("e1", "m1", "apollo-e1"))

    found = await event_store.get_by_apollo_event_id("apollo-e1")
    assert found is not None
    assert found.email_message_event_id == "e1"
    assert await event_store.get_by_apollo_event_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_multiple_fixture_events_with_null_apollo_id_coexist(event_store):
    await event_store.create(make_event("e1", "m1", None))
    await event_store.create(make_event("e2", "m1", None))

    events = await event_store.list_for_message("m1")
    assert len(events) == 2


@pytest.mark.asyncio
async def test_duplicate_apollo_event_id_raises(event_store):
    await event_store.create(make_event("e1", "m1", "apollo-e1"))
    with pytest.raises(ValueError):
        await event_store.create(make_event("e2", "m1", "apollo-e1"))


@pytest.mark.asyncio
async def test_events_survive_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "events_persist.db")

    first = SQLiteEmailMessageEventStore(db_path)
    await first.connect()
    await first.create(make_event("e1", "m1", "apollo-e1"))
    await first.close()

    second = SQLiteEmailMessageEventStore(db_path)
    await second.connect()
    events = await second.list_for_message("m1")
    await second.close()

    assert len(events) == 1
    assert events[0].apollo_event_id == "apollo-e1"
