"""
Tests for the Client CRM Engagement dinner-taxonomy migration (Stage 1E.1,
2026-09-08) -- app/repositories/engagement_taxonomy_migration.py.

Old-format rows are seeded via raw SQL/JSON, never via the Engagement
Pydantic model -- the whole point of this migration is that the CURRENT
model cannot construct or validate Stage 1E's old shape at all (confirmed
directly in this stage's own investigation: `engagement_type="investor_dinner"`
is rejected by the current EngagementType enum). Seeding must bypass the
model exactly the way real production data does.
"""

import json
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.client_crm import Engagement
from app.repositories.engagement_taxonomy_migration import (
    UnjustifiedLegacyEngagementError,
    migrate_legacy_dinner_taxonomy_rows,
)
from app.repositories.sqlite_engagement_store import (
    CREATE_CLIENT_INDEX_SQL,
    CREATE_TABLE_SQL,
    SQLiteEngagementStore,
)

pytestmark = pytest.mark.asyncio

NOW = "2026-09-01T00:00:00+00:00"


def _old_format_row(
    engagement_id: str,
    *,
    engagement_type: str,
    dinner_program: str | None = "not_set",  # sentinel: "not_set" omits the key entirely
    **overrides,
) -> dict:
    """Builds a raw Stage-1E-shaped Engagement dict (old taxonomy),
    exactly as it would have been persisted by the old code -- never
    constructed via the current Engagement model."""
    row = {
        "engagement_id": engagement_id,
        "client_id": "c1",
        "title": "SF Investor Dinner",
        "engagement_type": engagement_type,
        "engagement_date": "2026-09-22",
        "location": "San Francisco",
        "status": "confirmed",
        "owner": "Chris",
        "fee": 5000.0,
        "contract_status": "signed",
        "contract_url": "https://drive.example.com/contract",
        "signed_date": "2026-09-01",
        "payment_status": "partial",
        "luma_event_id": "luma-123",
        "created_at": NOW,
        "updated_at": NOW,
        "archived": False,
        **overrides,
    }
    if dinner_program != "not_set":
        row["dinner_program"] = dinner_program
    return row


@pytest_asyncio.fixture
async def raw_conn(tmp_path):
    """A bare aiosqlite connection to a fresh file with the engagements
    table created -- lets tests seed raw pre-migration rows directly,
    without going through SQLiteEngagementStore (which would already run
    the migration in connect())."""
    conn = await aiosqlite.connect(str(tmp_path / "engagements.db"))
    conn.row_factory = aiosqlite.Row
    await conn.execute(CREATE_TABLE_SQL)
    await conn.execute(CREATE_CLIENT_INDEX_SQL)
    await conn.commit()
    yield conn
    await conn.close()


async def _insert_raw(conn: aiosqlite.Connection, row: dict) -> None:
    await conn.execute(
        "INSERT INTO engagements (engagement_id, client_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
        (row["engagement_id"], row["client_id"], row["created_at"], row["updated_at"], json.dumps(row)),
    )
    await conn.commit()


async def _raw_data(conn: aiosqlite.Connection, engagement_id: str) -> dict:
    cursor = await conn.execute("SELECT data FROM engagements WHERE engagement_id = ?", (engagement_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return json.loads(row["data"])


# =====================================================================
# The known real production case
# =====================================================================


async def test_migrates_investor_dinner_plus_supernova_to_dinner_plus_investor_dinner(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e1", engagement_type="investor_dinner", dinner_program="supernova"))

    migrated_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    assert migrated_count == 1
    data = await _raw_data(raw_conn, "e1")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] == "investor_dinner"
    assert "dinner_program" not in data
    # Now genuinely readable by the current model.
    engagement = Engagement.model_validate(data)
    assert engagement.engagement_type == "dinner"
    assert engagement.dinner_type == "investor_dinner"


# =====================================================================
# Other combinations justified purely by the direct retired-label rename
# (Supernova/Galaxy/Aurora), still requiring engagement_type ==
# investor_dinner -- the only old engagement_type this migration trusts.
# =====================================================================


async def test_migrates_investor_dinner_plus_galaxy_to_dinner_plus_fireside_dinner(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e2", engagement_type="investor_dinner", dinner_program="galaxy"))

    migrated_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    assert migrated_count == 1
    data = await _raw_data(raw_conn, "e2")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] == "fireside_dinner"


async def test_migrates_investor_dinner_plus_aurora_to_dinner_plus_bizdev_dinner(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e3", engagement_type="investor_dinner", dinner_program="aurora"))

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    data = await _raw_data(raw_conn, "e3")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] == "bizdev_dinner"


async def test_migrates_dinner_program_other_to_null_dinner_type_rather_than_guessing(raw_conn):
    """Stage 1E.1's DinnerType has no OTHER member -- a historical
    "other"-tagged dinner named no specific kind even under the old
    taxonomy, so it becomes an honest absence (None), not a guess."""
    await _insert_raw(raw_conn, _old_format_row("e5", engagement_type="investor_dinner", dinner_program="other"))

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    data = await _raw_data(raw_conn, "e5")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] is None


async def test_migrates_investor_dinner_with_no_dinner_program_to_null_dinner_type(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e4", engagement_type="investor_dinner", dinner_program=None))

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    data = await _raw_data(raw_conn, "e4")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] is None


async def test_migrates_dinner_row_with_dinner_program_key_entirely_absent(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e6", engagement_type="investor_dinner", dinner_program="not_set"))

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    data = await _raw_data(raw_conn, "e6")
    assert data["engagement_type"] == "dinner"
    assert data["dinner_type"] is None


# =====================================================================
# Fail-closed: unjustified combinations are refused, never guessed
# =====================================================================


async def test_customer_dinner_fails_closed_even_with_a_recognized_dinner_program(raw_conn):
    """The explicit case this stage's own review called out: the old
    schema technically permitted "customer_dinner" + "galaxy", but that
    combination is not confirmed to represent a real historical event --
    refused rather than assumed legitimate."""
    await _insert_raw(raw_conn, _old_format_row("e14", engagement_type="customer_dinner", dinner_program="galaxy"))

    with pytest.raises(UnjustifiedLegacyEngagementError):
        await migrate_legacy_dinner_taxonomy_rows(raw_conn)


async def test_customer_dinner_fails_closed_even_with_no_dinner_program_at_all(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e15", engagement_type="customer_dinner", dinner_program=None))

    with pytest.raises(UnjustifiedLegacyEngagementError):
        await migrate_legacy_dinner_taxonomy_rows(raw_conn)


async def test_customer_dinner_fails_closed_even_with_the_exact_known_real_dinner_program(raw_conn):
    """Even "supernova" (the exact program name in the one confirmed real
    row) does not rescue a customer_dinner row -- engagement_type is what
    this migration refuses to trust here, not the program name."""
    await _insert_raw(raw_conn, _old_format_row("e16", engagement_type="customer_dinner", dinner_program="supernova"))

    with pytest.raises(UnjustifiedLegacyEngagementError):
        await migrate_legacy_dinner_taxonomy_rows(raw_conn)


async def test_investor_dinner_with_unrecognized_dinner_program_fails_closed(raw_conn):
    await _insert_raw(
        raw_conn, _old_format_row("e17", engagement_type="investor_dinner", dinner_program="moonlight-gala")
    )

    with pytest.raises(UnjustifiedLegacyEngagementError):
        await migrate_legacy_dinner_taxonomy_rows(raw_conn)


async def test_an_unjustified_row_blocks_writing_even_a_different_justified_row_in_the_same_pass(raw_conn):
    """Fail-closed applies to the whole migration pass, not just the one
    bad row -- if ANY row can't be trusted, nothing is written for ANY
    row this pass, so a partially-migrated database is never produced."""
    await _insert_raw(raw_conn, _old_format_row("e18", engagement_type="investor_dinner", dinner_program="supernova"))
    await _insert_raw(raw_conn, _old_format_row("e19", engagement_type="customer_dinner", dinner_program="galaxy"))

    with pytest.raises(UnjustifiedLegacyEngagementError):
        await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    # e18 (the justified row) was never written, even though it would
    # have migrated cleanly on its own.
    data = await _raw_data(raw_conn, "e18")
    assert data["engagement_type"] == "investor_dinner"
    assert data["dinner_program"] == "supernova"


# =====================================================================
# Already-new records / non-Engagement-shaped rows are left alone
# =====================================================================


async def test_already_new_format_record_is_left_completely_unchanged(raw_conn):
    new_row = {
        "engagement_id": "e7",
        "client_id": "c1",
        "title": "Fall Sponsorship",
        "engagement_type": "sponsorship",
        "dinner_type": None,
        "engagement_date": None,
        "location": None,
        "status": "planned",
        "owner": None,
        "fee": None,
        "contract_status": "not_sent",
        "contract_url": None,
        "signed_date": None,
        "payment_status": "unpaid",
        "luma_event_id": None,
        "created_at": NOW,
        "updated_at": NOW,
        "archived": False,
    }
    await _insert_raw(raw_conn, new_row)

    migrated_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    assert migrated_count == 0
    data = await _raw_data(raw_conn, "e7")
    assert data == new_row


async def test_new_format_dinner_record_with_a_real_dinner_type_is_left_unchanged(raw_conn):
    new_row = _old_format_row("e8", engagement_type="dinner", dinner_program="not_set")
    new_row["dinner_type"] = "fireside_dinner"
    await _insert_raw(raw_conn, new_row)

    migrated_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    assert migrated_count == 0
    data = await _raw_data(raw_conn, "e8")
    assert data["dinner_type"] == "fireside_dinner"


# =====================================================================
# Idempotency
# =====================================================================


async def test_migration_is_idempotent_second_run_migrates_nothing(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e9", engagement_type="investor_dinner", dinner_program="supernova"))

    first_run_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)
    second_run_count = await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    assert first_run_count == 1
    assert second_run_count == 0


async def test_migration_second_run_does_not_further_alter_an_already_migrated_row(raw_conn):
    await _insert_raw(raw_conn, _old_format_row("e10", engagement_type="investor_dinner", dinner_program="supernova"))
    await migrate_legacy_dinner_taxonomy_rows(raw_conn)
    after_first_run = await _raw_data(raw_conn, "e10")

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)
    after_second_run = await _raw_data(raw_conn, "e10")

    assert after_first_run == after_second_run


# =====================================================================
# Every non-taxonomy field is preserved exactly
# =====================================================================


async def test_all_non_taxonomy_fields_are_preserved_exactly(raw_conn):
    original = _old_format_row(
        "e11",
        engagement_type="investor_dinner",
        dinner_program="supernova",
        client_id="hive-client-id",
        title="SF Investor Dinner",
        engagement_date="2026-09-22",
        location="The Battery, San Francisco",
        status="confirmed",
        owner="Chris",
        fee=5000.0,
        contract_status="signed",
        contract_url="https://drive.example.com/contract",
        signed_date="2026-09-01",
        payment_status="partial",
        luma_event_id="luma-123",
        created_at="2026-09-05T10:00:00+00:00",
        updated_at="2026-09-05T10:00:00+00:00",
        archived=False,
    )
    await _insert_raw(raw_conn, original)

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    data = await _raw_data(raw_conn, "e11")
    unchanged_fields = [
        "engagement_id", "client_id", "title", "engagement_date", "location", "status", "owner", "fee",
        "contract_status", "contract_url", "signed_date", "payment_status", "luma_event_id",
        "created_at", "updated_at", "archived",
    ]
    for field in unchanged_fields:
        assert data[field] == original[field], f"{field} must be preserved exactly"
    # updated_at SQL column also untouched -- only the `data` blob is written.
    cursor = await raw_conn.execute("SELECT updated_at FROM engagements WHERE engagement_id = ?", ("e11",))
    row = await cursor.fetchone()
    await cursor.close()
    assert row["updated_at"] == original["updated_at"]


# =====================================================================
# Narrow scope -- never touches any other table
# =====================================================================


async def test_migration_never_touches_an_unrelated_table_on_the_same_connection(raw_conn):
    await raw_conn.execute("CREATE TABLE clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    await raw_conn.execute("INSERT INTO clients (client_id, data) VALUES (?, ?)", ("c1", '{"name": "Hive ASMBLD"}'))
    await raw_conn.commit()
    await _insert_raw(raw_conn, _old_format_row("e12", engagement_type="investor_dinner", dinner_program="supernova"))

    await migrate_legacy_dinner_taxonomy_rows(raw_conn)

    cursor = await raw_conn.execute("SELECT data FROM clients WHERE client_id = ?", ("c1",))
    row = await cursor.fetchone()
    await cursor.close()
    assert json.loads(row["data"]) == {"name": "Hive ASMBLD"}


# =====================================================================
# Full integration: SQLiteEngagementStore.connect() runs the migration
# automatically, before the store is usable for reads.
# =====================================================================


async def test_store_connect_migrates_legacy_rows_before_any_read(tmp_path):
    db_path = str(tmp_path / "engagements.db")

    # Seed a Stage-1E-shaped row directly via raw SQL (simulating a real
    # production database file from before this deploy).
    seed_conn = await aiosqlite.connect(db_path)
    seed_conn.row_factory = aiosqlite.Row
    await seed_conn.execute(CREATE_TABLE_SQL)
    await seed_conn.execute(CREATE_CLIENT_INDEX_SQL)
    await seed_conn.commit()
    await _insert_raw(
        seed_conn,
        _old_format_row(
            "5f89a109-3192-40a2-97d4-040828fc52b1",
            engagement_type="investor_dinner",
            dinner_program="supernova",
            client_id="hive-client-id",
        ),
    )
    await seed_conn.close()

    # Now open it the same way the real app does -- through the store.
    store = SQLiteEngagementStore(db_path)
    await store.connect()
    try:
        engagement = await store.get("5f89a109-3192-40a2-97d4-040828fc52b1")
        assert engagement is not None
        assert engagement.engagement_type == "dinner"
        assert engagement.dinner_type == "investor_dinner"
        assert engagement.title == "SF Investor Dinner"
        assert engagement.client_id == "hive-client-id"
        assert engagement.fee == 5000.0
        assert engagement.contract_status == "signed"
        assert engagement.luma_event_id == "luma-123"

        engagements = await store.list_for_client("hive-client-id")
        assert len(engagements) == 1
        assert engagements[0].engagement_id == "5f89a109-3192-40a2-97d4-040828fc52b1"
    finally:
        await store.close()


async def test_store_connect_migration_is_idempotent_across_restarts(tmp_path):
    db_path = str(tmp_path / "engagements.db")

    store1 = SQLiteEngagementStore(db_path)
    await store1.connect()
    now = datetime.now(timezone.utc)
    await store1._connection.execute(
        "INSERT INTO engagements (engagement_id, client_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
        (
            "e13",
            "c1",
            now.isoformat(),
            now.isoformat(),
            json.dumps(_old_format_row("e13", engagement_type="investor_dinner", dinner_program="supernova")),
        ),
    )
    await store1._connection.commit()
    # Migration already ran once at store1.connect() (before this manual
    # insert), so this row is untouched by that first pass -- simulate a
    # second app restart to confirm the SECOND pass now correctly migrates
    # it, and a third confirms nothing further changes.
    await store1.close()

    store2 = SQLiteEngagementStore(db_path)
    await store2.connect()
    engagement = await store2.get("e13")
    assert engagement.engagement_type == "dinner"
    assert engagement.dinner_type == "investor_dinner"
    await store2.close()

    store3 = SQLiteEngagementStore(db_path)
    await store3.connect()
    engagement_again = await store3.get("e13")
    assert engagement_again.engagement_type == "dinner"
    assert engagement_again.dinner_type == "investor_dinner"
    await store3.close()


async def test_store_connect_raises_on_an_unjustified_legacy_row_rather_than_booting(tmp_path):
    """Requirement: failure occurs before the application begins serving
    requests. SQLiteEngagementStore.connect() is called during app
    startup (app/main.py's lifespan, before Uvicorn accepts connections)
    with no surrounding try/except -- an exception here propagates all
    the way out and aborts startup entirely, rather than the app booting
    successfully with a row this migration refused to guess at."""
    db_path = str(tmp_path / "engagements.db")
    seed_conn = await aiosqlite.connect(db_path)
    seed_conn.row_factory = aiosqlite.Row
    await seed_conn.execute(CREATE_TABLE_SQL)
    await seed_conn.execute(CREATE_CLIENT_INDEX_SQL)
    await seed_conn.commit()
    await _insert_raw(seed_conn, _old_format_row("e20", engagement_type="customer_dinner", dinner_program="galaxy"))
    await seed_conn.close()

    store = SQLiteEngagementStore(db_path)
    with pytest.raises(UnjustifiedLegacyEngagementError):
        await store.connect()
