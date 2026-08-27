"""
LumaEventStore / LumaRegistrationStore / LumaQuestionMappingStore /
LumaBackfillCheckpointStore -- both Memory and SQLite implementations,
parametrized so every test runs against both (matching this repo's ABC +
Memory + SQLite convention -- the two must behave identically)."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.luma import (
    LumaApprovalStatus,
    LumaBackfillCheckpoint,
    LumaBackfillStatus,
    LumaEvent,
    LumaMatchStatus,
    LumaQuestionMapping,
    LumaRegistration,
)
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.repositories.sqlite_luma_backfill_checkpoint_store import SQLiteLumaBackfillCheckpointStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_question_mapping_store import SQLiteLumaQuestionMappingStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_event(**overrides) -> LumaEvent:
    defaults = dict(
        luma_event_id="evt-1", calendar_id="cal-1", name="Hotshot Dinner",
        start_at=_now(), end_at=_now(), status=None, location_summary="Austin",
        url="https://lu.ma/evt-1", synced_at=_now(), updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaEvent(**defaults)


def make_registration(**overrides) -> LumaRegistration:
    defaults = dict(
        luma_guest_id="gst-1", luma_event_id="evt-1", crm_contact_id=None,
        email_normalized="alice@example.com", match_status=LumaMatchStatus.MATCHED,
        approval_status=LumaApprovalStatus.APPROVED, registered_at=_now(),
        synced_at=_now(), updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


def make_mapping(**overrides) -> LumaQuestionMapping:
    defaults = dict(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="LinkedIn Profile",
        target_field_key="linkedin_url", active=True, created_at=_now(), updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaQuestionMapping(**defaults)


# --- LumaEventStore ------------------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def event_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryLumaEventStore()
    else:
        store = SQLiteLumaEventStore(str(tmp_path / "test.db"))
        await store.connect()
        yield store
        await store.close()


async def test_event_save_then_get(event_store):
    await event_store.save(make_event())
    fetched = await event_store.get("evt-1")
    assert fetched is not None
    assert fetched.name == "Hotshot Dinner"


async def test_event_save_is_an_upsert(event_store):
    await event_store.save(make_event(name="Original Name"))
    await event_store.save(make_event(name="Renamed"))
    fetched = await event_store.get("evt-1")
    assert fetched.name == "Renamed"
    assert len(await event_store.list()) == 1


async def test_event_get_unknown_returns_none(event_store):
    assert await event_store.get("nonexistent") is None


# --- LumaRegistrationStore -------------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def registration_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryLumaRegistrationStore()
    else:
        store = SQLiteLumaRegistrationStore(str(tmp_path / "test.db"))
        await store.connect()
        yield store
        await store.close()


async def test_registration_save_then_get(registration_store):
    await registration_store.save(make_registration())
    fetched = await registration_store.get("gst-1")
    assert fetched is not None
    assert fetched.approval_status == LumaApprovalStatus.APPROVED


async def test_registration_save_is_an_upsert_keyed_on_guest_id(registration_store):
    await registration_store.save(make_registration(approval_status=LumaApprovalStatus.PENDING_APPROVAL))
    await registration_store.save(make_registration(approval_status=LumaApprovalStatus.APPROVED))
    fetched = await registration_store.get("gst-1")
    assert fetched.approval_status == LumaApprovalStatus.APPROVED
    assert len(await registration_store.list()) == 1  # never a second row


async def test_registration_list_for_event(registration_store):
    await registration_store.save(make_registration(luma_guest_id="gst-1", luma_event_id="evt-1"))
    await registration_store.save(make_registration(luma_guest_id="gst-2", luma_event_id="evt-1"))
    await registration_store.save(make_registration(luma_guest_id="gst-3", luma_event_id="evt-2"))

    for_evt1 = await registration_store.list_for_event("evt-1")
    assert {r.luma_guest_id for r in for_evt1} == {"gst-1", "gst-2"}


async def test_registration_list_for_contact(registration_store):
    await registration_store.save(make_registration(luma_guest_id="gst-1", crm_contact_id="contact-a"))
    await registration_store.save(make_registration(luma_guest_id="gst-2", crm_contact_id="contact-a", luma_event_id="evt-2"))
    await registration_store.save(make_registration(luma_guest_id="gst-3", crm_contact_id="contact-b"))

    for_contact_a = await registration_store.list_for_contact("contact-a")
    assert {r.luma_guest_id for r in for_contact_a} == {"gst-1", "gst-2"}


async def test_registration_with_null_crm_contact_id_needs_review(registration_store):
    await registration_store.save(
        make_registration(crm_contact_id=None, match_status=LumaMatchStatus.NEEDS_REVIEW)
    )
    fetched = await registration_store.get("gst-1")
    assert fetched.crm_contact_id is None
    assert fetched.match_status == LumaMatchStatus.NEEDS_REVIEW


# --- LumaQuestionMappingStore -----------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def mapping_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryLumaQuestionMappingStore()
    else:
        store = SQLiteLumaQuestionMappingStore(str(tmp_path / "test.db"))
        await store.connect()
        yield store
        await store.close()


async def test_mapping_create_then_get(mapping_store):
    mapping = make_mapping()
    await mapping_store.create(mapping)
    fetched = await mapping_store.get(mapping.luma_question_mapping_id)
    assert fetched.question_label == "LinkedIn Profile"


async def test_mapping_list_excludes_inactive_when_asked(mapping_store):
    active = make_mapping(luma_question_mapping_id=str(uuid.uuid4()), active=True)
    inactive = make_mapping(luma_question_mapping_id=str(uuid.uuid4()), active=False)
    await mapping_store.create(active)
    await mapping_store.create(inactive)

    all_mappings = await mapping_store.list(include_inactive=True)
    active_only = await mapping_store.list(include_inactive=False)
    assert len(all_mappings) == 2
    assert len(active_only) == 1
    assert active_only[0].luma_question_mapping_id == active.luma_question_mapping_id


async def test_mapping_save_updates_in_place(mapping_store):
    mapping = make_mapping(active=True)
    await mapping_store.create(mapping)
    mapping.active = False
    await mapping_store.save(mapping)

    fetched = await mapping_store.get(mapping.luma_question_mapping_id)
    assert fetched.active is False


# --- LumaBackfillCheckpointStore --------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def checkpoint_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryLumaBackfillCheckpointStore()
    else:
        store = SQLiteLumaBackfillCheckpointStore(str(tmp_path / "test.db"))
        await store.connect()
        yield store
        await store.close()


async def test_checkpoint_unset_returns_none(checkpoint_store):
    assert await checkpoint_store.get() is None


async def test_checkpoint_save_then_get(checkpoint_store):
    checkpoint = LumaBackfillCheckpoint(status=LumaBackfillStatus.RUNNING, event_cursor="cursor-abc")
    await checkpoint_store.save(checkpoint)
    fetched = await checkpoint_store.get()
    assert fetched.status == LumaBackfillStatus.RUNNING
    assert fetched.event_cursor == "cursor-abc"


async def test_checkpoint_save_is_an_upsert(checkpoint_store):
    await checkpoint_store.save(LumaBackfillCheckpoint(status=LumaBackfillStatus.RUNNING, event_cursor="cursor-1"))
    await checkpoint_store.save(LumaBackfillCheckpoint(status=LumaBackfillStatus.COMPLETED, event_cursor="cursor-2"))
    fetched = await checkpoint_store.get()
    assert fetched.status == LumaBackfillStatus.COMPLETED
    assert fetched.event_cursor == "cursor-2"
