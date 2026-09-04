"""
Trigger feature -- Stage 5A foundation tests (2026-09-04).

Stage 5A is deliberately execution-inert: it adds durable schema
(MailCampaign.lead_start_mode/execution_active_since, MailLeadStartTrigger,
MailTriggerOccurrence, MailTriggerOccurrenceMember) and persistence only.
This file proves:
  - the new model validation helper behaves correctly;
  - the SQLite trigger store round-trips correctly;
  - the SQLite occurrence store's uniqueness constraints and its ONE-
    connection/atomic-freeze-transaction guarantee actually hold, using a
    real sqlite file (same tmp_path-backed convention as
    test_sqlite_mail_campaign_csv_prospect_link_store.py);
  - nothing anywhere -- service or API -- creates, reads, or executes a
    trigger/occurrence yet.

See mail_trigger_occurrence_store.py's own module docstring for why
occurrences and members are owned by ONE store/ONE connection rather than
two separate stores.
"""

from datetime import datetime, time, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.mail import (
    MailLeadStartTrigger,
    MailTriggerOccurrence,
    validate_lead_start_trigger,
)
from app.repositories.mail_lead_start_trigger_store import (
    MailLeadStartTriggerNotFoundError,
    MemoryMailLeadStartTriggerStore,
)
from app.repositories.mail_trigger_occurrence_store import MemoryMailTriggerOccurrenceStore
from app.repositories.sqlite_mail_lead_start_trigger_store import SQLiteMailLeadStartTriggerStore
from app.repositories.sqlite_mail_trigger_occurrence_store import SQLiteMailTriggerOccurrenceStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
SLOT_A = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
SLOT_B = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)


def _trigger(trigger_id="t1", mail_campaign_id="c1", weekdays=None, leads_to_start=20, enabled=True) -> MailLeadStartTrigger:
    return MailLeadStartTrigger(
        trigger_id=trigger_id,
        mail_campaign_id=mail_campaign_id,
        weekdays=weekdays if weekdays is not None else [0, 1, 2, 3, 4],
        local_time=time(9, 0),
        leads_to_start=leads_to_start,
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _occurrence(trigger_id="t1", mail_campaign_id="c1", scheduled_for=SLOT_A, target_count=20) -> MailTriggerOccurrence:
    return MailTriggerOccurrence(
        trigger_id=trigger_id, mail_campaign_id=mail_campaign_id, scheduled_for=scheduled_for,
        target_count=target_count, created_at=NOW,
    )


# --- validate_lead_start_trigger ------------------------------------------


def test_validate_accepts_valid_weekdays_and_count():
    validate_lead_start_trigger([0, 1, 2, 3, 4], 20)  # must not raise


def test_validate_rejects_empty_weekdays():
    with pytest.raises(ValueError):
        validate_lead_start_trigger([], 20)


def test_validate_rejects_out_of_range_weekday():
    with pytest.raises(ValueError):
        validate_lead_start_trigger([0, 7], 20)
    with pytest.raises(ValueError):
        validate_lead_start_trigger([-1, 2], 20)


def test_validate_rejects_duplicate_weekdays():
    with pytest.raises(ValueError):
        validate_lead_start_trigger([0, 0, 1], 20)


def test_validate_rejects_non_positive_leads_to_start():
    with pytest.raises(ValueError):
        validate_lead_start_trigger([0], 0)
    with pytest.raises(ValueError):
        validate_lead_start_trigger([0], -5)


# --- MailLeadStartTrigger store (Memory + SQLite) --------------------------


@pytest_asyncio.fixture
async def trigger_store(tmp_path):
    s = SQLiteMailLeadStartTriggerStore(str(tmp_path / "triggers.db"))
    await s.connect()
    yield s
    await s.close()


async def test_memory_trigger_store_create_and_get():
    store = MemoryMailLeadStartTriggerStore()
    await store.create(_trigger())
    got = await store.get("t1")
    assert got == _trigger()


async def test_sqlite_trigger_create_and_get_round_trips(trigger_store):
    await trigger_store.create(_trigger())
    got = await trigger_store.get("t1")
    assert got == _trigger()


async def test_sqlite_trigger_get_missing_returns_none(trigger_store):
    assert await trigger_store.get("does-not-exist") is None


async def test_sqlite_trigger_list_for_campaign_scoped_and_ordered(trigger_store):
    await trigger_store.create(_trigger(trigger_id="t1", mail_campaign_id="c1"))
    await trigger_store.create(_trigger(trigger_id="t2", mail_campaign_id="c1"))
    await trigger_store.create(_trigger(trigger_id="t3", mail_campaign_id="c2"))

    c1_triggers = await trigger_store.list_for_campaign("c1")
    assert {t.trigger_id for t in c1_triggers} == {"t1", "t2"}
    assert await trigger_store.list_for_campaign("c3") == []


async def test_sqlite_trigger_save_updates_fields(trigger_store):
    await trigger_store.create(_trigger())
    updated = _trigger().model_copy(update={"leads_to_start": 5, "enabled": False, "weekdays": [5, 6]})
    await trigger_store.save(updated)

    got = await trigger_store.get("t1")
    assert got.leads_to_start == 5
    assert got.enabled is False
    assert got.weekdays == [5, 6]


async def test_sqlite_trigger_save_missing_raises(trigger_store):
    with pytest.raises(MailLeadStartTriggerNotFoundError):
        await trigger_store.save(_trigger())


async def test_sqlite_trigger_delete_removes_and_is_noop_if_missing(trigger_store):
    await trigger_store.create(_trigger())
    await trigger_store.delete("t1")
    assert await trigger_store.get("t1") is None
    await trigger_store.delete("t1")  # no-op, must not raise


async def test_sqlite_trigger_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "triggers.db")
    store1 = SQLiteMailLeadStartTriggerStore(db_path)
    await store1.connect()
    await store1.create(_trigger())
    await store1.close()

    store2 = SQLiteMailLeadStartTriggerStore(db_path)
    await store2.connect()
    got = await store2.get("t1")
    assert got == _trigger()
    await store2.close()


# --- MailTriggerOccurrence / Member store (SQLite) -------------------------


@pytest_asyncio.fixture
async def occurrence_store(tmp_path):
    s = SQLiteMailTriggerOccurrenceStore(str(tmp_path / "occurrences.db"))
    await s.connect()
    yield s
    await s.close()


async def test_memory_occurrence_store_matches_sqlite_contract():
    """Sanity that the in-memory stand-in enforces the SAME uniqueness
    contract, so tests written against it exercise real behavior."""
    store = MemoryMailTriggerOccurrenceStore()
    assert await store.create_occurrence(_occurrence()) is True
    assert await store.create_occurrence(_occurrence()) is False
    assert await store.freeze_members("t1", SLOT_A, ["e1", "e2"], NOW) is True
    with pytest.raises(ValueError):
        await store.freeze_members("t-missing", SLOT_A, ["e3"], NOW)


async def test_create_occurrence_identity_is_trigger_and_scheduled_for(occurrence_store):
    assert await occurrence_store.create_occurrence(_occurrence(trigger_id="t1", scheduled_for=SLOT_A)) is True
    # Same (trigger_id, scheduled_for) -- the composite identity -- refused.
    assert await occurrence_store.create_occurrence(_occurrence(trigger_id="t1", scheduled_for=SLOT_A, target_count=999)) is False
    # A DIFFERENT scheduled_for for the same trigger is a genuinely different occurrence.
    assert await occurrence_store.create_occurrence(_occurrence(trigger_id="t1", scheduled_for=SLOT_B)) is True
    # A DIFFERENT trigger at the SAME scheduled_for is also genuinely different.
    assert await occurrence_store.create_occurrence(_occurrence(trigger_id="t2", scheduled_for=SLOT_A)) is True


async def test_get_occurrence_returns_none_before_creation(occurrence_store):
    assert await occurrence_store.get_occurrence("t1", SLOT_A) is None
    await occurrence_store.create_occurrence(_occurrence())
    got = await occurrence_store.get_occurrence("t1", SLOT_A)
    assert got is not None
    assert got.status == "PREPARING"
    assert got.frozen_at is None
    assert got.target_count == 20


async def test_freeze_members_requires_an_existing_occurrence(occurrence_store):
    with pytest.raises(ValueError):
        await occurrence_store.freeze_members("t1", SLOT_A, ["e1"], NOW)


async def test_freeze_members_stamps_frozen_at_and_inserts_pending_reconcile_members(occurrence_store):
    await occurrence_store.create_occurrence(_occurrence())
    result = await occurrence_store.freeze_members("t1", SLOT_A, ["e1", "e2", "e3"], NOW)
    assert result is True

    occurrence = await occurrence_store.get_occurrence("t1", SLOT_A)
    assert occurrence.frozen_at == NOW

    members = await occurrence_store.list_members("t1", SLOT_A)
    assert {m.enrollment_id for m in members} == {"e1", "e2", "e3"}
    assert all(m.outcome == "PENDING_RECONCILE" for m in members)
    assert all(m.reconciled_at is None for m in members)


async def test_freeze_members_is_a_true_no_op_the_second_time(occurrence_store):
    """A resumed occurrence (crash after freeze committed) must not
    re-freeze with a different candidate list -- the SECOND call changes
    nothing at all, proving the frozen cohort really is exactly-once."""
    await occurrence_store.create_occurrence(_occurrence())
    await occurrence_store.freeze_members("t1", SLOT_A, ["e1", "e2"], NOW)

    later = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
    result = await occurrence_store.freeze_members("t1", SLOT_A, ["e3", "e4", "e5"], later)
    assert result is False

    occurrence = await occurrence_store.get_occurrence("t1", SLOT_A)
    assert occurrence.frozen_at == NOW  # unchanged -- NOT overwritten with `later`

    members = await occurrence_store.list_members("t1", SLOT_A)
    assert {m.enrollment_id for m in members} == {"e1", "e2"}  # unchanged -- e3/e4/e5 never inserted


async def test_zero_eligible_candidates_still_stamps_frozen_at_distinctly_from_not_yet_frozen(occurrence_store):
    await occurrence_store.create_occurrence(_occurrence())
    result = await occurrence_store.freeze_members("t1", SLOT_A, [], NOW)
    assert result is True

    occurrence = await occurrence_store.get_occurrence("t1", SLOT_A)
    assert occurrence.frozen_at is not None  # frozen...
    assert await occurrence_store.list_members("t1", SLOT_A) == []  # ...with zero eligible members


async def test_global_unique_enrollment_id_across_different_occurrences(occurrence_store):
    """Once an enrollment is frozen into ANY occurrence's cohort, it can
    never be frozen into a different one -- the actual mechanism behind
    the 'a lead can only ever belong to one Trigger occurrence' invariant."""
    await occurrence_store.create_occurrence(_occurrence(trigger_id="t1", scheduled_for=SLOT_A))
    await occurrence_store.create_occurrence(_occurrence(trigger_id="t2", scheduled_for=SLOT_A))

    await occurrence_store.freeze_members("t1", SLOT_A, ["shared-enrollment", "only-in-a"], NOW)
    # t2's freeze still succeeds (it claims its OWN frozen_at)...
    result = await occurrence_store.freeze_members("t2", SLOT_A, ["shared-enrollment", "only-in-b"], NOW)
    assert result is True

    occurrence_t2 = await occurrence_store.get_occurrence("t2", SLOT_A)
    assert occurrence_t2.frozen_at is not None

    # ...but "shared-enrollment" is silently EXCLUDED from t2's cohort --
    # it already belongs to t1's. t2 ends up with fewer than requested,
    # by design (see the approved report's V1 "no refill loop" decision).
    members_t1 = {m.enrollment_id for m in await occurrence_store.list_members("t1", SLOT_A)}
    members_t2 = {m.enrollment_id for m in await occurrence_store.list_members("t2", SLOT_A)}
    assert members_t1 == {"shared-enrollment", "only-in-a"}
    assert members_t2 == {"only-in-b"}  # NOT {"shared-enrollment", "only-in-b"}


async def test_occurrence_and_member_tables_share_one_connection(occurrence_store):
    """Structural proof of the atomicity precondition: both tables really
    are reachable through the SAME aiosqlite.Connection object, not two."""
    assert isinstance(occurrence_store._connection, aiosqlite.Connection)
    tables = {
        row[0]
        for row in await (await occurrence_store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    }
    assert {"mail_trigger_occurrences", "mail_trigger_occurrence_members"} <= tables


async def test_freeze_members_rolls_back_completely_on_a_mid_transaction_failure(occurrence_store, monkeypatch):
    """The crash-safety property Stage 5D depends on: if ANYTHING fails
    partway through freeze_members()'s multi-statement write, NOTHING from
    that attempt persists -- not the frozen_at stamp, not any member row --
    and the connection is left perfectly healthy for a later, successful
    retry (the exact incident sqlite_txn.py's sqlite_write() exists to
    prevent -- see that module's own docstring)."""
    await occurrence_store.create_occurrence(_occurrence())

    real_execute = occurrence_store._connection.execute
    call_count = {"n": 0}

    async def flaky_execute(sql, params=()):
        call_count["n"] += 1
        # Let the frozen_at UPDATE (1st) and the first member INSERT (2nd)
        # through, then fail on the second member INSERT (3rd call).
        if call_count["n"] == 3:
            raise RuntimeError("simulated mid-transaction failure")
        return await real_execute(sql, params)

    monkeypatch.setattr(occurrence_store._connection, "execute", flaky_execute)
    with pytest.raises(RuntimeError):
        await occurrence_store.freeze_members("t1", SLOT_A, ["e1", "e2", "e3"], NOW)
    monkeypatch.undo()

    occurrence = await occurrence_store.get_occurrence("t1", SLOT_A)
    assert occurrence.frozen_at is None  # rolled back, not partially set
    assert await occurrence_store.list_members("t1", SLOT_A) == []  # rolled back, not "just e1"

    # And the connection itself is still healthy -- no stale open
    # transaction left behind (sqlite_write's own rollback() guarantee) --
    # a subsequent, un-flaky freeze on the SAME occurrence succeeds cleanly.
    result = await occurrence_store.freeze_members("t1", SLOT_A, ["e1", "e2", "e3"], NOW)
    assert result is True
    assert {m.enrollment_id for m in await occurrence_store.list_members("t1", SLOT_A)} == {"e1", "e2", "e3"}


async def test_occurrence_and_member_persistence_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "occurrences.db")
    store1 = SQLiteMailTriggerOccurrenceStore(db_path)
    await store1.connect()
    await store1.create_occurrence(_occurrence())
    await store1.freeze_members("t1", SLOT_A, ["e1", "e2"], NOW)
    await store1.close()

    store2 = SQLiteMailTriggerOccurrenceStore(db_path)
    await store2.connect()
    occurrence = await store2.get_occurrence("t1", SLOT_A)
    assert occurrence is not None
    assert occurrence.frozen_at == NOW
    members = {m.enrollment_id for m in await store2.list_members("t1", SLOT_A)}
    assert members == {"e1", "e2"}
    await store2.close()


# --- No Trigger execution/CRUD exists yet ----------------------------------


async def test_mail_campaign_service_has_no_trigger_methods_yet():
    """Stage 5A is schema/persistence only -- confirms no CRUD or
    execution method has been added to the service layer."""
    from app.services.mail_campaign_service import MailCampaignService

    for forbidden in ("create_trigger", "list_triggers", "update_trigger", "delete_trigger", "process_trigger_occurrences"):
        assert not hasattr(MailCampaignService, forbidden), f"MailCampaignService must not have {forbidden}() yet"


def test_trigger_crud_routes_registered_but_no_broader_admin_route_exists():
    """Superseded by Stage 5D (2026-09-04), which approved exactly four
    campaign-scoped Trigger CRUD routes -- see
    tests/test_mail_trigger_occurrence_execution.py for the full Stage 5D
    API-behavior suite. What still matters, and is checked here now: no
    OTHER trigger-shaped route exists (no bulk/admin/execution-trigger
    endpoint) -- exactly the four approved paths, nothing broader.

    Uses the resolved OpenAPI schema, not raw app.routes iteration --
    this FastAPI version wraps included sub-routers in an internal
    _IncludedRouter object with no bare `.path` attribute, so a naive
    `hasattr(route, "path")` walk silently finds nothing at all (which is
    exactly why the ORIGINAL Stage 5A version of this test -- asserting
    "no trigger route exists yet" -- passed vacuously even before any
    route existed, not because it genuinely detected absence)."""
    from app.main import app

    schema = app.openapi()
    trigger_paths = {path: set(ops.keys()) for path, ops in schema["paths"].items() if "trigger" in path.lower()}
    assert trigger_paths == {
        "/mail/campaigns/{mail_campaign_id}/triggers": {"get", "post"},
        "/mail/campaigns/{mail_campaign_id}/triggers/{trigger_id}": {"patch", "delete"},
    }


def test_mail_execution_worker_delegates_trigger_processing_rather_than_reimplementing_it():
    """Superseded by Stage 5D (2026-09-04), which approved wiring Trigger
    occurrence processing into MailExecutionWorker.tick() -- this file's
    original assertion ("no trigger-shaped code at all") was Stage 5A's
    own scope guard, correct only through Stage 5C. What still matters,
    and is checked here now: the worker DELEGATES to MailTriggerService
    rather than reimplementing occurrence discovery/freeze/reconciliation
    inline -- see tests/test_mail_trigger_occurrence_execution.py for the
    full Stage 5D behavioral suite."""
    from pathlib import Path

    source = Path("app/services/mail_execution_worker.py").read_text()
    assert "mail_trigger_service" in source
    for forbidden in ("freeze_members(", "MailTriggerOccurrence(", "create_occurrence("):
        assert forbidden not in source
