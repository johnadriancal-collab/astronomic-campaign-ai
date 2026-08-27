"""
LumaSyncService.run_event_backfill() -- the targeted, single-event backfill
added for the Hot Shot Investor Dinner ATX use case. Exercised against the
same FakeLumaClient duck-type as test_luma_backfill.py, so pagination,
event-scoping, approval-status filtering, and partial-failure isolation are
all testable without a real network call. Every guest still goes through
the REAL process_guest_event() path (same one the webhook and the
full-calendar backfill both use) -- only the Luma HTTP layer is faked.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaApprovalStatus, LumaQuestionMapping
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncError, LumaSyncService
from tests.test_luma_backfill import FakeLumaClient, _event_entry, _guest_entry, build_service

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def _guest(guest_id: str, email: str, approval_status: str = "approved", **overrides) -> dict:
    entry = _guest_entry(guest_id, email)
    entry["approval_status"] = approval_status
    entry.update(overrides)
    return entry


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
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="check_size_personal", label="Check Size (Personal)",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k"], active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="deploying_capital", label="Deploying Capital",
            field_type=CustomFieldType.SINGLE_SELECT, options=["Yes, actively", "Not at the moment"], active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    return CrmService(custom_field_store=custom_field_store)


async def test_only_the_target_events_guests_are_fetched(crm_service):
    """Two events on the calendar -- run_event_backfill for evt-1 must
    never call list_event_guests for evt-2 at all (not "processes them but
    discards the result" -- never even fetches them)."""
    event_pages = [{"entries": [_event_entry("evt-1"), _event_entry("evt-2")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "a@example.com")], "has_more": False, "next_cursor": None}],
        "evt-2": [{"entries": [_guest("gst-2", "b@example.com")], "has_more": False, "next_cursor": None}],
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1")

    assert result.event_id == "evt-1"
    assert result.guests_seen == 1
    fetched_event_ids = {c[0] for c in client.list_event_guests_calls}
    assert fetched_event_ids == {"evt-1"}
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1
    assert all_contacts[0].email == "a@example.com"


async def test_wrong_event_contact_is_never_created(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1"), _event_entry("evt-2")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "a@example.com")], "has_more": False, "next_cursor": None}],
        "evt-2": [{"entries": [_guest("gst-2", "b@example.com")], "has_more": False, "next_cursor": None}],
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1")

    all_emails = {c.email for c in await crm_service.contact_store.list()}
    assert all_emails == {"a@example.com"}


async def test_target_event_found_on_a_later_calendar_page_without_touching_earlier_events_guests(crm_service):
    """_find_calendar_event pages through the calendar looking for a match
    -- must stop as soon as it's found, and must never fetch an earlier
    (non-matching) event's guest list along the way."""
    event_pages = [
        {"entries": [_event_entry("evt-other")], "has_more": True, "next_cursor": "1"},
        {"entries": [_event_entry("evt-target")], "has_more": False, "next_cursor": None},
    ]
    guest_pages = {
        "evt-other": [{"entries": [_guest("gst-skip", "skip@example.com")], "has_more": False, "next_cursor": None}],
        "evt-target": [{"entries": [_guest("gst-1", "a@example.com")], "has_more": False, "next_cursor": None}],
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-target")

    assert result.guests_seen == 1
    assert ("evt-other", None) not in client.list_event_guests_calls
    all_emails = {c.email for c in await crm_service.contact_store.list()}
    assert all_emails == {"a@example.com"}


async def test_no_full_calendar_traversal_when_event_id_supplied(crm_service):
    """Sanity check spanning several events -- only the target event's
    guest list is ever fetched, regardless of how many other events exist
    on the calendar."""
    event_pages = [
        {
            "entries": [_event_entry("evt-a"), _event_entry("evt-b"), _event_entry("evt-target"), _event_entry("evt-c")],
            "has_more": False,
            "next_cursor": None,
        }
    ]
    guest_pages = {
        "evt-a": [{"entries": [_guest("gst-a", "a@example.com")], "has_more": False, "next_cursor": None}],
        "evt-b": [{"entries": [_guest("gst-b", "b@example.com")], "has_more": False, "next_cursor": None}],
        "evt-target": [{"entries": [_guest("gst-t", "t@example.com")], "has_more": False, "next_cursor": None}],
        "evt-c": [{"entries": [_guest("gst-c", "c@example.com")], "has_more": False, "next_cursor": None}],
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-target")

    fetched_event_ids = {c[0] for c in client.list_event_guests_calls}
    assert fetched_event_ids == {"evt-target"}


async def test_approval_status_filter_processes_only_matching_guests(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {
                "entries": [
                    _guest("gst-1", "approved@example.com", approval_status="approved"),
                    _guest("gst-2", "invited@example.com", approval_status="invited"),
                    _guest("gst-3", "declined@example.com", approval_status="declined"),
                ],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.guests_seen == 3
    assert result.guests_matching_filter == 1
    assert result.counts.registrations_created == 1
    all_emails = {c.email for c in await crm_service.contact_store.list()}
    assert all_emails == {"approved@example.com"}


async def test_invited_guests_are_excluded_no_contact_or_registration(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "invited@example.com", approval_status="invited")], "has_more": False, "next_cursor": None}]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.guests_matching_filter == 0
    assert result.counts.registrations_created == 0
    assert await crm_service.contact_store.list() == []
    assert await service.registration_store.list() == []


async def test_declined_guests_are_excluded_no_contact_or_registration(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "declined@example.com", approval_status="declined")], "has_more": False, "next_cursor": None}]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.guests_matching_filter == 0
    assert result.counts.registrations_created == 0
    assert await crm_service.contact_store.list() == []


async def test_no_filter_processes_every_guest_regardless_of_status(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {
                "entries": [
                    _guest("gst-1", "a@example.com", approval_status="approved"),
                    _guest("gst-2", "b@example.com", approval_status="invited"),
                ],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1")  # approval_status omitted entirely

    assert result.guests_matching_filter == 2
    assert result.counts.registrations_created == 2


async def test_already_ingested_guest_is_updated_in_place_not_duplicated(crm_service):
    """Mirrors the real Olga Bondareva case: a guest already ingested via
    the live webhook before the event-scoped backfill runs must be
    enriched in place (via the same EXISTING-match path), never
    duplicated."""
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {"evt-1": [{"entries": [_guest("gst-1", "olga@example.com")], "has_more": False, "next_cursor": None}]}
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    # Simulate the guest already having arrived once via the live webhook.
    await service.process_guest_event(_event_entry("evt-1"), _guest("gst-1", "olga@example.com"), webhook_delivery_id="wh-1")
    assert len(await crm_service.contact_store.list()) == 1
    assert len(await service.registration_store.list()) == 1

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.counts.registrations_created == 0
    assert result.counts.registrations_updated == 1
    all_contacts = await crm_service.contact_store.list()
    all_registrations = await service.registration_store.list()
    assert len(all_contacts) == 1  # no duplicate contact
    assert len(all_registrations) == 1  # no duplicate registration


async def test_rerun_is_safe_no_duplicate_contacts_or_registrations(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "a@example.com"), _guest("gst-2", "b@example.com")], "has_more": False, "next_cursor": None}]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)
    second = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert second.counts.registrations_created == 0
    all_contacts = await crm_service.contact_store.list()
    all_registrations = await service.registration_store.list()
    assert len(all_contacts) == 2
    assert len(all_registrations) == 2
    # Rerun with unchanged data produces no new enrichment/creation events.
    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    created_events = [e for e in page.items if e.event_type == "luma.contact.created"]
    assert len(created_events) == 2  # one per contact, not duplicated by the rerun


async def test_pagination_across_multiple_guest_pages(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {"entries": [_guest("gst-1", "a@example.com"), _guest("gst-2", "b@example.com")], "has_more": True, "next_cursor": "1"},
            {"entries": [_guest("gst-3", "c@example.com")], "has_more": False, "next_cursor": None},
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.guests_seen == 3
    assert result.counts.registrations_created == 3
    guest_calls = [c for c in client.list_event_guests_calls if c[0] == "evt-1"]
    assert guest_calls == [("evt-1", None), ("evt-1", "1")]


async def test_partial_guest_failure_does_not_abort_the_rest_of_the_event(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    bad_guest = {"approval_status": "approved"}  # missing "id" -- process_guest_event raises for this
    guest_pages = {
        "evt-1": [
            {
                "entries": [_guest("gst-1", "a@example.com"), bad_guest, _guest("gst-2", "b@example.com")],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    result = await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    assert result.counts.errors == 1
    assert result.counts.registrations_created == 2  # the two good guests still processed
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2


async def test_mappings_and_translation_normalizers_are_applied(crm_service):
    """Proves the full existing mapping/normalizer/enrichment pipeline is
    reused unchanged -- not reimplemented -- for an event-scoped guest."""
    mapping_store = MemoryLumaQuestionMappingStore()
    await mapping_store.create(
        LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()), question_label="Check Size", question_type="dropdown",
            target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
            active=True, created_at=_now(), updated_at=_now(),
        )
    )
    await mapping_store.create(
        LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()), question_label="Deploying Capital", question_type="dropdown",
            target_field_key="custom:deploying_capital", active=True, created_at=_now(), updated_at=_now(),
        )
    )
    await mapping_store.create(
        LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()), question_label="LinkedIn Profile",
            target_field_key="linkedin_url", normalizer="linkedin_url", active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest = _guest(
        "gst-1", "a@example.com",
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "Under $25K"},
            {"label": "Deploying Capital", "question_id": "q-2", "question_type": "dropdown", "value": "Yes, actively"},
            {"label": "LinkedIn Profile", "question_id": "q-3", "question_type": "linkedin", "value": "/in/alice"},
        ],
    )
    guest_pages = {"evt-1": [{"entries": [guest], "has_more": False, "next_cursor": None}]}
    client = FakeLumaClient(event_pages, guest_pages)
    service = LumaSyncService(
        crm_service=crm_service, event_store=MemoryLumaEventStore(), registration_store=MemoryLumaRegistrationStore(),
        mapping_store=mapping_store, activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(), luma_client=client,
    )

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    contact = (await crm_service.contact_store.list())[0]
    assert contact.custom_fields["check_size_personal"] == ["$1k - $10k", "$10k - $25k"]
    assert contact.custom_fields["deploying_capital"] == "Yes, actively"
    assert contact.linkedin_url == "https://www.linkedin.com/in/alice"
    # Raw answers preserved exactly, untouched by translation.
    registration = (await service.registration_store.list())[0]
    raw_check_size = next(a.value for a in registration.registration_answers if a.label == "Check Size")
    assert raw_check_size == "Under $25K"


async def test_identity_conflict_protection_is_preserved(crm_service):
    """A guest whose LinkedIn belongs to a DIFFERENT existing contact must
    still be safely suppressed during event-scoped backfill, exactly as
    it is for the live webhook -- proves this path isn't bypassing
    _enrich_existing_contact's conflict detection."""
    mapping_store = MemoryLumaQuestionMappingStore()
    await mapping_store.create(
        LumaQuestionMapping(
            luma_question_mapping_id=str(uuid.uuid4()), question_label="LinkedIn Profile",
            target_field_key="linkedin_url", normalizer="linkedin_url", active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    other_contact = CrmContact(
        crm_contact_id=str(uuid.uuid4()), email="other@example.com",
        linkedin_url="https://www.linkedin.com/in/shared-handle",
        created_at=_now(), updated_at=_now(),
    )
    await crm_service.contact_store.create(other_contact)
    existing_contact = CrmContact(
        crm_contact_id=str(uuid.uuid4()), email="a@example.com", created_at=_now(), updated_at=_now(),
    )
    await crm_service.contact_store.create(existing_contact)

    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest = _guest(
        "gst-1", "a@example.com",
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "/in/shared-handle"},
        ],
    )
    guest_pages = {"evt-1": [{"entries": [guest], "has_more": False, "next_cursor": None}]}
    client = FakeLumaClient(event_pages, guest_pages)
    service = LumaSyncService(
        crm_service=crm_service, event_store=MemoryLumaEventStore(), registration_store=MemoryLumaRegistrationStore(),
        mapping_store=mapping_store, activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(), luma_client=client,
    )

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    refetched = await crm_service.contact_store.get(existing_contact.crm_contact_id)
    assert refetched.linkedin_url is None  # never overwritten with the conflicting value
    other_refetched = await crm_service.contact_store.get(other_contact.crm_contact_id)
    assert other_refetched.linkedin_url == "https://www.linkedin.com/in/shared-handle"  # untouched
    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    conflict_events = [e for e in page.items if e.event_type == "luma.contact.identity_conflict"]
    assert len(conflict_events) == 1


async def test_unknown_event_id_raises_a_clean_error(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    client = FakeLumaClient(event_pages, {"evt-1": []})
    service = build_service(crm_service, client)

    with pytest.raises(LumaSyncError):
        await service.run_event_backfill("evt-does-not-exist")


async def test_event_backfill_without_luma_client_raises(crm_service):
    service = LumaSyncService(
        crm_service=crm_service, event_store=MemoryLumaEventStore(), registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(), activity_log=crm_service.activity_log, luma_client=None,
    )
    with pytest.raises(LumaSyncError):
        await service.run_event_backfill("evt-1")


# --- luma.event_backfill.completed summary event ------------------------


async def test_exactly_one_summary_event_on_a_successful_run(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {
                "entries": [
                    _guest("gst-1", "approved@example.com", approval_status="approved"),
                    _guest("gst-2", "invited@example.com", approval_status="invited"),
                    _guest("gst-3", "declined@example.com", approval_status="declined"),
                ],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    completed_events = [e for e in page.items if e.event_type == "luma.event_backfill.completed"]
    assert len(completed_events) == 1  # once per invocation, never once per guest


async def test_summary_event_has_correct_aggregate_counts(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {
                "entries": [
                    _guest("gst-1", "approved1@example.com", approval_status="approved"),
                    _guest("gst-2", "approved2@example.com", approval_status="approved"),
                    _guest("gst-3", "invited@example.com", approval_status="invited"),
                    _guest("gst-4", "declined@example.com", approval_status="declined"),
                ],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    event = next(e for e in page.items if e.event_type == "luma.event_backfill.completed")
    assert event.metadata["event_id"] == "evt-1"
    assert event.metadata["approval_status_filter"] == "approved"
    assert event.metadata["guests_seen"] == 4
    assert event.metadata["guests_matching_filter"] == 2  # invited + declined excluded
    assert event.metadata["registrations_created"] == 2
    assert event.metadata["registrations_updated"] == 0
    assert event.metadata["contacts_created"] == 2
    assert event.metadata["contacts_enriched"] == 0
    assert event.metadata["needs_review"] == 0
    assert event.metadata["errors"] == 0


async def test_summary_metadata_contains_no_guest_or_contact_pii(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [
            {
                "entries": [
                    _guest("gst-1", "secret-approved@example.com", approval_status="approved", user_name="Approved Guest"),
                    _guest("gst-2", "secret-invited@example.com", approval_status="invited", user_name="Invited Guest"),
                    _guest("gst-3", "secret-declined@example.com", approval_status="declined", user_name="Declined Guest"),
                ],
                "has_more": False,
                "next_cursor": None,
            }
        ]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    event = next(e for e in page.items if e.event_type == "luma.event_backfill.completed")
    metadata_text = str(event.metadata).lower()
    for leaked in ["secret-approved@example.com", "secret-invited@example.com", "secret-declined@example.com",
                   "approved guest", "invited guest", "declined guest", "gst-1", "gst-2", "gst-3"]:
        assert leaked.lower() not in metadata_text
    # Only the whitelisted aggregate keys are present -- no guest-level list.
    assert set(event.metadata.keys()) == {
        "event_id", "event_name", "approval_status_filter", "guests_seen", "guests_matching_filter",
        "registrations_created", "registrations_updated", "contacts_created", "contacts_enriched",
        "needs_review", "errors",
    }


async def test_rerun_produces_a_second_summary_event_but_no_duplicate_data(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    guest_pages = {
        "evt-1": [{"entries": [_guest("gst-1", "a@example.com", approval_status="approved")], "has_more": False, "next_cursor": None}]
    }
    client = FakeLumaClient(event_pages, guest_pages)
    service = build_service(crm_service, client)

    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)
    await service.run_event_backfill("evt-1", approval_status=LumaApprovalStatus.APPROVED)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    completed_events = [e for e in page.items if e.event_type == "luma.event_backfill.completed"]
    assert len(completed_events) == 2  # one summary event per invocation
    assert len(await crm_service.contact_store.list()) == 1  # still no duplicate contact
    assert len(await service.registration_store.list()) == 1  # still no duplicate registration


async def test_no_summary_event_when_the_run_fails_before_completion(crm_service):
    event_pages = [{"entries": [_event_entry("evt-1")], "has_more": False, "next_cursor": None}]
    client = FakeLumaClient(event_pages, {"evt-1": []})
    service = build_service(crm_service, client)

    with pytest.raises(LumaSyncError):
        await service.run_event_backfill("evt-does-not-exist")

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    completed_events = [e for e in page.items if e.event_type == "luma.event_backfill.completed"]
    assert completed_events == []  # a failed lookup must never emit a misleading "completed" event
