"""
Tests for EmailSequenceSyncService -- the explicit, manual sync between a
Campaign's Apollo sequence and our EmailSequence/EmailSequenceStep records.
Covers: first-sync snapshot creation, idempotency on repeat calls, status
derivation from Apollo's active/archived, and the core guarantee that a
failed Apollo call never advances last_synced_at or mutates anything.
"""

from unittest.mock import AsyncMock

import pytest

from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters, SequenceStep
from app.models.email_sequence import EmailSequenceStatus
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.services.email_sequence_sync_service import EmailSequenceSyncService


def make_built_campaign(campaign_id: str, apollo_sequence_id: str = "apollo-seq-1") -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt="test prompt",
        created_at="2026-07-31T00:00:00Z",
        status=CampaignStatus.BUILT,
        plan=CampaignPlan(
            campaign_name="Test Campaign",
            filters=Filters(),
            sequence=[
                SequenceStep(day=0, subject="Day 0 subject", body="Day 0 body"),
                SequenceStep(day=3, subject="Day 3 subject", body="Day 3 body"),
            ],
        ),
        apollo_list_id="list-1",
        apollo_sequence_id=apollo_sequence_id,
    )


def make_apollo_search_response(
    apollo_sequence_id: str,
    active: bool = True,
    archived: bool = False,
    status_reason: str = "manual_approve",
    unique_opened: int = 0,
    step_ids: tuple[str, str] = ("apollo-step-1", "apollo-step-2"),
) -> dict:
    return {
        "emailer_campaign": {
            "id": apollo_sequence_id,
            "active": active,
            "archived": archived,
            "status_reason": status_reason,
            "unique_scheduled": 10,
            "unique_delivered": 8,
            "unique_opened": unique_opened,
            "unique_clicked": 1,
            "unique_replied": 0,
            "unique_bounced": 2,
            "unique_unsubscribed": 0,
            "emailer_steps": [
                {"id": step_ids[0], "position": 1, "wait_time": 0, "wait_mode": "day", "type": "auto_email"},
                {"id": step_ids[1], "position": 2, "wait_time": 3, "wait_mode": "day", "type": "auto_email"},
            ],
        }
    }


@pytest.fixture
def stores():
    campaign_store = MemoryCampaignStore()
    sequence_store = MemoryEmailSequenceStore()
    step_store = MemoryEmailSequenceStepStore()
    return campaign_store, sequence_store, step_store


def make_service(stores, apollo) -> EmailSequenceSyncService:
    campaign_store, sequence_store, step_store = stores
    return EmailSequenceSyncService(
        campaign_store=campaign_store, store=sequence_store, step_store=step_store, apollo=apollo
    )


@pytest.mark.asyncio
async def test_sync_requires_campaign_to_have_apollo_sequence_id(stores):
    campaign_store, _seq_store, _step_store = stores
    campaign = make_built_campaign("c1")
    campaign.apollo_sequence_id = None
    await campaign_store.create(campaign)

    service = make_service(stores, AsyncMock())
    with pytest.raises(ValueError):
        await service.sync("c1")


@pytest.mark.asyncio
async def test_first_sync_creates_snapshot_from_campaign_plan(stores):
    campaign_store, seq_store, step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1")
    service = make_service(stores, apollo)

    sequence, steps = await service.sync("c1")

    assert sequence.campaign_id == "c1"
    assert sequence.apollo_sequence_id == "apollo-seq-1"
    assert sequence.name == "Test Campaign"
    assert len(steps) == 2
    assert steps[0].subject == "Day 0 subject"
    assert steps[0].day == 0
    assert steps[1].subject == "Day 3 subject"
    assert steps[1].day == 3


@pytest.mark.asyncio
async def test_first_sync_populates_status_and_stats_from_apollo(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1", unique_opened=42)
    service = make_service(stores, apollo)

    sequence, _steps = await service.sync("c1")

    assert sequence.status == EmailSequenceStatus.ACTIVE
    assert sequence.status_reason == "manual_approve"
    assert sequence.unique_opened == 42
    assert sequence.unique_bounced == 2
    assert sequence.last_synced_at is not None


@pytest.mark.asyncio
async def test_first_sync_populates_apollo_step_ids_by_position(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1")
    service = make_service(stores, apollo)

    _sequence, steps = await service.sync("c1")

    assert steps[0].apollo_step_id == "apollo-step-1"
    assert steps[1].apollo_step_id == "apollo-step-2"


@pytest.mark.asyncio
async def test_status_derivation_archived_takes_priority_over_active(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response(
        "apollo-seq-1", active=False, archived=True
    )
    service = make_service(stores, apollo)

    sequence, _steps = await service.sync("c1")

    assert sequence.status == EmailSequenceStatus.ARCHIVED


@pytest.mark.asyncio
async def test_status_derivation_paused_when_neither_active_nor_archived(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response(
        "apollo-seq-1", active=False, archived=False
    )
    service = make_service(stores, apollo)

    sequence, _steps = await service.sync("c1")

    assert sequence.status == EmailSequenceStatus.PAUSED


@pytest.mark.asyncio
async def test_resyncing_does_not_duplicate_sequence_or_steps(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1", unique_opened=1)
    service = make_service(stores, apollo)

    first_sequence, _ = await service.sync("c1")
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1", unique_opened=5)
    sequence, steps = await service.sync("c1")

    # Same underlying record (not a second one created) -- MemoryEmailSequenceStore.create()
    # would raise on a duplicate campaign_id, so a second sync() calling create() again
    # would have failed the test outright rather than silently duplicating.
    assert sequence.email_sequence_id == first_sequence.email_sequence_id
    assert len(steps) == 2
    # Stats reflect the SECOND sync's values -- proves it actually refreshed, not just no-op'd.
    assert sequence.unique_opened == 5


@pytest.mark.asyncio
async def test_failed_apollo_call_does_not_advance_last_synced_at(stores):
    campaign_store, seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1")
    service = make_service(stores, apollo)

    # A successful first sync establishes a baseline last_synced_at.
    first_sequence, _ = await service.sync("c1")
    first_synced_at = first_sequence.last_synced_at
    assert first_synced_at is not None

    # Now Apollo fails.
    apollo.get_sequence.side_effect = RuntimeError("Apollo is down")
    with pytest.raises(RuntimeError):
        await service.sync("c1")

    stored = await seq_store.get_by_campaign_id("c1")
    assert stored.last_synced_at == first_synced_at  # unchanged
    assert stored.status == EmailSequenceStatus.ACTIVE  # unchanged from the successful sync


@pytest.mark.asyncio
async def test_failed_first_sync_apollo_call_still_leaves_snapshot_but_no_synced_at(stores):
    """
    The deployed-configuration snapshot is derived from our own already-
    stored plan (not an Apollo call), so it's created even if the
    subsequent Apollo status call then fails -- but last_synced_at must
    stay None, since Apollo's own state was never actually confirmed.
    """
    campaign_store, seq_store, step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.side_effect = RuntimeError("Apollo is down")
    service = make_service(stores, apollo)

    with pytest.raises(RuntimeError):
        await service.sync("c1")

    stored = await seq_store.get_by_campaign_id("c1")
    assert stored is not None  # snapshot was created
    assert stored.last_synced_at is None  # but never confirmed synced
    steps = await step_store.list_for_sequence(stored.email_sequence_id)
    assert len(steps) == 2
    assert steps[0].apollo_step_id is None  # never confirmed by Apollo


@pytest.mark.asyncio
async def test_get_for_campaign_returns_none_before_first_sync(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    service = make_service(stores, AsyncMock())

    assert await service.get_for_campaign("c1") is None


@pytest.mark.asyncio
async def test_get_for_campaign_returns_populated_result_after_sync(stores):
    campaign_store, _seq_store, _step_store = stores
    await campaign_store.create(make_built_campaign("c1"))
    apollo = AsyncMock()
    apollo.get_sequence.return_value = make_apollo_search_response("apollo-seq-1")
    service = make_service(stores, apollo)
    await service.sync("c1")

    result = await service.get_for_campaign("c1")

    assert result is not None
    sequence, steps = result
    assert sequence.campaign_id == "c1"
    assert len(steps) == 2
