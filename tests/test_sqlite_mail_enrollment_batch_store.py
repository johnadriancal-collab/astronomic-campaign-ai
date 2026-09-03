"""
Direct SQLite-layer tests for MailEnrollmentBatchStore -- same tmp_path-
backed real SQLite file convention as test_sqlite_mail_send_window_store.py.
Proves the batch table round-trips real MailEnrollmentBatch objects,
campaign-scoped listing/ordering, reconnect durability, and (Stage 3)
idempotency-key uniqueness, save()/get_by_idempotency_key()/list_by_status(),
and the old-table migration path.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchSource, MailEnrollmentBatchStatus
from app.repositories.mail_enrollment_batch_store import (
    DuplicateBatchIdempotencyKeyError,
    MailEnrollmentBatchNotFoundError,
)
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
    idempotency_key=None, status=MailEnrollmentBatchStatus.READY,
    created_at=NOW, submitted=3, enrolled=2, already_enrolled=1, suppressed=0,
) -> MailEnrollmentBatch:
    return MailEnrollmentBatch(
        batch_id=batch_id,
        mail_campaign_id=campaign_id,
        source=source,
        source_list_id="list-1" if source == MailEnrollmentBatchSource.CRM_LIST else None,
        source_import_batch_id="import-1" if source == MailEnrollmentBatchSource.CSV_UPLOAD else None,
        idempotency_key=idempotency_key or f"key-{batch_id}",
        status=status,
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
        idempotency_key="csv-key-1", status=MailEnrollmentBatchStatus.PREPARING,
        submitted=10, enrolled=7, already_enrolled=3, suppressed=2,
    )
    await store.create(batch)

    fetched = await store.get("b1")
    assert fetched == batch
    assert fetched.source == MailEnrollmentBatchSource.CSV_UPLOAD
    assert fetched.source_import_batch_id == "import-1"
    assert fetched.idempotency_key == "csv-key-1"
    assert fetched.status == MailEnrollmentBatchStatus.PREPARING
    assert fetched.submitted_count == 10
    assert fetched.enrolled_count == 7
    assert fetched.already_enrolled_count == 3
    assert fetched.suppressed_count == 2


async def test_create_duplicate_batch_id_is_rejected(store):
    await store.create(_batch(batch_id="b1", idempotency_key="key-a"))
    with pytest.raises(ValueError):
        await store.create(_batch(batch_id="b1", idempotency_key="key-b"))


async def test_create_duplicate_idempotency_key_for_same_campaign_is_rejected(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", idempotency_key="shared-key"))
    with pytest.raises(DuplicateBatchIdempotencyKeyError):
        await store.create(_batch(batch_id="b2", campaign_id="c1", idempotency_key="shared-key"))


async def test_same_idempotency_key_is_fine_across_different_campaigns(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", idempotency_key="shared-key"))
    await store.create(_batch(batch_id="b2", campaign_id="c2", idempotency_key="shared-key"))

    assert (await store.get("b1")).mail_campaign_id == "c1"
    assert (await store.get("b2")).mail_campaign_id == "c2"


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


async def test_get_by_idempotency_key_finds_match_scoped_to_campaign(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", idempotency_key="key-1"))
    await store.create(_batch(batch_id="b2", campaign_id="c2", idempotency_key="key-1"))

    found = await store.get_by_idempotency_key("c1", "key-1")
    assert found is not None
    assert found.batch_id == "b1"


async def test_get_by_idempotency_key_returns_none_when_no_match(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", idempotency_key="key-1"))

    assert await store.get_by_idempotency_key("c1", "does-not-exist") is None
    assert await store.get_by_idempotency_key("other-campaign", "key-1") is None


async def test_save_updates_an_existing_batch_in_place(store):
    batch = _batch(batch_id="b1", status=MailEnrollmentBatchStatus.PREPARING, submitted=5, enrolled=0)
    await store.create(batch)

    updated = batch.model_copy(update={"status": MailEnrollmentBatchStatus.READY, "enrolled_count": 5})
    await store.save(updated)

    fetched = await store.get("b1")
    assert fetched.status == MailEnrollmentBatchStatus.READY
    assert fetched.enrolled_count == 5
    assert fetched.submitted_count == 5


async def test_save_on_missing_batch_raises_not_found(store):
    with pytest.raises(MailEnrollmentBatchNotFoundError):
        await store.save(_batch(batch_id="does-not-exist"))


async def test_save_enforces_idempotency_key_uniqueness(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", idempotency_key="key-a"))
    await store.create(_batch(batch_id="b2", campaign_id="c1", idempotency_key="key-b"))

    with pytest.raises(DuplicateBatchIdempotencyKeyError):
        await store.save(_batch(batch_id="b2", campaign_id="c1", idempotency_key="key-a"))


async def test_list_by_status_filters_across_campaigns(store):
    await store.create(_batch(batch_id="b1", campaign_id="c1", status=MailEnrollmentBatchStatus.PREPARING))
    await store.create(_batch(batch_id="b2", campaign_id="c2", status=MailEnrollmentBatchStatus.PREPARING))
    await store.create(_batch(batch_id="b3", campaign_id="c1", status=MailEnrollmentBatchStatus.READY))

    preparing = await store.list_by_status(MailEnrollmentBatchStatus.PREPARING)
    assert {b.batch_id for b in preparing} == {"b1", "b2"}

    ready = await store.list_by_status(MailEnrollmentBatchStatus.READY)
    assert {b.batch_id for b in ready} == {"b3"}


async def test_migration_backfills_old_stage_2_shaped_table(tmp_path):
    """Stage 2's table shipped without idempotency_key/status columns --
    just (batch_id, mail_campaign_id, created_at, data), per this store's
    own CREATE_TABLE_SQL, which deliberately still matches that original
    shape. A store connecting against an already-deployed Stage-2 table
    must migrate it in place rather than fail. Stage 2 never shipped a
    write path for this table, so every real deployment's copy has zero
    rows -- this test simulates the empty-but-old-shaped table, not
    pre-existing data needing a JSON-payload backfill."""
    import aiosqlite

    db_path = str(tmp_path / "old_batches.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE mail_enrollment_batches (
                batch_id TEXT PRIMARY KEY,
                mail_campaign_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        await conn.commit()

    migrated_store = SQLiteMailEnrollmentBatchStore(db_path)
    await migrated_store.connect()
    await migrated_store.create(_batch(batch_id="b1", idempotency_key="key-1"))
    await migrated_store.close()

    # A second connect() re-runs the migration against an already-migrated
    # table -- must be idempotent, not fail on "column already exists".
    reconnected_store = SQLiteMailEnrollmentBatchStore(db_path)
    await reconnected_store.connect()
    try:
        fetched = await reconnected_store.get("b1")
        assert fetched.idempotency_key == "key-1"
        assert fetched.status == MailEnrollmentBatchStatus.READY

        with pytest.raises(DuplicateBatchIdempotencyKeyError):
            await reconnected_store.create(_batch(batch_id="b2", idempotency_key="key-1"))
    finally:
        await reconnected_store.close()
