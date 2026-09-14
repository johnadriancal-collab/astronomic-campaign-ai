"""
Stage 6C (2026-09-14) -- historical Luma location reconciliation driver.
Exercised directly against Memory stores; NEVER touches production.
Structural cohort tests exercise the REAL, approved FROZEN_COHORT; all
other logic tests monkeypatch a small, controlled test cohort so the
mechanics (drift, idempotency, field restriction, provenance) can be
proven without depending on real production IDs.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaEvent, LumaRegistration
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services import luma_location_historical_reconciliation as reconciliation_module
from app.services.activity_log_service import ActivityLogService
from app.services.luma_contact_location_enrichment import FIELD_PROVENANCE_KEY
from app.services.luma_location_historical_cohort import (
    BLOCKED_CONTACT_ID_NOT_IN_COHORT,
    FROZEN_COHORT,
    FROZEN_COHORT_FIELD_TOTALS,
    FROZEN_COHORT_SIZE,
    FrozenReconciliationRow,
)
from app.services.luma_location_historical_reconciliation import (
    RowOutcome,
    apply_frozen_cohort_reconciliation,
    compute_dry_run_report,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _contact(crm_contact_id="contact-1", **overrides) -> CrmContact:
    return CrmContact(crm_contact_id=crm_contact_id, created_at=NOW, updated_at=NOW, **overrides)


def _event(luma_event_id="evt-1", **overrides) -> LumaEvent:
    defaults = dict(name="Test Dinner", synced_at=NOW, updated_at=NOW)
    defaults.update(overrides)
    return LumaEvent(luma_event_id=luma_event_id, **defaults)


def _registration(luma_guest_id="gst-1", luma_event_id="evt-1", crm_contact_id="contact-1", **overrides) -> LumaRegistration:
    defaults = dict(approval_status=LumaApprovalStatus.APPROVED, registered_at=NOW, synced_at=NOW, updated_at=NOW)
    defaults.update(overrides)
    return LumaRegistration(luma_guest_id=luma_guest_id, luma_event_id=luma_event_id, crm_contact_id=crm_contact_id, **defaults)


@pytest.fixture
def stores():
    contact_store = MemoryCrmContactStore()
    registration_store = MemoryLumaRegistrationStore()
    event_store = MemoryLumaEventStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return contact_store, registration_store, event_store, activity_log


TEST_ROW = FrozenReconciliationRow(
    crm_contact_id="contact-1",
    luma_guest_id="gst-1",
    luma_event_id="evt-1",
    engagement_id="eng-1",
    fields=("city", "state", "country"),
    expected_values={"city": "Austin", "state": "Texas", "country": "United States"},
)

TEST_ROW_PARTIAL_FIELDS = FrozenReconciliationRow(
    crm_contact_id="contact-2",
    luma_guest_id="gst-2",
    luma_event_id="evt-1",
    engagement_id="eng-1",
    fields=("state", "country"),  # deliberately excludes city
    expected_values={"state": "Texas", "country": "United States"},
)


def _patch_cohort(monkeypatch, rows):
    monkeypatch.setattr(reconciliation_module, "FROZEN_COHORT", tuple(rows))


# =====================================================================
# Structural cohort tests -- against the REAL, approved FROZEN_COHORT
# =====================================================================


def test_frozen_cohort_has_exactly_17_rows():
    assert len(FROZEN_COHORT) == 17 == FROZEN_COHORT_SIZE


def test_frozen_cohort_field_totals_match_approved_counts():
    totals = {"city": 0, "state": 0, "country": 0}
    for row in FROZEN_COHORT:
        for f in row.fields:
            totals[f] += 1
    assert totals == FROZEN_COHORT_FIELD_TOTALS == {"city": 16, "state": 17, "country": 17}


def test_blocked_contact_id_is_not_in_frozen_cohort():
    ids = {row.crm_contact_id for row in FROZEN_COHORT}
    assert BLOCKED_CONTACT_ID_NOT_IN_COHORT not in ids
    assert BLOCKED_CONTACT_ID_NOT_IN_COHORT == "155132ee-cc78-4de7-b3fb-601f8d8df344"


def test_cohort_module_has_no_dynamic_scan_capability():
    """Structural guard: the cohort DATA module itself must never import
    any repository/store class -- proving it cannot possibly query
    production data to expand itself. Only luma_location_historical_reconciliation.py
    (a SEPARATE module) is allowed to query stores, and only against this
    fixed list."""
    import pathlib

    source = pathlib.Path("app/services/luma_location_historical_cohort.py").read_text()
    for forbidden in ("Store", "import asyncio", "aiosqlite"):
        assert forbidden not in source


# =====================================================================
# Dry run: zero writes
# =====================================================================


async def test_dry_run_makes_zero_writes(stores, monkeypatch):
    contact_store, registration_store, event_store, _al = stores
    _patch_cohort(monkeypatch, [TEST_ROW])
    await contact_store.create(_contact())
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    report = await compute_dry_run_report(contact_store, registration_store, event_store)

    assert report.counts.eligible_now == 1
    contact = await contact_store.get("contact-1")
    assert contact.city is None  # completely untouched -- dry run never writes
    assert contact.custom_fields == {}


# =====================================================================
# CLI: both confirmation gates required
# =====================================================================


def test_cli_write_without_confirm_flag_refuses(monkeypatch, capsys):
    from scripts import run_luma_location_historical_reconciliation as cli

    monkeypatch.setattr("sys.argv", ["prog", "--write"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


def test_cli_confirm_without_write_flag_refuses(monkeypatch):
    from scripts import run_luma_location_historical_reconciliation as cli

    monkeypatch.setattr("sys.argv", ["prog", "--confirm-production-writes"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


# =====================================================================
# Write path: only frozen IDs, only approved fields, never overwrites
# =====================================================================


async def test_write_only_touches_frozen_ids(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])  # only contact-1 is frozen

    await contact_store.create(_contact("contact-1"))
    await contact_store.create(_contact("contact-not-frozen"))  # NOT in cohort -- must never change
    await registration_store.save(_registration())
    await registration_store.save(_registration(luma_guest_id="gst-x", crm_contact_id="contact-not-frozen"))
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    assert (await contact_store.get("contact-1")).city == "Austin"
    assert (await contact_store.get("contact-not-frozen")).city is None


async def test_write_only_fills_approved_fields(stores, monkeypatch):
    """TEST_ROW_PARTIAL_FIELDS approves only state/country -- city must
    stay blank even though the event has city data and the Contact's
    city is ALSO blank."""
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW_PARTIAL_FIELDS])

    await contact_store.create(_contact("contact-2"))
    await registration_store.save(_registration(luma_guest_id="gst-2", crm_contact_id="contact-2"))
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    contact = await contact_store.get("contact-2")
    assert contact.city is None  # NOT approved for this row -- never filled
    assert contact.state == "Texas"
    assert contact.country == "United States"


async def test_nonblank_fields_are_preserved(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact("contact-1", city="San Francisco"))  # already set -- must be preserved
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    contact = await contact_store.get("contact-1")
    assert contact.city == "San Francisco"  # preserved, never overwritten
    assert contact.state == "Texas"
    assert contact.country == "United States"


async def test_changed_fields_receive_standard_stage6b_provenance(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    contact = await contact_store.get("contact-1")
    provenance = contact.custom_fields[FIELD_PROVENANCE_KEY]
    for f in ("city", "state", "country"):
        assert provenance[f]["source"] == "luma_event_location"
        assert provenance[f]["luma_event_id"] == "evt-1"
        assert provenance[f]["luma_guest_id"] == "gst-1"
        assert provenance[f]["engagement_id"] == "eng-1"


async def test_activity_is_structural_only(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    page = await activity_log.list_events()
    enriched = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert len(enriched) == 1
    # Matches the exact existing Stage 6B live-path convention (confirmed
    # against the real Henry Vo production acceptance case): the 3
    # location fields PLUS "custom:field_provenance" itself, since that
    # custom field also changed -- structural keys only, never values.
    assert set(enriched[0].metadata["fields_updated"]) == {"city", "state", "country", "custom:field_provenance"}
    assert "Austin" not in str(enriched[0].metadata)
    assert "Texas" not in str(enriched[0].metadata)


# =====================================================================
# Idempotency
# =====================================================================


async def test_second_execution_is_a_complete_no_op(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    first_report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)
    assert first_report.results[0].outcome == RowOutcome.FILLED
    contact_after_first = await contact_store.get("contact-1")
    updated_at_after_first = contact_after_first.updated_at

    second_report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)
    assert second_report.results[0].outcome == RowOutcome.ALREADY_SATISFIED

    contact_after_second = await contact_store.get("contact-1")
    assert contact_after_second.updated_at == updated_at_after_first  # zero further mutation

    page = await activity_log.list_events()
    enriched = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert len(enriched) == 1  # still just the one from the first run -- no second Activity entry


# =====================================================================
# Drift: skipped, never substituted
# =====================================================================


async def test_stale_drifted_row_is_skipped_not_substituted(stores, monkeypatch):
    """Simulates drift: the registration's registered_at has become None
    since the cohort was frozen (e.g. some other process cleared it) --
    the row must be skipped, and nothing about it silently substituted
    with different data."""
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration(registered_at=None))  # drifted
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    assert report.results[0].outcome == RowOutcome.DRIFT_NOT_REGISTERED
    contact = await contact_store.get("contact-1")
    assert contact.city is None  # never written


async def test_drift_when_expected_value_no_longer_matches(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration())
    # Event geo now disagrees with the frozen expected_values ("Austin"/"Texas") --
    await event_store.save(_event(location_city="Denver", location_region="Colorado", location_country="US"))

    report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    assert report.results[0].outcome == RowOutcome.DRIFT_VALUE_CHANGED
    contact = await contact_store.get("contact-1")
    assert contact.city is None


async def test_drift_when_geo_unavailable(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])

    await contact_store.create(_contact())
    await registration_store.save(_registration())
    await event_store.save(_event())  # no structured geo at all

    report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    assert report.results[0].outcome == RowOutcome.DRIFT_GEO_UNAVAILABLE


async def test_drift_contact_missing(stores, monkeypatch):
    contact_store, registration_store, event_store, activity_log = stores
    _patch_cohort(monkeypatch, [TEST_ROW])
    # No Contact created at all.
    await registration_store.save(_registration())
    await event_store.save(_event(location_city="Austin", location_region="Texas", location_country="US"))

    report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)
    assert report.results[0].outcome == RowOutcome.DRIFT_CONTACT_MISSING


# =====================================================================
# The one blocked Contact is genuinely untouchable via this command
# =====================================================================


async def test_blocked_contact_cannot_be_changed_by_the_real_frozen_cohort(stores):
    """Uses the REAL, unpatched FROZEN_COHORT -- proves the blocked
    Contact (evt-hKRtbvrUo3tp1ND, zero structured geo) is not merely
    'skipped due to drift' but structurally ABSENT from the write
    universe entirely."""
    contact_store, registration_store, event_store, activity_log = stores
    await contact_store.create(_contact(BLOCKED_CONTACT_ID_NOT_IN_COHORT))

    report = await apply_frozen_cohort_reconciliation(contact_store, registration_store, event_store, activity_log)

    assert all(r.crm_contact_id != BLOCKED_CONTACT_ID_NOT_IN_COHORT for r in report.results)
    contact = await contact_store.get(BLOCKED_CONTACT_ID_NOT_IN_COHORT)
    assert contact.city is None
    assert contact.custom_fields == {}


# =====================================================================
# Ordinary Luma backfill paths remain unaffected
# =====================================================================


def test_ordinary_luma_backfill_module_never_imports_this_reconciliation_module():
    """Structural guard: luma_sync_service.py (the live webhook + backfill
    driver) must have no reference to this Stage 6C module at all -- its
    own existing allow_location_enrichment=False safeguard on
    run_backfill()/run_event_backfill() (Stage 6B's own review) remains
    the only thing gating historical writes there, completely
    independent of and unaffected by this new, separate operator
    command."""
    import pathlib

    source = pathlib.Path("app/services/luma_sync_service.py").read_text()
    assert "luma_location_historical_reconciliation" not in source
    assert "luma_location_historical_cohort" not in source
