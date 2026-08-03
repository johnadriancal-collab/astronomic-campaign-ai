"""
Covers Finding 1 from the determinism audit: search()'s idempotency guard
must key off `campaign.status` alone. Gating on `selected_prospects` being
non-empty too would treat a legitimate zero-selection outcome as "not yet
searched" and let a second call re-run a real Apollo search + Claude
ranking call for the same campaign.
"""

from unittest.mock import AsyncMock

import pytest

from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.repositories.campaign_store import MemoryCampaignStore
from app.services.campaign_service import CampaignService


def make_campaign() -> Campaign:
    return Campaign(
        campaign_id="c1",
        original_prompt="test prompt",
        created_at="2026-07-31T00:00:00Z",
        plan=CampaignPlan(campaign_name="Test Campaign", filters=Filters(), sequence=[]),
    )


@pytest.mark.asyncio
async def test_search_with_zero_selected_prospects_does_not_rerun():
    store = MemoryCampaignStore()
    await store.create(make_campaign())

    fake_apollo = AsyncMock()
    fake_apollo.search_people.return_value = {"total_entries": 0, "people": []}

    fake_ranker = AsyncMock()
    # Simulates Claude legitimately selecting nobody (empty pool, or every
    # returned apollo_person_id dropped as unmatched in parse_response()).
    fake_ranker.rank.return_value = {"ranked": []}

    service = CampaignService(apollo=fake_apollo, ranker=fake_ranker, store=store)

    first = await service.search("c1")
    assert first.status == CampaignStatus.SEARCHED
    assert first.selected_prospects == []
    assert fake_apollo.search_people.await_count == 1
    assert fake_ranker.rank.await_count == 1

    second = await service.search("c1")
    assert second.status == CampaignStatus.SEARCHED
    assert second.selected_prospects == []

    # The real assertion: a second call must not trigger another real
    # Apollo search or Claude ranking call, even though selected_prospects
    # is empty both times.
    assert fake_apollo.search_people.await_count == 1
    assert fake_ranker.rank.await_count == 1
    assert "Search requested again -- returning cached result" in second.logs


@pytest.mark.asyncio
async def test_search_with_nonempty_selection_still_does_not_rerun():
    """Regression guard: the normal (non-empty) cached-result path must keep working."""
    store = MemoryCampaignStore()
    await store.create(make_campaign())

    fake_apollo = AsyncMock()
    fake_apollo.search_people.return_value = {
        "total_entries": 1,
        "people": [{"id": "p1", "has_email": True}],
    }

    fake_ranker = AsyncMock()
    fake_ranker.rank.return_value = {"ranked": [{"id": "p1", "claude_score": 90}]}

    service = CampaignService(apollo=fake_apollo, ranker=fake_ranker, store=store)

    await service.search("c1")
    await service.search("c1")

    assert fake_apollo.search_people.await_count == 1
    assert fake_ranker.rank.await_count == 1
