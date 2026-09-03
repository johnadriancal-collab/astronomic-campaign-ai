"""
Direct SQLite-layer tests for MailCampaignCsvProspectLinkStore (Stage 4B,
2026-09-03) -- same tmp_path-backed real SQLite file convention as
test_sqlite_mail_enrollment_batch_store.py. Proves the real UNIQUE
(mail_campaign_id, idempotency_key) constraint, round-trip, and that
connect() is idempotent (a brand-new table, so "migration" here just
means CREATE TABLE IF NOT EXISTS never fails on a second call -- there is
no prior deployed shape to accommodate, unlike MailEnrollmentBatch's own
store).
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailCampaignCsvProspectLink
from app.repositories.mail_campaign_csv_prospect_link_store import DuplicateCsvProspectLinkError
from app.repositories.sqlite_mail_campaign_csv_prospect_link_store import SQLiteMailCampaignCsvProspectLinkStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteMailCampaignCsvProspectLinkStore(str(tmp_path / "links.db"))
    await s.connect()
    yield s
    await s.close()


def _link(mail_campaign_id="c1", idempotency_key="k1", import_batch_id="b1", created_at=NOW) -> MailCampaignCsvProspectLink:
    return MailCampaignCsvProspectLink(
        mail_campaign_id=mail_campaign_id, idempotency_key=idempotency_key,
        import_batch_id=import_batch_id, created_at=created_at,
    )


async def test_get_missing_returns_none(store):
    assert await store.get_by_idempotency_key("c1", "k1") is None


async def test_create_and_get_round_trips(store):
    await store.create(_link())
    got = await store.get_by_idempotency_key("c1", "k1")
    assert got == _link()


async def test_create_duplicate_campaign_and_key_is_rejected(store):
    await store.create(_link(import_batch_id="b1"))
    with pytest.raises(DuplicateCsvProspectLinkError):
        await store.create(_link(import_batch_id="b2"))  # same (campaign, key) -- different import_batch_id


async def test_get_by_idempotency_key_is_scoped_to_campaign(store):
    await store.create(_link(mail_campaign_id="c1", idempotency_key="shared-key", import_batch_id="b1"))
    await store.create(_link(mail_campaign_id="c2", idempotency_key="shared-key", import_batch_id="b2"))

    assert (await store.get_by_idempotency_key("c1", "shared-key")).import_batch_id == "b1"
    assert (await store.get_by_idempotency_key("c2", "shared-key")).import_batch_id == "b2"


async def test_same_idempotency_key_is_fine_across_different_campaigns(store):
    # Same assertion as above, phrased as the positive guarantee the
    # composite PRIMARY KEY (not idempotency_key alone) is what's enforced.
    await store.create(_link(mail_campaign_id="c1", idempotency_key="k", import_batch_id="b1"))
    await store.create(_link(mail_campaign_id="c2", idempotency_key="k", import_batch_id="b2"))
    assert await store.get_by_idempotency_key("c1", "k") is not None
    assert await store.get_by_idempotency_key("c2", "k") is not None


async def test_get_by_idempotency_key_returns_none_for_wrong_key(store):
    await store.create(_link(idempotency_key="k1"))
    assert await store.get_by_idempotency_key("c1", "does-not-exist") is None


async def test_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "links.db")
    store1 = SQLiteMailCampaignCsvProspectLinkStore(db_path)
    await store1.connect()
    await store1.create(_link())
    await store1.close()

    store2 = SQLiteMailCampaignCsvProspectLinkStore(db_path)
    await store2.connect()
    fetched = await store2.get_by_idempotency_key("c1", "k1")
    assert fetched is not None
    assert fetched.import_batch_id == "b1"
    await store2.close()


async def test_second_connect_is_idempotent_and_preserves_existing_rows(tmp_path):
    db_path = str(tmp_path / "links.db")
    store1 = SQLiteMailCampaignCsvProspectLinkStore(db_path)
    await store1.connect()
    await store1.create(_link())
    await store1.close()

    # A second connect() against the SAME already-created table must not
    # fail (CREATE TABLE IF NOT EXISTS is trivially idempotent) and must
    # not disturb existing rows.
    store2 = SQLiteMailCampaignCsvProspectLinkStore(db_path)
    await store2.connect()
    await store2.connect()  # connecting twice on the same instance too
    assert (await store2.get_by_idempotency_key("c1", "k1")).import_batch_id == "b1"

    with pytest.raises(DuplicateCsvProspectLinkError):
        await store2.create(_link(import_batch_id="b2"))
    await store2.close()


async def test_table_contains_no_pii_columns(tmp_path):
    """Structural proof of the 'no PII/raw CSV data in this table'
    requirement -- the schema itself is exactly four columns: two ids
    forming the key, the linked import batch id, and a timestamp."""
    import aiosqlite

    db_path = str(tmp_path / "links.db")
    store = SQLiteMailCampaignCsvProspectLinkStore(db_path)
    await store.connect()
    try:
        conn = await aiosqlite.connect(db_path)
        try:
            cursor = await conn.execute("PRAGMA table_info(mail_campaign_csv_prospect_links)")
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
        finally:
            await conn.close()
        assert columns == {"mail_campaign_id", "idempotency_key", "import_batch_id", "created_at"}
    finally:
        await store.close()
