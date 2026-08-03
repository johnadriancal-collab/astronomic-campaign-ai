"""
Tests for EmailMessageSyncService -- pagination against
/emailer_messages/search, idempotent upsert by apollo_message_id, the
"messages_last_synced_at only advances on full success" guarantee,
Lead-resolution (and skipping unmapped contacts), per-message event sync,
and local test-fixture generation (zero Apollo calls, idempotent).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.models.email_message import EmailMessageSource
from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.models.lead import CampaignLead, CampaignLeadStatus, Lead, LeadStatus
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.email_message_event_store import MemoryEmailMessageEventStore
from app.repositories.email_message_store import EmailMessageNotFoundError, MemoryEmailMessageStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.email_message_sync_service import EmailMessageSyncService


def make_sequence(email_sequence_id: str = "seq-1", campaign_id: str = "c1", apollo_sequence_id: str = "apollo-seq-1") -> EmailSequence:
    now = datetime.now(timezone.utc)
    return EmailSequence(
        email_sequence_id=email_sequence_id,
        campaign_id=campaign_id,
        apollo_sequence_id=apollo_sequence_id,
        name="Test Sequence",
        status=EmailSequenceStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )


def make_step(email_sequence_id: str = "seq-1", apollo_step_id: str = "apollo-step-1") -> EmailSequenceStep:
    return EmailSequenceStep(
        email_sequence_step_id="step-1",
        email_sequence_id=email_sequence_id,
        apollo_step_id=apollo_step_id,
        position=1,
        day=0,
        subject="Subject",
        body="Body",
    )


def make_lead(lead_id: str, apollo_contact_id: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        lead_id=lead_id,
        apollo_contact_id=apollo_contact_id,
        first_name="Test",
        last_name="Lead",
        email="test@example.com",
        title=None,
        company=None,
        company_domain=None,
        status=LeadStatus.NEW,
        created_at=now,
        updated_at=now,
        apollo_snapshot={},
    )


def make_raw_message(
    apollo_message_id: str,
    contact_id: str = "apollo-contact-1",
    status: str = "completed",
    emailer_step_id: str = "apollo-step-1",
    **overrides,
) -> dict:
    base = {
        "id": apollo_message_id,
        "contact_id": contact_id,
        "emailer_step_id": emailer_step_id,
        "emailer_touch_id": "apollo-touch-1",
        "status": status,
        "created_at": "2026-07-31T00:00:00.000Z",
        "due_at": "2026-07-31T00:05:00.000Z",
        "completed_at": "2026-07-31T00:10:00.000Z",
        "failed_at": None,
        "replied": False,
        "reply_class": None,
        "bounce": False,
        "spam_blocked": False,
        "failure_reason": None,
        "provider_message_id": "gmail-msg-1",
        "provider_thread_id": "gmail-thread-1",
    }
    base.update(overrides)
    return base


@pytest.fixture
def stores():
    return {
        "sequence_store": MemoryEmailSequenceStore(),
        "step_store": MemoryEmailSequenceStepStore(),
        "message_store": MemoryEmailMessageStore(),
        "event_store": MemoryEmailMessageEventStore(),
        "lead_store": MemoryLeadStore(),
        "campaign_lead_store": MemoryCampaignLeadStore(),
    }


def make_service(stores, apollo) -> EmailMessageSyncService:
    return EmailMessageSyncService(apollo=apollo, **stores)


async def seed_sequence_and_lead(stores, contact_id: str = "apollo-contact-1", lead_id: str = "lead-1"):
    await stores["sequence_store"].create(make_sequence())
    await stores["step_store"].create(make_step())
    await stores["lead_store"].create(make_lead(lead_id, contact_id))


@pytest.mark.asyncio
async def test_sync_messages_requires_sequence_to_exist(stores):
    service = make_service(stores, AsyncMock())
    with pytest.raises(ValueError):
        await service.sync_messages("c1")


@pytest.mark.asyncio
async def test_sync_messages_creates_and_maps_a_message(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}
    service = make_service(stores, apollo)

    sequence, messages = await service.sync_messages("c1")

    assert len(messages) == 1
    message = messages[0]
    assert message.apollo_message_id == "apollo-m1"
    assert message.lead_id == "lead-1"
    assert message.email_sequence_step_id == "step-1"
    assert message.apollo_touch_id == "apollo-touch-1"
    assert message.status == "completed"
    assert message.provider_message_id == "gmail-msg-1"
    assert message.source == EmailMessageSource.APOLLO_SYNC
    assert sequence.messages_last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_messages_paginates_until_short_page(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    page1 = {"emailer_messages": [make_raw_message(f"apollo-m{i}") for i in range(100)]}
    page2 = {"emailer_messages": [make_raw_message("apollo-m100")]}
    apollo.search_messages.side_effect = [page1, page2]
    service = make_service(stores, apollo)

    _sequence, messages = await service.sync_messages("c1")

    assert len(messages) == 101
    assert apollo.search_messages.call_count == 2


@pytest.mark.asyncio
async def test_resyncing_upserts_by_apollo_message_id_without_duplicating(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1", status="completed")]}
    service = make_service(stores, apollo)

    await service.sync_messages("c1")
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1", status="failed")]}
    _sequence, messages = await service.sync_messages("c1")

    assert len(messages) == 1
    assert messages[0].status == "failed"


@pytest.mark.asyncio
async def test_message_with_unmapped_contact_is_skipped_not_stored(stores):
    await seed_sequence_and_lead(stores, contact_id="apollo-contact-1")
    apollo = AsyncMock()
    apollo.search_messages.return_value = {
        "emailer_messages": [make_raw_message("apollo-m1", contact_id="apollo-contact-UNKNOWN")]
    }
    service = make_service(stores, apollo)

    _sequence, messages = await service.sync_messages("c1")

    assert messages == []


@pytest.mark.asyncio
async def test_failed_apollo_call_does_not_advance_messages_last_synced_at(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}
    service = make_service(stores, apollo)
    sequence, _ = await service.sync_messages("c1")
    first_synced_at = sequence.messages_last_synced_at
    assert first_synced_at is not None

    apollo.search_messages.side_effect = RuntimeError("Apollo is down")
    with pytest.raises(RuntimeError):
        await service.sync_messages("c1")

    stored = await stores["sequence_store"].get_by_campaign_id("c1")
    assert stored.messages_last_synced_at == first_synced_at


@pytest.mark.asyncio
async def test_failed_apollo_call_partway_through_pagination_persists_nothing_new(stores):
    """A failure on page 2 must not persist page 1's messages either -- the whole sweep is all-or-nothing."""
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    page1 = {"emailer_messages": [make_raw_message(f"apollo-m{i}") for i in range(100)]}
    apollo.search_messages.side_effect = [page1, RuntimeError("Apollo is down")]
    service = make_service(stores, apollo)

    with pytest.raises(RuntimeError):
        await service.sync_messages("c1")

    stored_messages = await stores["message_store"].list_for_sequence("seq-1")
    assert stored_messages == []


@pytest.mark.asyncio
async def test_sync_message_events_creates_events_grouped_by_type(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}
    service = make_service(stores, apollo)
    _sequence, messages = await service.sync_messages("c1")
    message_id = messages[0].email_message_id

    apollo.get_message_activities.return_value = {
        "activities": [
            {
                "event_group_type": "open",
                "emailer_message_events": [
                    {"id": "evt-1", "type": "open", "created_at": "2026-07-31T01:00:00.000Z", "contact_id": "apollo-contact-1"}
                ],
            },
            {
                "event_group_type": "click",
                "emailer_message_events": [
                    {"id": "evt-2", "type": "click", "created_at": "2026-07-31T01:00:05.000Z", "contact_id": "apollo-contact-1"},
                    {"id": "evt-3", "type": "click", "created_at": "2026-07-31T01:00:10.000Z", "contact_id": "apollo-contact-1"},
                ],
            },
        ]
    }

    events = await service.sync_message_events(message_id)

    assert len(events) == 3
    assert sum(1 for e in events if e.event_type == "open") == 1
    assert sum(1 for e in events if e.event_type == "click") == 2


@pytest.mark.asyncio
async def test_resyncing_events_does_not_duplicate_already_seen_events(stores):
    await seed_sequence_and_lead(stores)
    apollo = AsyncMock()
    apollo.search_messages.return_value = {"emailer_messages": [make_raw_message("apollo-m1")]}
    service = make_service(stores, apollo)
    _sequence, messages = await service.sync_messages("c1")
    message_id = messages[0].email_message_id

    apollo.get_message_activities.return_value = {
        "activities": [
            {
                "event_group_type": "open",
                "emailer_message_events": [
                    {"id": "evt-1", "type": "open", "created_at": "2026-07-31T01:00:00.000Z"}
                ],
            }
        ]
    }
    await service.sync_message_events(message_id)
    events = await service.sync_message_events(message_id)

    assert len(events) == 1


@pytest.mark.asyncio
async def test_sync_events_rejects_test_fixture_messages(stores):
    await seed_sequence_and_lead(stores)
    fixture_service = make_service(stores, AsyncMock())
    # Build a fixture directly via generate_test_fixtures to get a real fixture message id.
    await stores["campaign_lead_store"].add(
        CampaignLead(campaign_id="c1", lead_id="lead-1", status=CampaignLeadStatus.ADDED, added_at=datetime.now(timezone.utc))
    )
    fixtures = await fixture_service.generate_test_fixtures("c1")

    with pytest.raises(ValueError):
        await fixture_service.sync_message_events(fixtures[0].email_message_id)


@pytest.mark.asyncio
async def test_sync_events_404s_for_unknown_message(stores):
    service = make_service(stores, AsyncMock())
    with pytest.raises(EmailMessageNotFoundError):
        await service.sync_message_events("does-not-exist")


@pytest.mark.asyncio
async def test_generate_test_fixtures_requires_sequence_and_leads(stores):
    service = make_service(stores, AsyncMock())
    with pytest.raises(ValueError):
        await service.generate_test_fixtures("c1")

    await stores["sequence_store"].create(make_sequence())
    await stores["step_store"].create(make_step())
    with pytest.raises(ValueError):
        await service.generate_test_fixtures("c1")  # sequence exists but no CampaignLeads yet


@pytest.mark.asyncio
async def test_generate_test_fixtures_makes_zero_apollo_calls(stores):
    await stores["sequence_store"].create(make_sequence())
    await stores["step_store"].create(make_step())
    await stores["campaign_lead_store"].add(
        CampaignLead(campaign_id="c1", lead_id="lead-1", status=CampaignLeadStatus.ADDED, added_at=datetime.now(timezone.utc))
    )
    apollo = AsyncMock()
    service = make_service(stores, apollo)

    fixtures = await service.generate_test_fixtures("c1")

    assert len(fixtures) == 1
    assert fixtures[0].source == EmailMessageSource.TEST_FIXTURE
    assert fixtures[0].apollo_message_id is None
    assert fixtures[0].last_synced_at is None
    apollo.search_messages.assert_not_called()
    apollo.get_message_activities.assert_not_called()


@pytest.mark.asyncio
async def test_generate_test_fixtures_is_idempotent(stores):
    await stores["sequence_store"].create(make_sequence())
    await stores["step_store"].create(make_step())
    await stores["campaign_lead_store"].add(
        CampaignLead(campaign_id="c1", lead_id="lead-1", status=CampaignLeadStatus.ADDED, added_at=datetime.now(timezone.utc))
    )
    service = make_service(stores, AsyncMock())

    first = await service.generate_test_fixtures("c1")
    second = await service.generate_test_fixtures("c1")

    assert {m.email_message_id for m in first} == {m.email_message_id for m in second}


@pytest.mark.asyncio
async def test_list_for_campaign_returns_empty_before_any_sequence(stores):
    service = make_service(stores, AsyncMock())
    assert await service.list_for_campaign("c1") == []
