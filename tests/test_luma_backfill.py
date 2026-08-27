"""
LumaSyncService.run_backfill() -- exercised against a fake LumaClient
(duck-typed, matching FakeClaudeClient's precedent in
tests/test_astro_ai_service.py) so pagination, resumability, and
partial-failure isolation are all testable without any real network call.
Every guest still goes through the REAL process_guest_event() path (same
one the webhook uses) -- only the Luma HTTP layer is faked.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaBackfillCheckpoint, LumaBackfillStatus
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncService
from tests.test_luma_sync_service import make_event, make_guest

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


class FakeLumaClient:
    """event_pages: list of {"entries", "has_more", "next_cursor"} keyed by
    integer page index (cursor="1" means "give me page 1"). guest_pages:
    {event_id: [pages...]}, same shape."""

    def __init__(self, event_pages: list[dict], guest_pages: dict[str, list[dict]]):
        self.event_pages = event_pages
        self.guest_pages = guest_pages
        self.list_calendar_events_calls: list[str | None] = []
        self.list_event_guests_calls: list[tuple[str, str | None]] = []

    async def list_calendar_events(self, cursor: str | None = None, limit: int = 50, status: str = "approved") -> dict:
        self.list_calendar_events_calls.append(cursor)
        index = 0 if cursor is None else int(cursor)
        return self.event_pages[index]

    async def list_event_guests(self, event_id: str, cursor: str | None = None, limit: int = 50) -> dict:
        self.list_event_guests_calls.append((event_id, cursor))
        index = 0 if cursor is None else int(cursor)
        return self.guest_pages[event_id][index]


@pytest_asyncio.fixture
async def crm_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="investor_type", label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT, options=["Angel Investor"], active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    return CrmService(custom_field_store=custom_field_store)


def _guest_entry(guest_id: str, email: str) -> dict:
    """CONFIRMED against the real production GET /v1/events/guests/list
    response (2026-08-27): each entry IS the guest object directly, not
    nested under a "guest" key."""
    return make_guest(guest_id=guest_id, email=email)


def _event_entry(event_id: str) -> dict:
    """CONFIRMED against the real production GET /v1/calendars/events/list
    response (2026-08-27): each entry IS the event object directly
    (alongside sibling tags/submitted_by keys), not nested under an
    "event" key as originally assumed from documentation alone."""
    return make_event(event_id=event_id)


def build_service(crm_service, luma_client, checkpoint_store=None):
    return LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=checkpoint_store or MemoryLumaBackfillCheckpointStore(),
        luma_client=luma_client,
    )


async def test_backfill_processes_all_events_and_guests_across_multiple_pages(crm_service):
    event_pages = [
        {"entries": [_event_entry("evt-1")], "has_more": True, "next_cursor": "1"},
        {"entries": [_event_entry("evt-2")], "has_more": False, "next_cursor": None},
    ]
    guest_pages = {
        "evt-1": [{"entries": [_guest_entry("gst-1", "a@example.com"), _guest_entry("gst-2", "b@example.com")], "has_more": False, "next_cursor": None}],
        "evt-2": [{"entries": [_guest_entry("gst-3", "c@example.com")], "has_more": False, "next_cursor": None}],
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    checkpoint = await service.run_backfill(resume=False)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED
    assert checkpoint.counts.events_processed == 2
    assert checkpoint.counts.registrations_created == 3
    assert checkpoint.counts.contacts_created == 3
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 3


async def test_backfill_is_safe_to_rerun_no_duplicates(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {"evt-1": [{"entries": [_guest_entry("gst-1", "a@example.com")], "has_more": False, "next_cursor": None}]}
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_backfill(resume=False)
    second_checkpoint = await service.run_backfill(resume=False)  # forced fresh rerun

    assert second_checkpoint.status == LumaBackfillStatus.COMPLETED
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1  # still just one -- no duplicate contact
    all_registrations = await service.registration_store.list()
    assert len(all_registrations) == 1  # still just one -- no duplicate registration


async def test_backfill_isolates_a_single_bad_guest_and_keeps_going(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    bad_guest = {"guest": {}}  # missing "id" -- process_guest_event raises LumaSyncError for this
    guest_pages = {
        "evt-1": [
            {
                "entries": [_guest_entry("gst-1", "a@example.com"), bad_guest, _guest_entry("gst-2", "b@example.com")],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    checkpoint = await service.run_backfill(resume=False)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED  # the whole run still finishes
    assert checkpoint.counts.errors == 1
    assert checkpoint.counts.registrations_created == 2  # the two good guests still processed
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2


async def test_backfill_resumes_from_a_mid_event_checkpoint_without_refetching_completed_pages(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {"entries": [_guest_entry("gst-1", "a@example.com"), _guest_entry("gst-2", "b@example.com")], "has_more": True, "next_cursor": "1"},
            {"entries": [_guest_entry("gst-3", "c@example.com"), _guest_entry("gst-4", "d@example.com")], "has_more": True, "next_cursor": "2"},
            {"entries": [_guest_entry("gst-5", "e@example.com")], "has_more": False, "next_cursor": None},
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    checkpoint_store = MemoryLumaBackfillCheckpointStore()
    # Simulate a prior run that crashed after finishing page 0 (gst-1, gst-2)
    # of evt-1's guests, mid-event.
    await checkpoint_store.save(
        LumaBackfillCheckpoint(
            status=LumaBackfillStatus.FAILED,
            event_cursor=None,
            in_progress_event_id="evt-1",
            in_progress_guest_cursor="1",
            started_at=_now(),
            updated_at=_now(),
        )
    )
    service = build_service(crm_service, client, checkpoint_store=checkpoint_store)

    checkpoint = await service.run_backfill(resume=True)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED
    # Page 0 (cursor=None) of evt-1's guests must never be refetched on resume.
    guest_calls_for_evt1 = [c for c in client.list_event_guests_calls if c[0] == "evt-1"]
    assert (("evt-1", None)) not in guest_calls_for_evt1
    assert guest_calls_for_evt1[0] == ("evt-1", "1")
    # Only page 1 (gst-3, gst-4) and page 2 (gst-5) get processed in this run.
    assert checkpoint.counts.registrations_created == 3


async def test_backfill_marks_itself_running_then_completed(crm_service):
    event_pages = [{"entries": [], "has_more": False, "next_cursor": None}]
    client = FakeLumaClient(event_pages, {})
    service = build_service(crm_service, client)

    checkpoint = await service.run_backfill(resume=False)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED
    assert checkpoint.started_at is not None
    assert checkpoint.completed_at is not None


async def test_backfill_records_exactly_one_sync_completed_activity_event(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {"evt-1": [{"entries": [_guest_entry("gst-1", "a@example.com")], "has_more": False, "next_cursor": None}]}
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_backfill(resume=False)

    from app.models.activity import ActivityCategory

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    completed_events = [e for e in page.items if e.event_type == "luma.sync.completed"]
    assert len(completed_events) == 1
    # Never per-registration detail -- aggregate counts only.
    assert "email" not in str(completed_events[0].metadata).lower()


async def test_backfill_without_luma_client_raises(crm_service):
    from app.services.luma_sync_service import LumaSyncError

    service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(),
        luma_client=None,
    )
    with pytest.raises(LumaSyncError):
        await service.run_backfill()
