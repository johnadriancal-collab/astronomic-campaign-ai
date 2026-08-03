"""
Persistence tests for SQLiteLeadStore and SQLiteCampaignLeadStore -- same
contract MemoryLeadStore/MemoryCampaignLeadStore already satisfy (see
test_lead_system.py), plus the one thing memory stores can't prove: data
surviving a fresh connection to the same file (and, for campaign_leads,
surviving the claude_score/claude_reason schema migration on a table that
already had real rows before those columns existed).
"""

from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.lead import CampaignLead, CampaignLeadStatus, Lead, LeadStatus
from app.repositories.lead_store import LeadNotFoundError
from app.repositories.sqlite_campaign_lead_store import SQLiteCampaignLeadStore
from app.repositories.sqlite_lead_store import SQLiteLeadStore


def make_lead(lead_id: str, apollo_contact_id: str) -> Lead:
    now = datetime.now(timezone.utc)
    return Lead(
        lead_id=lead_id,
        apollo_contact_id=apollo_contact_id,
        first_name="Riley",
        title="VP of Sales",
        company="Acme Logistics",
        status=LeadStatus.NEW,
        created_at=now,
        updated_at=now,
    )


@pytest_asyncio.fixture
async def lead_store(tmp_path):
    store = SQLiteLeadStore(str(tmp_path / "leads.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def campaign_lead_store(tmp_path):
    store = SQLiteCampaignLeadStore(str(tmp_path / "campaign_leads.db"))
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(lead_store):
    await lead_store.create(make_lead("l1", "contact-1"))

    fetched = await lead_store.get("l1")
    assert fetched is not None
    assert fetched.apollo_contact_id == "contact-1"
    assert fetched.company == "Acme Logistics"


@pytest.mark.asyncio
async def test_get_by_apollo_contact_id(lead_store):
    await lead_store.create(make_lead("l1", "contact-1"))

    found = await lead_store.get_by_apollo_contact_id("contact-1")
    assert found is not None
    assert found.lead_id == "l1"
    assert await lead_store.get_by_apollo_contact_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_duplicate_apollo_contact_id_raises(lead_store):
    await lead_store.create(make_lead("l1", "contact-1"))
    with pytest.raises(ValueError):
        await lead_store.create(make_lead("l2", "contact-1"))


@pytest.mark.asyncio
async def test_save_missing_lead_raises_not_found(lead_store):
    with pytest.raises(LeadNotFoundError):
        await lead_store.save(make_lead("does-not-exist", "contact-x"))


@pytest.mark.asyncio
async def test_data_survives_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist_test.db")

    first = SQLiteLeadStore(db_path)
    await first.connect()
    await first.create(make_lead("l1", "contact-1"))
    await first.close()

    second = SQLiteLeadStore(db_path)
    await second.connect()
    fetched = await second.get("l1")
    await second.close()

    assert fetched is not None
    assert fetched.apollo_contact_id == "contact-1"


@pytest.mark.asyncio
async def test_campaign_lead_add_is_idempotent(campaign_lead_store):
    membership = CampaignLead(
        campaign_id="c1", lead_id="l1", status=CampaignLeadStatus.ADDED, added_at=datetime.now(timezone.utc)
    )

    await campaign_lead_store.add(membership)
    await campaign_lead_store.add(membership)  # re-adding the same pair must be a no-op

    assert len(await campaign_lead_store.list_for_campaign("c1")) == 1
    assert len(await campaign_lead_store.list_for_lead("l1")) == 1


@pytest.mark.asyncio
async def test_campaign_lead_list_for_lead_across_multiple_campaigns(campaign_lead_store):
    now = datetime.now(timezone.utc)
    await campaign_lead_store.add(CampaignLead(campaign_id="c1", lead_id="l1", added_at=now))
    await campaign_lead_store.add(CampaignLead(campaign_id="c2", lead_id="l1", added_at=now))

    memberships = await campaign_lead_store.list_for_lead("l1")
    assert {m.campaign_id for m in memberships} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_campaign_lead_persists_claude_score_and_reason(campaign_lead_store):
    await campaign_lead_store.add(
        CampaignLead(
            campaign_id="c1",
            lead_id="l1",
            added_at=datetime.now(timezone.utc),
            claude_score=91.5,
            claude_reason="Strong title and industry match.",
        )
    )

    memberships = await campaign_lead_store.list_for_campaign("c1")
    assert memberships[0].claude_score == 91.5
    assert memberships[0].claude_reason == "Strong title and industry match."


@pytest.mark.asyncio
async def test_existing_campaign_leads_survive_the_score_column_migration(tmp_path):
    """
    Requirement: rows created before claude_score/claude_reason existed on
    this table must still be readable (and the table still writable) after
    SQLiteCampaignLeadStore.connect() runs its ALTER TABLE migration --
    proving the migration is additive, not destructive.
    """
    db_path = str(tmp_path / "pre_migration.db")

    # Simulate the OLD schema (before this change) with a real row in it,
    # written with raw SQL exactly like the pre-migration code would have.
    old_conn = await aiosqlite.connect(db_path)
    await old_conn.execute(
        """
        CREATE TABLE campaign_leads (
            campaign_id TEXT NOT NULL,
            lead_id TEXT NOT NULL,
            status TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (campaign_id, lead_id)
        )
        """
    )
    await old_conn.execute(
        "INSERT INTO campaign_leads (campaign_id, lead_id, status, added_at) VALUES (?, ?, ?, ?)",
        ("pre-existing-campaign", "pre-existing-lead", "added", "2026-07-31T11:10:11+00:00"),
    )
    await old_conn.commit()
    await old_conn.close()

    # Now open it through the current store -- this is what happens on
    # the next app startup against a real, already-populated database.
    store = SQLiteCampaignLeadStore(db_path)
    await store.connect()

    surviving = await store.list_for_campaign("pre-existing-campaign")
    assert len(surviving) == 1
    assert surviving[0].lead_id == "pre-existing-lead"
    assert surviving[0].claude_score is None  # not fabricated -- genuinely never recorded pre-migration

    # And the table is still fully writable post-migration.
    await store.add(
        CampaignLead(
            campaign_id="new-campaign",
            lead_id="new-lead",
            added_at=datetime.now(timezone.utc),
            claude_score=80,
            claude_reason="Added after migration.",
        )
    )
    assert len((await store.list_for_campaign("new-campaign"))) == 1

    await store.close()
