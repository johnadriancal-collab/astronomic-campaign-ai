"""
Persistence tests for SQLiteCampaignStore -- the persistent replacement for
MemoryCampaignStore. Covers the same store contract MemoryCampaignStore
already satisfies (create/get/save/list, not-found behavior), plus the one
thing MemoryCampaignStore structurally cannot prove: that data survives
being read back through a brand-new connection to the same file, i.e. it
actually persisted rather than just living in process memory.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.repositories.campaign_store import CampaignNotFoundError
from app.repositories.sqlite_campaign_store import SQLiteCampaignStore
from app.services.campaign_service import CampaignService


def make_campaign(campaign_id: str = "c1") -> Campaign:
    return Campaign(
        campaign_id=campaign_id,
        original_prompt="test prompt",
        created_at="2026-07-31T00:00:00Z",
        plan=CampaignPlan(campaign_name="Test Campaign", filters=Filters(), sequence=[]),
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test_campaigns.db")
    s = SQLiteCampaignStore(db_path)
    await s.connect()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(store):
    campaign = make_campaign()
    await store.create(campaign)

    fetched = await store.get("c1")
    assert fetched is not None
    assert fetched.campaign_id == "c1"
    assert fetched.plan.campaign_name == "Test Campaign"
    assert fetched.status == CampaignStatus.DRAFT


@pytest.mark.asyncio
async def test_get_missing_campaign_returns_none(store):
    assert await store.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_create_duplicate_raises_value_error(store):
    await store.create(make_campaign())
    with pytest.raises(ValueError):
        await store.create(make_campaign())


@pytest.mark.asyncio
async def test_save_missing_campaign_raises_not_found(store):
    with pytest.raises(CampaignNotFoundError):
        await store.save(make_campaign())


@pytest.mark.asyncio
async def test_save_persists_mutations(store):
    campaign = make_campaign()
    await store.create(campaign)

    campaign.status = CampaignStatus.SEARCHED
    campaign.total_matches = 42
    await store.save(campaign)

    fetched = await store.get("c1")
    assert fetched.status == CampaignStatus.SEARCHED
    assert fetched.total_matches == 42


@pytest.mark.asyncio
async def test_list_returns_all_campaigns(store):
    await store.create(make_campaign("c1"))
    await store.create(make_campaign("c2"))

    campaigns = await store.list()
    assert {c.campaign_id for c in campaigns} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_data_survives_a_fresh_connection_to_the_same_file(tmp_path):
    """
    The real point of this store: unlike MemoryCampaignStore, data must
    still be there after the original connection is gone and a brand-new
    SQLiteCampaignStore opens the same file -- i.e. actual persistence,
    not just a dict that happens to survive within one process.
    """
    db_path = str(tmp_path / "persist_test.db")

    first = SQLiteCampaignStore(db_path)
    await first.connect()
    await first.create(make_campaign("persisted-1"))
    await first.close()

    second = SQLiteCampaignStore(db_path)
    await second.connect()
    fetched = await second.get("persisted-1")
    await second.close()

    assert fetched is not None
    assert fetched.campaign_id == "persisted-1"
    assert fetched.plan.campaign_name == "Test Campaign"


@pytest.mark.asyncio
async def test_campaign_service_search_idempotency_holds_with_sqlite_store(store):
    """
    Regression check for the Finding 1 fix (see test_campaign_service_search.py)
    against the real persistent store, not just MemoryCampaignStore -- proves
    the store swap didn't reintroduce the "empty selection re-triggers search"
    bug.
    """
    await store.create(make_campaign())

    fake_apollo = AsyncMock()
    fake_apollo.search_people.return_value = {"total_entries": 0, "people": []}
    fake_ranker = AsyncMock()
    fake_ranker.rank.return_value = {"ranked": []}

    service = CampaignService(apollo=fake_apollo, ranker=fake_ranker, store=store)

    await service.search("c1")
    await service.search("c1")

    assert fake_apollo.search_people.await_count == 1
    assert fake_ranker.rank.await_count == 1
