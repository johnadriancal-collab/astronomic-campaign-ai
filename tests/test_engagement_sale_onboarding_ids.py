from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.client_crm import Engagement, EngagementType
from app.repositories.engagement_store import EngagementExternalIdConflictError, MemoryEngagementStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _engagement(engagement_id="e1", client_id="c1", **overrides) -> Engagement:
    return Engagement(
        engagement_id=engagement_id,
        client_id=client_id,
        title=overrides.pop("title", "Investor Dinner — Austin"),
        engagement_type=overrides.pop("engagement_type", EngagementType.OTHER),
        created_at=NOW,
        updated_at=NOW,
        **overrides,
    )


@pytest_asyncio.fixture
async def sqlite_engagement_store(tmp_path):
    s = SQLiteEngagementStore(str(tmp_path / "engagements.db"))
    await s.connect()
    yield s
    await s.close()


# --- get_by_sale_id ---------------------------------------------------


async def test_memory_get_by_sale_id():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", sale_id="sale-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_sale_id("sale-123")
    assert found.engagement_id == "e1"
    assert await store.get_by_sale_id("does-not-exist") is None


async def test_sqlite_get_by_sale_id(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", sale_id="sale-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_sale_id("sale-123")
    assert found.engagement_id == "e1"
    assert await store.get_by_sale_id("does-not-exist") is None


# --- get_by_docusign_envelope_id ---------------------------------------


async def test_memory_get_by_docusign_envelope_id():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", docusign_envelope_id="env-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_docusign_envelope_id("env-123")
    assert found.engagement_id == "e1"
    assert await store.get_by_docusign_envelope_id("does-not-exist") is None


async def test_sqlite_get_by_docusign_envelope_id(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", docusign_envelope_id="env-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_docusign_envelope_id("env-123")
    assert found.engagement_id == "e1"


# --- uniqueness enforcement ---------------------------------------------


async def test_memory_store_rejects_a_second_engagement_with_the_same_sale_id():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", sale_id="sale-123"))
    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e2", "c1", sale_id="sale-123"))
    assert exc_info.value.field == "sale_id"


async def test_sqlite_store_rejects_a_second_engagement_with_the_same_sale_id(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", sale_id="sale-123"))
    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e2", "c1", sale_id="sale-123"))
    assert exc_info.value.field == "sale_id"
    assert await store.get("e2") is None  # rejected write left no trace


async def test_memory_store_rejects_a_second_engagement_with_the_same_docusign_envelope_id():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", docusign_envelope_id="env-123"))
    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e2", "c1", docusign_envelope_id="env-123"))
    assert exc_info.value.field == "docusign_envelope_id"


async def test_sqlite_store_rejects_a_second_engagement_with_the_same_docusign_envelope_id(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", docusign_envelope_id="env-123"))
    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e2", "c1", docusign_envelope_id="env-123"))
    assert exc_info.value.field == "docusign_envelope_id"
    assert await store.get("e2") is None


async def test_sqlite_store_two_null_sale_ids_never_conflict(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))  # must not raise
    assert await store.get("e2") is not None


async def test_sqlite_store_sale_id_and_docusign_envelope_id_are_independent_constraints(sqlite_engagement_store):
    """A DIFFERENT sale_id with the SAME docusign_envelope_id must still
    conflict (and vice versa) -- the two partial unique indexes are
    independent, neither one's uniqueness masks the other's."""
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", sale_id="sale-1", docusign_envelope_id="env-1"))

    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e2", "c1", sale_id="sale-2", docusign_envelope_id="env-1"))
    assert exc_info.value.field == "docusign_envelope_id"

    with pytest.raises(EngagementExternalIdConflictError) as exc_info:
        await store.create(_engagement("e3", "c1", sale_id="sale-1", docusign_envelope_id="env-3"))
    assert exc_info.value.field == "sale_id"


async def test_existing_engagements_survive_the_sale_onboarding_column_migration(tmp_path):
    """Every pre-existing Engagement row (created before sale_id/
    docusign_envelope_id existed as real columns) must still be readable,
    and the table still writable, after SQLiteEngagementStore.connect()
    runs its migration -- proving it's additive, not destructive. Same
    convention as the analogous luma_event_id migration test."""
    db_path = str(tmp_path / "engagements.db")
    store = SQLiteEngagementStore(db_path)
    await store.connect()
    await store.create(_engagement("e1", "c1"))
    await store.close()

    # Reconnect -- runs the migration against a table that already has rows.
    store2 = SQLiteEngagementStore(db_path)
    await store2.connect()
    existing = await store2.get("e1")
    assert existing is not None
    assert existing.sale_id is None
    assert existing.docusign_envelope_id is None

    await store2.create(_engagement("e2", "c1", sale_id="sale-1", docusign_envelope_id="env-1"))
    assert (await store2.get_by_sale_id("sale-1")).engagement_id == "e2"
    await store2.close()
