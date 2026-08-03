"""
Tests for CampaignSyncService -- discovery/update of SYNCED campaigns from
Apollo's full sequence list, archive-reconciliation, duplicate prevention,
NATIVE-campaign protection, and the sync report's accuracy.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.apollo.client import ApolloAPIError
from app.models.campaign import Campaign, CampaignPlan, CampaignSource, CampaignStatus, Filters, SequenceStep
from app.models.email_sequence import EmailSequence, EmailSequenceStatus, EmailSequenceStep
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.email_sequence_step_store import MemoryEmailSequenceStepStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.services.campaign_sync_service import CampaignSyncService


def make_apollo_sequence(apollo_id: str, name: str, active: bool = True, archived: bool = False, **overrides) -> dict:
    base = {
        "id": apollo_id,
        "name": name,
        "active": active,
        "archived": archived,
        "status_reason": None,
        "unique_scheduled": 0,
        "unique_delivered": 0,
        "unique_opened": 0,
        "unique_clicked": 0,
        "unique_replied": 0,
        "unique_bounced": 0,
        "unique_unsubscribed": 0,
        "emailer_steps": [
            {"id": f"{apollo_id}-step-1", "position": 1, "wait_time": 0, "wait_mode": "day", "type": "auto_email"},
        ],
    }
    base.update(overrides)
    return base


def make_list_response(sequences: list[dict], page: int = 1, total_pages: int = 1) -> dict:
    return {
        "emailer_campaigns": sequences,
        "pagination": {"page": page, "per_page": 100, "total_entries": len(sequences), "total_pages": total_pages},
    }


@pytest.fixture
def stores():
    return MemoryCampaignStore(), MemoryEmailSequenceStore(), MemoryEmailSequenceStepStore()


def make_service(stores, apollo) -> CampaignSyncService:
    campaign_store, sequence_store, step_store = stores
    return CampaignSyncService(campaign_store=campaign_store, sequence_store=sequence_store, step_store=step_store, apollo=apollo)


@pytest.mark.asyncio
async def test_discovers_new_sequence_as_synced_campaign(stores):
    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response(
        [make_apollo_sequence("apollo-1", "Lumen Analytics")]
    )
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.found == 1
    assert report.created == 1
    assert report.updated == 0
    assert report.unchanged == 0
    assert report.archived == 0

    campaign_store, sequence_store, step_store = stores
    campaigns = await campaign_store.list()
    assert len(campaigns) == 1
    assert campaigns[0].source == CampaignSource.SYNCED
    assert campaigns[0].plan.campaign_name == "Lumen Analytics"
    assert campaigns[0].apollo_sequence_id == "apollo-1"
    assert campaigns[0].status == CampaignStatus.ACTIVE

    sequence = await sequence_store.get_by_apollo_sequence_id("apollo-1")
    assert sequence is not None
    steps = await step_store.list_for_sequence(sequence.email_sequence_id)
    assert len(steps) == 1
    assert steps[0].apollo_step_id == "apollo-1-step-1"


@pytest.mark.asyncio
async def test_pages_through_full_list_using_total_pages(stores):
    apollo = AsyncMock()
    apollo.list_sequences.side_effect = [
        make_list_response([make_apollo_sequence("apollo-1", "First")], page=1, total_pages=2),
        make_list_response([make_apollo_sequence("apollo-2", "Second")], page=2, total_pages=2),
    ]
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.found == 2
    assert report.created == 2
    assert apollo.list_sequences.call_count == 2


@pytest.mark.asyncio
async def test_resyncing_does_not_create_duplicate_campaigns(stores):
    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response(
        [make_apollo_sequence("apollo-1", "Lumen Analytics")]
    )
    service = make_service(stores, apollo)

    await service.sync()
    report2 = await service.sync()

    campaign_store, _seq_store, _step_store = stores
    assert len(await campaign_store.list()) == 1
    assert report2.created == 0
    assert report2.unchanged == 1


@pytest.mark.asyncio
async def test_updates_synced_campaign_when_apollo_data_changes(stores):
    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response(
        [make_apollo_sequence("apollo-1", "Lumen Analytics", unique_opened=0)]
    )
    service = make_service(stores, apollo)
    await service.sync()

    apollo.list_sequences.return_value = make_list_response(
        [make_apollo_sequence("apollo-1", "Lumen Analytics", unique_opened=5)]
    )
    report2 = await service.sync()

    assert report2.updated == 1
    assert report2.unchanged == 0
    campaign_store, sequence_store, _step_store = stores
    sequence = await sequence_store.get_by_apollo_sequence_id("apollo-1")
    assert sequence.unique_opened == 5


@pytest.mark.asyncio
async def test_native_campaign_content_is_never_touched(stores):
    campaign_store, sequence_store, step_store = stores
    now = datetime.now(timezone.utc)
    native = Campaign(
        campaign_id="native-1",
        original_prompt="a real builder prompt",
        created_at=now,
        status=CampaignStatus.ACTIVE,
        source=CampaignSource.NATIVE,
        plan=CampaignPlan(campaign_name="My Real Campaign", filters=Filters(), sequence=[SequenceStep(day=0, subject="Real subject", body="Real body")]),
        apollo_sequence_id="apollo-native-1",
    )
    await campaign_store.create(native)
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-native-1",
            campaign_id="native-1",
            apollo_sequence_id="apollo-native-1",
            name="My Real Campaign",
            status=EmailSequenceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    await step_store.create(
        EmailSequenceStep(
            email_sequence_step_id="step-native-1",
            email_sequence_id="seq-native-1",
            apollo_step_id="apollo-native-1-step-1",
            position=1,
            day=0,
            subject="Real subject",
            body="Real body",
        )
    )

    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response(
        [make_apollo_sequence("apollo-native-1", "Renamed In Apollo")]
    )
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.created == 0
    assert report.updated == 0
    assert report.unchanged == 0  # NATIVE campaigns are skipped entirely, not counted either way

    unchanged_campaign = await campaign_store.get("native-1")
    assert unchanged_campaign.plan.campaign_name == "My Real Campaign"  # untouched, not renamed
    unchanged_steps = await step_store.list_for_sequence("seq-native-1")
    assert unchanged_steps[0].subject == "Real subject"  # untouched


@pytest.mark.asyncio
async def test_reconciles_archived_via_explicit_archived_flag(stores):
    campaign_store, sequence_store, step_store = stores
    now = datetime.now(timezone.utc)
    await campaign_store.create(
        Campaign(
            campaign_id="c1",
            original_prompt="",
            created_at=now,
            source=CampaignSource.SYNCED,
            plan=CampaignPlan(campaign_name="Solstice Health", filters=Filters(), sequence=[]),
            apollo_sequence_id="apollo-solstice",
        )
    )
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="c1",
            apollo_sequence_id="apollo-solstice",
            name="Solstice Health",
            status=EmailSequenceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )

    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response([])  # Solstice no longer in the list
    apollo.get_sequence.return_value = {"emailer_campaign": {"id": "apollo-solstice", "archived": True, "active": False}}
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.archived == 1
    sequence = await sequence_store.get_by_apollo_sequence_id("apollo-solstice")
    assert sequence.status == EmailSequenceStatus.ARCHIVED


@pytest.mark.asyncio
async def test_reconciles_archived_via_404(stores):
    campaign_store, sequence_store, _step_store = stores
    now = datetime.now(timezone.utc)
    await campaign_store.create(
        Campaign(
            campaign_id="c1",
            original_prompt="",
            created_at=now,
            source=CampaignSource.SYNCED,
            plan=CampaignPlan(campaign_name="Deleted One", filters=Filters(), sequence=[]),
            apollo_sequence_id="apollo-deleted",
        )
    )
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="c1",
            apollo_sequence_id="apollo-deleted",
            name="Deleted One",
            status=EmailSequenceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )

    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response([])
    apollo.get_sequence.side_effect = ApolloAPIError("not found", status_code=404)
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.archived == 1
    sequence = await sequence_store.get_by_apollo_sequence_id("apollo-deleted")
    assert sequence.status == EmailSequenceStatus.ARCHIVED


@pytest.mark.asyncio
async def test_ambiguous_reconciliation_error_propagates_and_does_not_archive(stores):
    campaign_store, sequence_store, _step_store = stores
    now = datetime.now(timezone.utc)
    await campaign_store.create(
        Campaign(
            campaign_id="c1",
            original_prompt="",
            created_at=now,
            source=CampaignSource.SYNCED,
            plan=CampaignPlan(campaign_name="Maybe Fine", filters=Filters(), sequence=[]),
            apollo_sequence_id="apollo-x",
        )
    )
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="c1",
            apollo_sequence_id="apollo-x",
            name="Maybe Fine",
            status=EmailSequenceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )

    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response([])
    apollo.get_sequence.side_effect = ApolloAPIError("server error", status_code=500)
    service = make_service(stores, apollo)

    with pytest.raises(ApolloAPIError):
        await service.sync()

    sequence = await sequence_store.get_by_apollo_sequence_id("apollo-x")
    assert sequence.status == EmailSequenceStatus.ACTIVE  # untouched, not guessed as archived


@pytest.mark.asyncio
async def test_full_list_failure_aborts_and_creates_nothing(stores):
    apollo = AsyncMock()
    apollo.list_sequences.side_effect = ApolloAPIError("Apollo is down")
    service = make_service(stores, apollo)

    with pytest.raises(ApolloAPIError):
        await service.sync()

    campaign_store, _seq_store, _step_store = stores
    assert await campaign_store.list() == []


@pytest.mark.asyncio
async def test_already_archived_sequence_is_not_rechecked(stores):
    campaign_store, sequence_store, _step_store = stores
    now = datetime.now(timezone.utc)
    await campaign_store.create(
        Campaign(
            campaign_id="c1",
            original_prompt="",
            created_at=now,
            source=CampaignSource.SYNCED,
            plan=CampaignPlan(campaign_name="Already Gone", filters=Filters(), sequence=[]),
            apollo_sequence_id="apollo-gone",
        )
    )
    await sequence_store.create(
        EmailSequence(
            email_sequence_id="seq-1",
            campaign_id="c1",
            apollo_sequence_id="apollo-gone",
            name="Already Gone",
            status=EmailSequenceStatus.ARCHIVED,
            created_at=now,
            updated_at=now,
        )
    )

    apollo = AsyncMock()
    apollo.list_sequences.return_value = make_list_response([])
    service = make_service(stores, apollo)

    report = await service.sync()

    assert report.archived == 0
    apollo.get_sequence.assert_not_called()
