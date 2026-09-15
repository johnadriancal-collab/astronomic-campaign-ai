"""
AstroClientCrmTools tests -- Astro AI Phase 3's Client CRM read
(get_crm_contact_event_history) and one pending-action-gated write
(mark_crm_contact_engagement_attendance) tools.

Exercised against REAL CrmService + ClientCrmService instances (in-memory
stores), same "prove the actual path, not a mock of it" convention
test_astro_crm_tools.py already established.
"""

import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio

from app.models.client_crm import ParticipantAttendanceStatus, ParticipantRole, ParticipantRsvpStatus, ParticipantSource
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_client_crm_tools import AstroClientCrmTools
from app.services.astro_pending_action_store import AstroPendingActionStore
from app.services.astro_crm_tools import AstroCrmTools
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.crm_service import CrmService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 9, 15, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def crm_service():
    service = CrmService()
    await service.contact_store.create(
        CrmContact(
            crm_contact_id="john-1",
            created_at=_now(),
            updated_at=_now(),
            first_name="John",
            last_name="Adrian Cal",
            company="Astronomic",
        )
    )
    await service.contact_store.create(
        CrmContact(crm_contact_id="ambiguous-1", created_at=_now(), updated_at=_now(), first_name="Jane", last_name="Doe")
    )
    await service.contact_store.create(
        CrmContact(crm_contact_id="ambiguous-2", created_at=_now(), updated_at=_now(), first_name="Jane", last_name="Doe")
    )
    return service


@pytest_asyncio.fixture
async def client_crm_service(crm_service):
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return ClientCrmService(
        client_store=MemoryClientStore(),
        activity_log=activity_log,
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=crm_service.contact_store,
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=MemoryLumaEventStore(),
        client_touchpoint_store=MemoryClientTouchpointStore(),
        contact_engagement_signal_service=ContactEngagementSignalService(
            crm_contact_store=crm_service.contact_store, activity_log=activity_log
        ),
    )


@pytest.fixture
def tools(crm_service, client_crm_service):
    return AstroClientCrmTools(client_crm_service, crm_service)


class _WriteTools:
    """Test-only pairing of AstroClientCrmTools (which proposes
    mark_crm_contact_engagement_attendance) with AstroCrmTools (which owns
    confirm_astro_action) sharing ONE AstroPendingActionStore -- exactly
    how app/main.py wires them together in production (see
    astro_hub_tools.py's own docstring for why this cross-domain
    confirmation is safe). `dispatch()` routes by name across both,
    mirroring AstroHubTools' own routing at a much smaller scale."""

    def __init__(self, crm_service, client_crm_service):
        pending_action_store = AstroPendingActionStore()
        self.client_crm_service = client_crm_service
        self._client_crm_tools = AstroClientCrmTools(client_crm_service, crm_service, pending_action_store=pending_action_store)
        self._crm_tools = AstroCrmTools(crm_service, pending_action_store=pending_action_store)

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        if name == "confirm_astro_action":
            return await self._crm_tools.dispatch(name, tool_input)
        return await self._client_crm_tools.dispatch(name, tool_input)


@pytest.fixture
def write_tools(crm_service, client_crm_service):
    return _WriteTools(crm_service, client_crm_service)


async def _make_engagement(client_crm_service, title="Austin Forward", **overrides):
    client = await client_crm_service.create_client({"name": "Astronomic -- Direct Events"})
    fields = {"title": title, "engagement_type": "dinner"}
    fields.update(overrides)
    engagement = await client_crm_service.create_client_engagement(client.client_id, fields)
    return client, engagement


# --- get_crm_contact_event_history -------------------------------------


async def test_event_history_returns_structured_entries(tools, client_crm_service):
    client, engagement = await _make_engagement(client_crm_service)
    await client_crm_service.create_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        {"crm_contact_id": "john-1", "attendance_status": "attended", "role": "guest"},
    )

    result = await tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert result["status"] == "found"
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert event["event_name"] == "Austin Forward"
    assert event["attendance_status"] == "attended"
    assert event["role"] == "guest"
    assert event["rsvp_status"] is None


async def test_event_history_empty_when_no_engagements(tools):
    result = await tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert result == {"status": "found", "contact": {"name": "John Adrian Cal"}, "events": []}


async def test_event_history_distinguishes_declined_from_attended(tools, client_crm_service):
    client_a, dinner_a = await _make_engagement(client_crm_service, title="Dinner A")
    client_b, dinner_b = await _make_engagement(client_crm_service, title="Dinner B")
    await client_crm_service.create_engagement_participant(
        client_a.client_id, dinner_a.engagement_id, {"crm_contact_id": "john-1", "attendance_status": "attended"}
    )
    await client_crm_service.create_engagement_participant(
        client_b.client_id, dinner_b.engagement_id, {"crm_contact_id": "john-1", "rsvp_status": "declined"}
    )

    result = await tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    by_title = {e["event_name"]: e for e in result["events"]}
    assert by_title["Dinner A"]["attendance_status"] == "attended"
    assert by_title["Dinner A"]["rsvp_status"] is None
    assert by_title["Dinner B"]["rsvp_status"] == "declined"
    assert by_title["Dinner B"]["attendance_status"] is None


async def test_event_history_ambiguous_contact_never_guesses(tools):
    result = await tools.dispatch("get_crm_contact_event_history", {"last_name": "Doe"})
    assert result["status"] == "ambiguous"


async def test_event_history_never_reads_dinners_attended(tools, client_crm_service):
    """Structural separation (Part B/Decision 2): this tool's response
    must never surface custom_fields.dinners_attended -- that stays only
    on get_crm_contact's own projection."""
    result = await tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert "dinners_attended" not in result
    assert "dinners_attended" not in str(result.get("contact", {}))


# --- mark_crm_contact_engagement_attendance -----------------------------


async def test_mark_attendance_does_not_execute_immediately(write_tools, client_crm_service):
    await _make_engagement(write_tools.client_crm_service)
    result = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    assert result["status"] == "pending_confirmation"
    assert "pending_action_id" in result

    history = await write_tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert history["events"] == []


async def test_mark_attendance_confirmed_creates_a_new_participant(write_tools):
    await _make_engagement(write_tools.client_crm_service)
    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    confirm = await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})
    assert confirm["status"] == "confirmed"
    assert confirm["result"]["status"] == "created"
    assert confirm["result"]["attendance_status"] == "attended"
    assert confirm["result"]["role"] == "guest"

    history = await write_tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert history["events"][0]["attendance_status"] == "attended"


async def test_mark_attendance_new_participant_uses_astro_ai_source(write_tools, client_crm_service):
    await _make_engagement(write_tools.client_crm_service)
    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    result = await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})
    participant = await write_tools.client_crm_service.engagement_participant_store.get(result["result"]["participant_id"])
    assert participant.source == ParticipantSource.ASTRO_AI


async def test_mark_attendance_repeated_confirmed_command_creates_only_one_participant(write_tools):
    """Idempotency (Phase 12): the SAME propose+confirm sequence run twice
    must still end with exactly one EngagementParticipant -- the second
    run's propose step should see the existing participant and UPDATE it,
    never create a duplicate."""
    await _make_engagement(write_tools.client_crm_service)

    first_propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    await write_tools.dispatch("confirm_astro_action", {"pending_action_id": first_propose["pending_action_id"]})

    second_propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    # Already attended with no role change requested -- a true no-op, no
    # pending action needed.
    assert second_propose["status"] == "no_change"

    history = await write_tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert len(history["events"]) == 1


async def test_mark_attendance_updates_existing_declined_participant_to_attended(write_tools):
    client, engagement = await _make_engagement(write_tools.client_crm_service)
    await write_tools.client_crm_service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "john-1", "rsvp_status": "declined"}
    )

    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    assert propose["status"] == "pending_confirmation"
    assert "declined" in propose["description"].lower()
    assert "attended" in propose["description"].lower()

    confirm = await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})
    assert confirm["result"]["status"] == "updated"

    history = await write_tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    [event] = history["events"]
    assert event["attendance_status"] == "attended"


async def test_mark_attendance_does_not_alter_role_unless_specified(write_tools):
    client, engagement = await _make_engagement(write_tools.client_crm_service)
    await write_tools.client_crm_service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "john-1", "role": "sponsor", "rsvp_status": "confirmed"}
    )

    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})

    history = await write_tools.dispatch("get_crm_contact_event_history", {"first_name": "John", "last_name": "Adrian Cal"})
    assert history["events"][0]["role"] == "sponsor"


async def test_mark_attendance_role_explicitly_supplied_is_validated_and_applied(write_tools):
    await _make_engagement(write_tools.client_crm_service)
    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward", "role": "sponsor"},
    )
    confirm = await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})
    assert confirm["result"]["role"] == "sponsor"


async def test_mark_attendance_invalid_role_is_rejected(write_tools):
    result = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward", "role": "not_a_role"},
    )
    assert result["error"] == "invalid_filter"


async def test_mark_attendance_ambiguous_engagement_proposes_nothing(write_tools):
    client = await write_tools.client_crm_service.create_client({"name": "Astronomic -- Direct Events"})
    await write_tools.client_crm_service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 3, 1)}
    )
    await write_tools.client_crm_service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 9, 10)}
    )

    result = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    assert result["status"] == "engagement_ambiguous"
    assert result["total"] == 2
    assert "pending_action_id" not in result
    for candidate in result["candidates"]:
        assert set(candidate.keys()) == {"engagement_id", "title", "engagement_date", "location", "client_name"}


async def test_mark_attendance_missing_engagement_never_creates_one(write_tools, client_crm_service):
    result = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Nonexistent Gala"},
    )
    assert result == {"status": "engagement_not_found"}
    assert await client_crm_service.client_store.list() == []


async def test_mark_attendance_ambiguous_contact_proposes_nothing(write_tools):
    await _make_engagement(write_tools.client_crm_service)
    result = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance", {"last_name": "Doe", "engagement_title": "Austin Forward"}
    )
    assert result["status"] == "ambiguous"


async def test_mark_attendance_requires_pending_action_store(tools):
    result = await tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    assert result == {"error": "tool_failed", "message": "Confirmation isn't available right now -- please try again."}


async def test_mark_attendance_target_changed_between_propose_and_confirm(write_tools):
    """Optimistic-concurrency guard: if the existing Participant's status
    changed (by some OTHER path) after this was proposed, confirm must
    report target_changed rather than silently overwrite it."""
    client, engagement = await _make_engagement(write_tools.client_crm_service)
    participant = await write_tools.client_crm_service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "john-1", "rsvp_status": "declined"}
    )
    propose = await write_tools.dispatch(
        "mark_crm_contact_engagement_attendance",
        {"first_name": "John", "last_name": "Adrian Cal", "engagement_title": "Austin Forward"},
    )
    # Someone else updates the same participant before confirmation.
    await write_tools.client_crm_service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"rsvp_status": "confirmed"}
    )

    confirm = await write_tools.dispatch("confirm_astro_action", {"pending_action_id": propose["pending_action_id"]})
    assert confirm["result"]["error"] == "target_changed"
