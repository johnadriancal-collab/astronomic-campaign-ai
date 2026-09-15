"""
ClientCrmService's Astro AI Phase 3 (2026-09-15) additions:
find_engagements() -- the first cross-client Engagement lookup in this
codebase (title required, exact-after-normalization; engagement_date/
location/client_name optionally narrow further) -- and
get_active_participant_for_contact() -- the read half of "update the
existing EngagementParticipant rather than creating a duplicate" that
mark_crm_contact_engagement_attendance needs.
"""

from datetime import date, datetime, timezone

import pytest

from app.models.client_crm import EngagementParticipant, ParticipantSource
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
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def service():
    crm_contact_store = MemoryCrmContactStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return ClientCrmService(
        client_store=MemoryClientStore(),
        activity_log=activity_log,
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=crm_contact_store,
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=MemoryLumaEventStore(),
        client_touchpoint_store=MemoryClientTouchpointStore(),
        contact_engagement_signal_service=ContactEngagementSignalService(
            crm_contact_store=crm_contact_store, activity_log=activity_log
        ),
    )


async def _make_engagement(service, client_name="Astronomic -- Direct Events", **overrides):
    client = await service.create_client({"name": client_name})
    fields = {"title": "Austin Forward", "engagement_type": "dinner"}
    fields.update(overrides)
    engagement = await service.create_client_engagement(client.client_id, fields)
    return client, engagement


# --- find_engagements --------------------------------------------------


async def test_find_by_title_alone_resolves_a_unique_match(service):
    _client, engagement = await _make_engagement(service)
    results = await service.find_engagements(title="Austin Forward")
    assert [e.engagement_id for e in results] == [engagement.engagement_id]


async def test_find_is_case_and_whitespace_insensitive_but_exact_otherwise(service):
    _client, engagement = await _make_engagement(service)
    results = await service.find_engagements(title="  austin   forward ")
    assert [e.engagement_id for e in results] == [engagement.engagement_id]


async def test_find_does_not_substring_match(service):
    await _make_engagement(service, title="Austin Forward")
    assert await service.find_engagements(title="Austin") == []
    assert await service.find_engagements(title="Austin Forward Dinner") == []


async def test_find_across_multiple_clients(service):
    """The whole point of this method -- a title match works regardless
    of which Client the Engagement belongs to, with no client_id given."""
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_engagement(client_a.client_id, {"title": "SF Investor Dinner", "engagement_type": "dinner"})
    client_b = await service.create_client({"name": "Acme Co"})
    engagement_b = await service.create_client_engagement(client_b.client_id, {"title": "Acme Kickoff", "engagement_type": "other"})

    results = await service.find_engagements(title="Acme Kickoff")
    assert [e.engagement_id for e in results] == [engagement_b.engagement_id]


async def test_find_returns_multiple_candidates_when_title_repeats(service):
    client = await service.create_client({"name": "Astronomic -- Direct Events"})
    first = await service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 3, 1)}
    )
    second = await service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 9, 10)}
    )

    results = await service.find_engagements(title="Austin Forward")
    assert {e.engagement_id for e in results} == {first.engagement_id, second.engagement_id}


async def test_find_narrows_a_repeated_title_by_date(service):
    client = await service.create_client({"name": "Astronomic -- Direct Events"})
    await service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 3, 1)}
    )
    september = await service.create_client_engagement(
        client.client_id, {"title": "Austin Forward", "engagement_type": "dinner", "engagement_date": date(2026, 9, 10)}
    )

    results = await service.find_engagements(title="Austin Forward", engagement_date=date(2026, 9, 10))
    assert [e.engagement_id for e in results] == [september.engagement_id]


async def test_find_narrows_by_client_name(service):
    client_a = await service.create_client({"name": "Astronomic -- Direct Events"})
    engagement_a = await service.create_client_engagement(client_a.client_id, {"title": "Investor Night", "engagement_type": "other"})
    client_b = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_engagement(client_b.client_id, {"title": "Investor Night", "engagement_type": "other"})

    results = await service.find_engagements(title="Investor Night", client_name="Astronomic -- Direct Events")
    assert [e.engagement_id for e in results] == [engagement_a.engagement_id]


async def test_find_narrows_by_location(service):
    client = await service.create_client({"name": "Astronomic -- Direct Events"})
    austin = await service.create_client_engagement(
        client.client_id, {"title": "Investor Night", "engagement_type": "other", "location": "Austin, TX"}
    )
    await service.create_client_engagement(
        client.client_id, {"title": "Investor Night", "engagement_type": "other", "location": "Denver, CO"}
    )

    results = await service.find_engagements(title="Investor Night", location="austin, tx")
    assert [e.engagement_id for e in results] == [austin.engagement_id]


async def test_find_with_no_title_returns_nothing(service):
    await _make_engagement(service)
    assert await service.find_engagements(title="") == []
    assert await service.find_engagements(title=None) == []


async def test_find_no_match_returns_empty_list(service):
    await _make_engagement(service, title="Austin Forward")
    assert await service.find_engagements(title="Nonexistent Event") == []


# --- get_active_participant_for_contact ---------------------------------


async def test_get_active_participant_returns_none_when_absent(service):
    _client, engagement = await _make_engagement(service)
    assert await service.get_active_participant_for_contact(engagement.engagement_id, "contact-1") is None


async def test_get_active_participant_returns_the_existing_row(service):
    client, engagement = await _make_engagement(service)
    await service.crm_contact_store.create(
        CrmContact(crm_contact_id="contact-1", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), first_name="Jane")
    )
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "contact-1", "attendance_status": "attended"}
    )

    found = await service.get_active_participant_for_contact(engagement.engagement_id, "contact-1")
    assert found is not None
    assert found.participant_id == participant.participant_id


async def test_get_active_participant_excludes_archived_rows(service):
    client, engagement = await _make_engagement(service)
    now = datetime.now(timezone.utc)
    archived = EngagementParticipant(
        participant_id="p1",
        engagement_id=engagement.engagement_id,
        client_id=client.client_id,
        crm_contact_id="contact-1",
        first_name="Jane",
        created_at=now,
        updated_at=now,
        archived=True,
    )
    await service.engagement_participant_store.create(archived)

    assert await service.get_active_participant_for_contact(engagement.engagement_id, "contact-1") is None


# --- create/update_engagement_participant source attribution ------------


async def test_create_engagement_participant_defaults_source_to_manual(service):
    client, engagement = await _make_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Walk", "last_name": "In"}
    )
    assert participant.source.value == "manual"

    events = (await service.activity_log.store.list())
    created = next(e for e in events if e.event_type == "engagement_participant.created")
    assert created.source.value == "manual_client_crm"
    assert created.actor is None


async def test_create_engagement_participant_with_astro_ai_source(service):
    client, engagement = await _make_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        {"first_name": "Walk", "last_name": "In", "attendance_status": "attended"},
        source=ParticipantSource.ASTRO_AI,
    )
    assert participant.source.value == "astro_ai"

    events = (await service.activity_log.store.list())
    created = next(e for e in events if e.event_type == "engagement_participant.created")
    assert created.source.value == "astro_ai"
    assert created.actor == "astro_ai"
    # Exactly one event for this one mutation.
    assert len([e for e in events if e.event_type == "engagement_participant.created"]) == 1


async def test_update_engagement_participant_with_astro_ai_source(service):
    client, engagement = await _make_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Walk", "last_name": "In"}
    )
    updated = await service.update_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        participant.participant_id,
        {"attendance_status": "attended"},
        source=ParticipantSource.ASTRO_AI,
    )
    assert updated.source.value == "astro_ai"

    events = (await service.activity_log.store.list())
    update_events = [e for e in events if e.event_type == "engagement_participant.updated"]
    assert len(update_events) == 1
    assert update_events[0].source.value == "astro_ai"
    assert update_events[0].actor == "astro_ai"


async def test_update_engagement_participant_without_source_leaves_it_unchanged(service):
    client, engagement = await _make_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Walk", "last_name": "In"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"rsvp_status": "confirmed"}
    )
    assert updated.source.value == "manual"
