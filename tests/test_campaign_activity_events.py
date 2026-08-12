"""
Activity Log tests for the campaign lifecycle: campaign.created (preview),
campaign.build_completed/build_failed (build), campaign.activated,
campaign.paused, and campaign.sync_completed (CampaignSyncService.sync()).
Only events for lifecycle actions that actually exist -- no invented
update/delete/archive campaign events (there is no PATCH/DELETE for
Campaign anywhere in this app).
"""

from unittest.mock import AsyncMock

import pytest

from app.models.activity import ActivityCategory
from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.lead_service import LeadService


def make_prospect(person_id: str, **overrides) -> dict:
    base = {
        "id": person_id,
        "first_name": "Jamie",
        "last_name_obfuscated": "Sm***h",
        "title": "VP of Operations",
        "has_email": False,
        "organization": {"name": "Acme Logistics"},
    }
    base.update(overrides)
    return base


def make_searched_campaign(campaign_id: str, prospects: list[dict]) -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt=f"prompt for {campaign_id}",
        created_at="2026-07-31T00:00:00Z",
        status=CampaignStatus.SEARCHED,
        plan=CampaignPlan(campaign_name="Austin Investors", filters=Filters(), sequence=[]),
        selected_prospects=prospects,
    )


def make_mock_apollo(**overrides) -> AsyncMock:
    apollo = AsyncMock()
    apollo.create_list.return_value = {"label": {"id": "list-1"}}
    apollo.create_sequence.return_value = {"emailer_campaign": {"id": "seq-1"}}
    apollo.add_sequence_steps.return_value = {}
    apollo.list_email_accounts.return_value = {"email_accounts": [{"id": "mailbox-1"}]}
    apollo.enroll_contacts.return_value = {}
    apollo.create_contact.return_value = {"contact": {"id": "contact-1"}}
    apollo.activate_sequence.return_value = {}
    apollo.deactivate_sequence.return_value = {}
    for key, value in overrides.items():
        setattr(getattr(apollo, key), "side_effect" if callable(value) else "return_value", value)
    return apollo


@pytest.fixture
def service_factory():
    def make(apollo) -> CampaignService:
        campaign_store = MemoryCampaignStore()
        lead_store = MemoryLeadStore()
        campaign_lead_store = MemoryCampaignLeadStore()
        lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)
        return CampaignService(
            apollo=apollo, store=campaign_store, lead_service=lead_service, campaign_lead_store=campaign_lead_store
        ), campaign_store

    return make


@pytest.mark.asyncio
async def test_preview_emits_campaign_created_event(service_factory):
    fake_agent = AsyncMock()
    fake_agent.generate_campaign_plan.return_value = CampaignPlan(
        campaign_name="Austin Investors", filters=Filters(), sequence=[]
    )
    service, _store = service_factory(make_mock_apollo())
    service.agent = fake_agent

    campaign = await service.preview("Find investors in Austin")

    events = await service.activity_log.store.list()
    assert len(events) == 1
    assert events[0].event_type == "campaign.created"
    assert events[0].entity_id == campaign.campaign_id
    assert events[0].entity_name == "Austin Investors"
    assert events[0].category == ActivityCategory.CAMPAIGNS


@pytest.mark.asyncio
async def test_successful_build_emits_build_completed(service_factory):
    apollo = make_mock_apollo()
    service, store = service_factory(apollo)
    await store.create(make_searched_campaign("c1", [make_prospect("p1")]))

    await service.build("c1")

    events = await service.activity_log.store.list()
    build_events = [e for e in events if e.event_type.startswith("campaign.build_")]
    assert len(build_events) == 1
    assert build_events[0].event_type == "campaign.build_completed"
    assert build_events[0].metadata["contacts_created"] == 1


@pytest.mark.asyncio
async def test_build_failure_at_list_creation_emits_build_failed_not_completed(service_factory):
    apollo = make_mock_apollo()
    apollo.create_list.side_effect = RuntimeError("Apollo list API down")
    service, store = service_factory(apollo)
    await store.create(make_searched_campaign("c1", [make_prospect("p1")]))

    campaign = await service.build("c1")
    assert campaign.status == CampaignStatus.FAILED

    events = await service.activity_log.store.list()
    assert len(events) == 1
    assert events[0].event_type == "campaign.build_failed"
    assert events[0].category == ActivityCategory.ERRORS
    assert "Apollo list API down" in events[0].metadata["error"]


@pytest.mark.asyncio
async def test_build_failure_at_sequence_creation_emits_build_failed(service_factory):
    apollo = make_mock_apollo()
    apollo.create_sequence.side_effect = RuntimeError("sequence API down")
    service, store = service_factory(apollo)
    await store.create(make_searched_campaign("c1", [make_prospect("p1")]))

    campaign = await service.build("c1")
    assert campaign.status == CampaignStatus.FAILED

    events = await service.activity_log.store.list()
    build_events = [e for e in events if e.event_type.startswith("campaign.build_")]
    assert len(build_events) == 1
    assert build_events[0].event_type == "campaign.build_failed"


@pytest.mark.asyncio
async def test_soft_per_contact_failure_still_reports_build_completed(service_factory):
    """A soft failure (one contact's creation fails, but list+sequence
    succeed) must NOT be reported as campaign.build_failed -- build() itself
    still finishes successfully overall; the soft error count is carried in
    build_completed's own metadata instead."""
    apollo = make_mock_apollo()
    apollo.create_contact.side_effect = RuntimeError("contact creation failed for one person")
    service, store = service_factory(apollo)
    await store.create(make_searched_campaign("c1", [make_prospect("p1")]))

    campaign = await service.build("c1")
    assert campaign.status == CampaignStatus.BUILT

    events = await service.activity_log.store.list()
    assert [e.event_type for e in events] == ["campaign.build_completed"]
    assert events[0].metadata["soft_errors"] >= 1


@pytest.mark.asyncio
async def test_activate_emits_campaign_activated_event(service_factory):
    apollo = make_mock_apollo()
    service, store = service_factory(apollo)
    campaign = make_searched_campaign("c1", [])
    campaign.status = CampaignStatus.READY
    campaign.apollo_sequence_id = "seq-1"
    await store.create(campaign)

    await service.activate("c1")

    events = await service.activity_log.store.list()
    assert events[0].event_type == "campaign.activated"
    assert events[0].entity_id == "c1"


@pytest.mark.asyncio
async def test_pause_emits_campaign_paused_event(service_factory):
    apollo = make_mock_apollo()
    service, store = service_factory(apollo)
    campaign = make_searched_campaign("c1", [])
    campaign.status = CampaignStatus.ACTIVE
    campaign.apollo_sequence_id = "seq-1"
    await store.create(campaign)

    await service.pause("c1")

    events = await service.activity_log.store.list()
    assert events[0].event_type == "campaign.paused"


@pytest.mark.asyncio
async def test_activating_an_already_active_campaign_is_a_no_op_and_emits_nothing_new(service_factory):
    apollo = make_mock_apollo()
    service, store = service_factory(apollo)
    campaign = make_searched_campaign("c1", [])
    campaign.status = CampaignStatus.ACTIVE
    campaign.apollo_sequence_id = "seq-1"
    await store.create(campaign)

    await service.activate("c1")  # idempotent no-op per CampaignService.activate()'s own docstring

    events = await service.activity_log.store.list()
    assert events == []


# --- CampaignSyncService: one summary event per sync() call ---


@pytest.mark.asyncio
async def test_sync_emits_one_summary_event_not_one_per_sequence():
    from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
    from app.repositories.email_sequence_store import MemoryEmailSequenceStore
    from app.services.campaign_sync_service import CampaignSyncService

    campaign_store = MemoryCampaignStore()
    sequence_store = MemoryEmailSequenceStore()
    step_store = MemoryEmailSequenceStepStore()

    apollo = AsyncMock()
    apollo.list_sequences.return_value = {
        "emailer_campaigns": [
            {"id": f"seq-{i}", "name": f"Sequence {i}", "num_contacts": 10, "created_at": "2026-08-01T00:00:00Z"}
            for i in range(5)
        ],
        "pagination": {"total_pages": 1},
    }

    service = CampaignSyncService(campaign_store=campaign_store, sequence_store=sequence_store, step_store=step_store, apollo=apollo)
    report = await service.sync()
    assert report.created == 5

    events = await service.activity_log.store.list()
    sync_events = [e for e in events if e.event_type == "campaign.sync_completed"]
    assert len(sync_events) == 1
    assert sync_events[0].metadata["created"] == 5
    assert sync_events[0].entity_type is None  # bulk summary, no single entity


@pytest.mark.asyncio
async def test_sync_with_nothing_changed_emits_no_event():
    from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
    from app.repositories.email_sequence_store import MemoryEmailSequenceStore
    from app.services.campaign_sync_service import CampaignSyncService

    campaign_store = MemoryCampaignStore()
    sequence_store = MemoryEmailSequenceStore()
    step_store = MemoryEmailSequenceStepStore()
    apollo = AsyncMock()
    apollo.list_sequences.return_value = {"emailer_campaigns": [], "pagination": {"total_pages": 1}}

    service = CampaignSyncService(campaign_store=campaign_store, sequence_store=sequence_store, step_store=step_store, apollo=apollo)
    report = await service.sync()
    assert report.created == report.updated == report.archived == 0

    events = await service.activity_log.store.list()
    assert events == []
