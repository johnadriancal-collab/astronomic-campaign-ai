"""
Direct SQLite-layer tests for MailEnrollmentBatchStore -- same tmp_path-
backed real SQLite file convention as test_sqlite_mail_send_window_store.py.
Proves the batch table round-trips real MailEnrollmentBatch objects,
campaign-scoped listing/ordering, and reconnect durability.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchSource
from app.repositories.sqlite_mail_enrollment_batch_store import SQLiteMailEnrollmentBatchStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteMailEnrollmentBatchStore(str(tmp_path / "batches.db"))
    await s.connect()
    yield s
    await s.close()


def _batch(
    batch_id="b1", campaign_id="c1", source=MailEnrollmentBatchSource.CRM_LIST,
    created_at=NOW, submitted=3, enrolled=2, already_enrolled=1, suppressed=0,
) -> MailEnrollmentBatch:
    return MailEnrollmentBatch(
        batch_id=batch_id,
        mail_campaign_id=campaign_id,
        source=source,
        source_list_id="list-1" if source == MailEnrollmentBatchSource.CRM_LIST else None,
        source_import_batch_id="import-1" if source == MailEnrollmentBatchSource.CSV_UPLOAD else None,
        created_at=created_at,
        created_by_actor=None,
        submitted_count=submitted,
        enrolled_count=enrolled,
        already_enrolled_count=already_enrolled,
        suppressed_count=suppressed,
    )


async def test_list_starts_empty(store):
    assert await store.list_for_campaign("c1") == []


async def test_get_missing_returns_none(store):
    assert await store.get("does-not-exist") is None


async def test_create_and_get_round_trips_every_field(store):
    batch = _batch(
        batch_id="b1", campaign_id="c1", source=MailEnrollmentBatchSource.CSV_UPLOAD,
        submitted=10, enrolled=7, already_enrolled=3, suppressed=2,
    )
    await store.create(batch)

    fetched = await store.get("b1")
    assert fetched == batch
    assert fetched.source == MailEnrollmentBatchSource.CSV_UPLOAD
    assert fetched.source_import_batch_id == "import-1"
    assert fetched.submitted_count == 10
    assert fetched.enrolled_count == 7
    assert fetched.already_enrolled_count == 3
    assert fetched.suppressed_count == 2


async def test_create_duplicate_batch_id_is_rejected(store):
    await store.create(_batch(batch_id="b1"))
    with pytest.raises(ValueError):
        await store.create(_batch(batch_id="b1"))


async def test_list_for_campaign_only_returns_matching_campaign(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1"))
    await store.create(_batch(batch_id="b2", campaign_id="c2"))

    c1_batches = await store.list_for_campaign("c1")
    assert [b.batch_id for b in c1_batches] == ["b1"]


async def test_list_for_campaign_orders_newest_first(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", created_at=NOW))
    await store.create(_batch(batch_id="b2", campaign_id="c1", created_at=NOW + timedelta(hours=1)))
    await store.create(_batch(batch_id="b3", campaign_id="c1", created_at=NOW - timedelta(hours=1)))

    ordered = await store.list_for_campaign("c1")
    assert [b.batch_id for b in ordered] == ["b2", "b1", "b3"]


async def test_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "batches.db")
    store1 = SQLiteMailEnrollmentBatchStore(db_path)
    await store1.connect()
    await store1.create(_batch(batch_id="b1", campaign_id="c1"))
    await store1.close()

    store2 = SQLiteMailEnrollmentBatchStore(db_path)
    await store2.connect()
    fetched = await store2.get("b1")
    assert fetched is not None
    assert fetched.mail_campaign_id == "c1"
    await store2.close()
