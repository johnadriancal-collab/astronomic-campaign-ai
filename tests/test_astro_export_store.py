"""
AstroExportStore -- in-memory, 15-minute-TTL holding area for Astro AI's
export_crm_contacts CSV bytes. Mirrors MailboxService's `_pending_states`
OAuth-state pattern; TTL is exercised the same way that suite does
(directly backdating `created_at`, never sleeping in a test).
"""

from datetime import datetime, timedelta, timezone

from app.services.astro_export_store import EXPORT_TTL, AstroExportStore


def test_put_then_get_returns_the_stored_export():
    store = AstroExportStore()
    export_id = store.put(filename="foo.csv", contact_count=3, csv_bytes=b"a,b\r\n1,2")
    export = store.get(export_id)
    assert export is not None
    assert export.filename == "foo.csv"
    assert export.contact_count == 3
    assert export.csv_bytes == b"a,b\r\n1,2"


def test_unknown_export_id_returns_none():
    store = AstroExportStore()
    assert store.get("does-not-exist") is None


def test_get_does_not_consume_the_export_multiple_reads_allowed():
    """Unlike MailboxService's single-use OAuth state, a download link may
    reasonably be clicked more than once within the TTL window."""
    store = AstroExportStore()
    export_id = store.put(filename="foo.csv", contact_count=1, csv_bytes=b"x")
    first = store.get(export_id)
    second = store.get(export_id)
    assert first is not None
    assert second is not None
    assert first.csv_bytes == second.csv_bytes


def test_export_expires_after_the_ttl():
    store = AstroExportStore()
    export_id = store.put(filename="foo.csv", contact_count=1, csv_bytes=b"x")
    store._pending[export_id].created_at = datetime.now(timezone.utc) - EXPORT_TTL - timedelta(seconds=1)
    assert store.get(export_id) is None


def test_export_still_available_just_under_the_ttl():
    store = AstroExportStore()
    export_id = store.put(filename="foo.csv", contact_count=1, csv_bytes=b"x")
    store._pending[export_id].created_at = datetime.now(timezone.utc) - EXPORT_TTL + timedelta(seconds=5)
    assert store.get(export_id) is not None


def test_two_exports_have_independent_ids_and_data():
    store = AstroExportStore()
    id_a = store.put(filename="a.csv", contact_count=1, csv_bytes=b"A")
    id_b = store.put(filename="b.csv", contact_count=2, csv_bytes=b"B")
    assert id_a != id_b
    assert store.get(id_a).csv_bytes == b"A"
    assert store.get(id_b).csv_bytes == b"B"


def test_pruning_an_expired_export_does_not_affect_a_fresh_one():
    store = AstroExportStore()
    stale_id = store.put(filename="stale.csv", contact_count=1, csv_bytes=b"old")
    store._pending[stale_id].created_at = datetime.now(timezone.utc) - EXPORT_TTL - timedelta(seconds=1)
    fresh_id = store.put(filename="fresh.csv", contact_count=1, csv_bytes=b"new")

    assert store.get(stale_id) is None
    assert store.get(fresh_id) is not None
