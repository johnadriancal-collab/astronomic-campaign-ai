"""
Regression fixtures for Luma's REAL production response shapes -- both
list endpoints return entries FLAT (the object's own fields directly on
the entry, alongside sibling metadata keys like `tags`/`submitted_by`),
NOT nested under a type-name key ("event"/"guest") as originally assumed
from documentation alone. Confirmed by direct live API calls against
Astronomic's production Luma calendar on 2026-08-27 -- these fixtures are
shaped exactly like what was actually observed (field set and nesting;
values are fabricated, no real PII), so a future accidental reversion to
the wrong nested-wrapper assumption is caught immediately.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaBackfillStatus
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


# Real shape (fabricated values) of one GET /v1/calendars/events/list entry --
# the event's own fields directly, `tags`/`submitted_by` as siblings, NOT
# nested under "event".
REAL_SHAPED_EVENT_ENTRY = {
    "platform": "luma",
    "id": "evt-ZWviVlhfy5Wstc9",
    "user_id": "usr-mphJrFs4zb1TBFg",
    "calendar_id": "cal-sUMK7QRQl7rdBab",
    "start_at": "2026-09-17T23:00:00.000Z",
    "duration_interval": "P0Y0M0DT3H0M0S",
    "end_at": "2026-09-18T02:00:00.000Z",
    "created_at": "2026-08-06T10:31:46.593Z",
    "timezone": "America/Chicago",
    "name": "Hot Shot Investor Dinner ATX",
    "geo_address_json": {"address": "Austin", "city": "Austin", "region": "Texas", "country": "US"},
    "coordinate": {"longitude": -97.7430608, "latitude": 30.267152999999997},
    "meeting_url": None,
    "location_type": "offline",
    "location_visibility": "guests-only",
    "cover_url": "https://images.lumacdn.com/uploads/example.png",
    "registration_questions": [
        {"id": "8yheuwry", "label": "What company do you work for?", "required": True, "question_type": "company", "collect_job_title": True, "job_title_label": "What is your job title?"},
        {"id": "h3t80aca", "label": "What is your LinkedIn profile?", "required": True, "question_type": "linkedin"},
    ],
    "url": "https://lu.ma/example",
    "visibility": "public",
    "waitlist_status": None,
    "registration_open": True,
    "require_approval": True,
    "max_capacity": None,
    "can_register_for_multiple_tickets": False,
    "spots_remaining": None,
    "display_price": None,
    "feedback_email": None,
    "access": "manage",
    "tags": [],
    "submitted_by": None,
}

# Real shape (fabricated values) of one GET /v1/events/guests/list entry --
# the guest's own fields directly, NOT nested under "guest".
REAL_SHAPED_GUEST_ENTRY = {
    "id": "gst-5YfBKHqIo3pd1cG",
    "user_id": "usr-example",
    "user_email": "example@example.com",
    "user_name": "Example Person",
    "user_first_name": "Example",
    "user_last_name": "Person",
    "approval_status": "pending_approval",
    "check_in_qr_code": "https://lu.ma/check-in/example",
    "eth_address": None,
    "invited_at": None,
    "joined_at": None,
    "phone_number": "+15550000000",
    "registered_at": "2026-08-27T08:32:21.663Z",
    "registration_answers": [
        {"label": "What company do you work for?", "question_id": "8yheuwry", "value": {"company": "Astronomic", "job_title": "Investor"}, "question_type": "company", "answer": "Astronomic", "answer_company": "Astronomic", "answer_job_title": "Investor"},
        {"label": "What is your LinkedIn profile?", "question_id": "h3t80aca", "value": "/in/example-person", "question_type": "linkedin", "answer": "/in/example-person"},
    ],
    "solana_address": None,
    "utm_source": None,
    "event_tickets": [
        {"id": "tkt-example", "amount": 0, "amount_discount": 0, "amount_tax": 0, "currency": "usd", "checked_in_at": None, "event_ticket_type_id": "ttype-example", "is_captured": False, "name": "Standard"}
    ],
}


class _FakeLumaClient:
    def __init__(self, event_entries: list[dict], guest_entries: list[dict]):
        self._event_entries = event_entries
        self._guest_entries = guest_entries

    async def list_calendar_events(self, cursor=None, limit=50, status="approved"):
        return {"entries": self._event_entries, "has_more": False, "next_cursor": None}

    async def list_event_guests(self, event_id, cursor=None, limit=50):
        return {"entries": self._guest_entries, "has_more": False, "next_cursor": None}


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


async def test_real_shaped_event_entry_is_parsed_correctly(crm_service):
    """Direct process_guest_event() call using the real (fabricated-value)
    event shape -- proves _parse_event() correctly reads a FLAT entry."""
    service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
    )
    result = await service.process_guest_event(REAL_SHAPED_EVENT_ENTRY, REAL_SHAPED_GUEST_ENTRY)

    stored_event = await service.event_store.get("evt-ZWviVlhfy5Wstc9")
    assert stored_event is not None
    assert stored_event.name == "Hot Shot Investor Dinner ATX"
    assert stored_event.calendar_id == "cal-sUMK7QRQl7rdBab"
    assert result.registration.luma_event_id == "evt-ZWviVlhfy5Wstc9"


async def test_backfill_correctly_parses_the_real_flat_events_list_shape(crm_service):
    """End-to-end through run_backfill() using a FakeLumaClient that
    returns entries in the REAL flat shape -- this is the actual
    regression test for the bug: before the fix, `entry.get("event")`
    against a flat entry returned None, so event_id was None and every
    event was silently skipped (0 events processed)."""
    client = _FakeLumaClient([REAL_SHAPED_EVENT_ENTRY], [REAL_SHAPED_GUEST_ENTRY])
    service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(),
        luma_client=client,
    )

    checkpoint = await service.run_backfill(resume=False)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED
    assert checkpoint.counts.events_processed == 1
    assert checkpoint.counts.registrations_created == 1
    assert checkpoint.counts.contacts_created == 1

    all_events = await service.event_store.list()
    assert len(all_events) == 1
    assert all_events[0].name == "Hot Shot Investor Dinner ATX"

    all_registrations = await service.registration_store.list()
    assert len(all_registrations) == 1
    assert all_registrations[0].luma_guest_id == "gst-5YfBKHqIo3pd1cG"
    # The real registration_answers, including the extra "answer"/"answer_company"
    # convenience keys Luma also sends, are preserved (extras simply ignored).
    assert len(all_registrations[0].registration_answers) == 2


async def test_backfill_pagination_still_works_with_the_real_shape(crm_service):
    """Multi-page events + multi-page guests, all in the real flat shape."""

    class _PaginatedFakeClient:
        def __init__(self):
            self.event_calls = []
            self.guest_calls = []

        async def list_calendar_events(self, cursor=None, limit=50, status="approved"):
            self.event_calls.append(cursor)
            if cursor is None:
                return {"entries": [REAL_SHAPED_EVENT_ENTRY], "has_more": True, "next_cursor": "page-2"}
            second_event = {**REAL_SHAPED_EVENT_ENTRY, "id": "evt-second", "name": "Second Real Event"}
            return {"entries": [second_event], "has_more": False, "next_cursor": None}

        async def list_event_guests(self, event_id, cursor=None, limit=50):
            self.guest_calls.append((event_id, cursor))
            return {"entries": [REAL_SHAPED_GUEST_ENTRY], "has_more": False, "next_cursor": None}

    client = _PaginatedFakeClient()
    service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(),
        luma_client=client,
    )

    checkpoint = await service.run_backfill(resume=False)

    assert checkpoint.status == LumaBackfillStatus.COMPLETED
    assert checkpoint.counts.events_processed == 2
    all_events = await service.event_store.list()
    assert {e.luma_event_id for e in all_events} == {"evt-ZWviVlhfy5Wstc9", "evt-second"}


async def test_real_shaped_guest_entry_answers_map_correctly_with_a_configured_mapping(crm_service):
    """Confirms the flat guest shape's registration_answers (including the
    nested company dict) flow correctly through _build_mapped_fields when
    a real mapping is configured."""
    from app.models.luma import LumaQuestionMapping

    mapping_store = MemoryLumaQuestionMappingStore()
    await mapping_store.create(
        LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()), question_label="What company do you work for?",
            question_type="company", target_field_key="company", extract_key="company",
            active=True, created_at=_now(), updated_at=_now(),
        )
    )
    service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=mapping_store,
        activity_log=crm_service.activity_log,
    )

    result = await service.process_guest_event(REAL_SHAPED_EVENT_ENTRY, REAL_SHAPED_GUEST_ENTRY)

    assert result.contact.company == "Astronomic"
