"""
Persistence tests for SQLiteItfIngestionLogStore -- same upsert contract
MemoryItfIngestionLogStore already satisfies, plus data surviving a fresh
connection to the same file.
"""

from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.itf import ItfIngestionLogEntry, ItfRowStatus
from app.repositories.sqlite_itf_ingestion_log_store import SQLiteItfIngestionLogStore


def make_entry(
    row_number: int,
    content_hash: str = "hash-1",
    status: ItfRowStatus = ItfRowStatus.CREATED,
    response_id: str | None = None,
) -> ItfIngestionLogEntry:
    return ItfIngestionLogEntry(
        row_number=row_number,
        content_hash=content_hash,
        status=status,
        response_id=response_id,
        crm_contact_id="contact-1",
        email="riley@example.com",
        processed_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def log_store(tmp_path):
    store = SQLiteItfIngestionLogStore(str(tmp_path / "itf_log.db"))
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_save_and_get_roundtrip(log_store):
    await log_store.save(make_entry(2))

    fetched = await log_store.get(2)
    assert fetched is not None
    assert fetched.content_hash == "hash-1"
    assert fetched.status == ItfRowStatus.CREATED
    assert fetched.crm_contact_id == "contact-1"


@pytest.mark.asyncio
async def test_get_missing_row_returns_none(log_store):
    assert await log_store.get(999) is None


@pytest.mark.asyncio
async def test_save_upserts_on_same_row_number(log_store):
    await log_store.save(make_entry(2, content_hash="hash-1", status=ItfRowStatus.ERROR))
    await log_store.save(make_entry(2, content_hash="hash-2", status=ItfRowStatus.CREATED))

    fetched = await log_store.get(2)
    assert fetched.content_hash == "hash-2"
    assert fetched.status == ItfRowStatus.CREATED


@pytest.mark.asyncio
async def test_get_all_returns_every_entry_keyed_by_row_number(log_store):
    await log_store.save(make_entry(2))
    await log_store.save(make_entry(3, content_hash="hash-2"))

    all_entries = await log_store.get_all()
    assert set(all_entries.keys()) == {2, 3}
    assert all_entries[3].content_hash == "hash-2"


@pytest.mark.asyncio
async def test_data_survives_a_fresh_connection_to_the_same_file(tmp_path):
    db_path = str(tmp_path / "itf_log.db")

    store1 = SQLiteItfIngestionLogStore(db_path)
    await store1.connect()
    await store1.save(make_entry(2))
    await store1.close()

    store2 = SQLiteItfIngestionLogStore(db_path)
    await store2.connect()
    fetched = await store2.get(2)
    assert fetched is not None
    assert fetched.crm_contact_id == "contact-1"
    await store2.close()


@pytest.mark.asyncio
async def test_response_id_persists_when_present(log_store):
    await log_store.save(make_entry(2, response_id="form-response-abc"))
    fetched = await log_store.get(2)
    assert fetched.response_id == "form-response-abc"


@pytest.mark.asyncio
async def test_response_id_is_none_when_not_provided(log_store):
    await log_store.save(make_entry(2))
    fetched = await log_store.get(2)
    assert fetched.response_id is None


@pytest.mark.asyncio
async def test_migration_adds_response_id_column_to_a_pre_existing_table(tmp_path):
    """A table created before response_id existed in the schema must still
    load correctly and accept the new column -- same safety contract as
    SQLiteCampaignLeadStore's claude_score/claude_reason migration."""
    db_path = str(tmp_path / "itf_log.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        """
        CREATE TABLE itf_ingestion_log (
            row_number INTEGER PRIMARY KEY,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            crm_contact_id TEXT,
            email TEXT,
            error_message TEXT,
            processed_at TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        "INSERT INTO itf_ingestion_log (row_number, content_hash, status, crm_contact_id, email, processed_at) "
        "VALUES (5, 'old-hash', 'created', 'contact-old', 'old@example.com', '2026-01-01T00:00:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    store = SQLiteItfIngestionLogStore(db_path)
    await store.connect()
    pre_existing = await store.get(5)
    assert pre_existing is not None
    assert pre_existing.response_id is None
    assert pre_existing.crm_contact_id == "contact-old"

    await store.save(make_entry(6, response_id="new-response"))
    fresh = await store.get(6)
    assert fresh.response_id == "new-response"
    await store.close()
