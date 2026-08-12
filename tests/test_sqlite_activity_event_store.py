"""
Persistence tests for SQLiteActivityEventStore -- data surviving a fresh
connection to the same file (the "survives backend restarts" contract),
newest-first ordering, and graceful handling of a corrupted row (one
unreadable event must never break retrieval of every other event).
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory, ActivityEvent, ActivitySource
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore


def make_event(event_id: str, **overrides) -> ActivityEvent:
    defaults = dict(
        event_id=event_id,
        event_type="contact.created",
        category=ActivityCategory.CONTACTS,
        created_at=datetime.now(timezone.utc),
        source=ActivitySource.MANUAL_CRM,
        summary=f"summary for {event_id}",
    )
    defaults.update(overrides)
    return ActivityEvent(**defaults)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteActivityEventStore(str(tmp_path / "activity_events.db"))
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_and_list_roundtrip(store):
    await store.create(make_event("e1", summary="Ada Lovelace was created."))
    events = await store.list()
    assert len(events) == 1
    assert events[0].summary == "Ada Lovelace was created."
    assert events[0].metadata == {}


@pytest.mark.asyncio
async def test_list_is_newest_first(store):
    now = datetime.now(timezone.utc)
    await store.create(make_event("e1", created_at=now - timedelta(minutes=2), summary="oldest"))
    await store.create(make_event("e2", created_at=now, summary="newest"))
    await store.create(make_event("e3", created_at=now - timedelta(minutes=1), summary="middle"))

    events = await store.list()
    assert [e.summary for e in events] == ["newest", "middle", "oldest"]


@pytest.mark.asyncio
async def test_data_survives_a_fresh_connection_to_the_same_file(tmp_path):
    db_path = str(tmp_path / "activity_events.db")
    store1 = SQLiteActivityEventStore(db_path)
    await store1.connect()
    await store1.create(make_event("e1", summary="persisted across reconnect"))
    await store1.close()

    store2 = SQLiteActivityEventStore(db_path)
    await store2.connect()
    events = await store2.list()
    await store2.close()

    assert len(events) == 1
    assert events[0].summary == "persisted across reconnect"


@pytest.mark.asyncio
async def test_metadata_roundtrips_as_structured_data(store):
    await store.create(make_event("e1", metadata={"added": 89, "already_member": 4}))
    events = await store.list()
    assert events[0].metadata == {"added": 89, "already_member": 4}


@pytest.mark.asyncio
async def test_duplicate_event_id_raises(store):
    await store.create(make_event("e1"))
    with pytest.raises(ValueError):
        await store.create(make_event("e1"))


@pytest.mark.asyncio
async def test_malformed_row_is_skipped_not_fatal(store, tmp_path):
    """One corrupted row must never break retrieval of every other event --
    simulates corruption by writing an unparseable `data` blob directly,
    bypassing the model entirely."""
    await store.create(make_event("good-1", summary="readable event"))

    raw_conn = await aiosqlite.connect(str(tmp_path / "activity_events.db"))
    await raw_conn.execute(
        "INSERT INTO activity_events (event_id, category, created_at, data) VALUES (?, ?, ?, ?)",
        ("corrupted-1", "contacts", datetime.now(timezone.utc).isoformat(), "{not valid json"),
    )
    await raw_conn.commit()
    await raw_conn.close()

    events = await store.list()
    assert len(events) == 1
    assert events[0].summary == "readable event"
