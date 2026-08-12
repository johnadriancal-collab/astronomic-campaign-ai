"""
Tests for ActivityLogService: persistence, chronological ordering, category
filtering, search, pagination, and the best-effort failure contract (a
logging failure must never raise back to the caller).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.models.activity import ActivityCategory, ActivitySource
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.activity_log_service import ActivityLogService


@pytest.fixture
def service():
    return ActivityLogService(store=MemoryActivityEventStore())


@pytest.mark.asyncio
async def test_record_persists_and_returns_the_event(service):
    event = await service.record(
        event_type="contact.created",
        category=ActivityCategory.CONTACTS,
        source=ActivitySource.MANUAL_CRM,
        summary="Ada Lovelace was manually created in the CRM.",
        entity_type="contact",
        entity_id="c1",
        entity_name="Ada Lovelace",
    )
    assert event is not None
    assert event.event_id
    assert event.actor is None  # no auth system exists -- never invented

    page = await service.list_events()
    assert page.total == 1
    assert page.items[0].event_id == event.event_id


@pytest.mark.asyncio
async def test_events_are_returned_newest_first(service):
    await service.record("a", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "first")
    await service.record("b", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "second")
    await service.record("c", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "third")

    page = await service.list_events()
    assert [e.summary for e in page.items] == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_category_filter(service):
    await service.record("itf.submission_received", ActivityCategory.ITF, ActivitySource.ITF_AUTOMATION, "itf event")
    await service.record("list.created", ActivityCategory.LISTS, ActivitySource.LISTS, "list event")

    page = await service.list_events(category=ActivityCategory.ITF)
    assert page.total == 1
    assert page.items[0].summary == "itf event"


@pytest.mark.asyncio
async def test_search_matches_summary_and_entity_name(service):
    await service.record(
        "contact.created", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM,
        "Amos Ben-Meir was manually created.", entity_name="Amos Ben-Meir",
    )
    await service.record("contact.created", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "Someone Else was created.")

    page = await service.list_events(q="ben-meir")  # case-insensitive
    assert page.total == 1
    assert page.items[0].entity_name == "Amos Ben-Meir"


@pytest.mark.asyncio
async def test_date_range_filter(service):
    now = datetime.now(timezone.utc)
    old_event = await service.record("a", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "old")
    # Backdate directly on the store's dict (record() always stamps "now") --
    # simplest way to exercise a real date boundary deterministically.
    old_event_dated = old_event.model_copy(update={"created_at": now - timedelta(days=5)})
    service.store._events[old_event.event_id] = old_event_dated  # type: ignore[attr-defined]
    await service.record("b", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "recent")

    page = await service.list_events(date_from=now - timedelta(days=1))
    assert page.total == 1
    assert page.items[0].summary == "recent"


@pytest.mark.asyncio
async def test_pagination(service):
    for i in range(5):
        await service.record(f"event-{i}", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, f"summary {i}")

    page1 = await service.list_events(page=1, page_size=2)
    page2 = await service.list_events(page=2, page_size=2)
    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert page1.items[0].event_id != page2.items[0].event_id


@pytest.mark.asyncio
async def test_record_never_raises_when_the_store_write_fails():
    """The best-effort contract: a failing store must not propagate to the
    caller. This is what lets every emission call site in CrmService/
    CrmImportService/ItfIngestionService/CampaignService skip their own
    try/except around record()."""
    failing_store = AsyncMock()
    failing_store.create.side_effect = RuntimeError("disk full")
    service = ActivityLogService(store=failing_store)

    result = await service.record(
        event_type="contact.created",
        category=ActivityCategory.CONTACTS,
        source=ActivitySource.MANUAL_CRM,
        summary="should not raise",
    )
    assert result is None  # failure is signaled by None, never an exception


@pytest.mark.asyncio
async def test_metadata_defaults_to_empty_dict(service):
    event = await service.record("a", ActivityCategory.CONTACTS, ActivitySource.MANUAL_CRM, "no metadata passed")
    assert event.metadata == {}
