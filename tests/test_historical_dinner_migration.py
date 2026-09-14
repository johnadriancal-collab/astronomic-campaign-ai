from datetime import datetime, timezone

import pytest

from app.models.client_crm import ClientStatus, DIRECT_EVENTS_CLIENT_NAME
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.historical_dinner_migration import apply_ready_rows, compute_dry_run_report
from app.services.historical_dinner_migration_manifest import MANIFEST_ROWS

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _service():
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    luma_event_store = MemoryLumaEventStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        engagement_store=engagement_store,
        engagement_closeout_store=engagement_closeout_store,
        engagement_participant_store=engagement_participant_store,
        luma_event_store=luma_event_store,
        client_touchpoint_store=client_touchpoint_store,
        contact_engagement_signal_service=signal_service,
    )
    return service, crm_contact_store


async def _seed_all_manifest_contacts(crm_contact_store: MemoryCrmContactStore) -> None:
    seen: set[str] = set()
    for row in MANIFEST_ROWS:
        for contact_id in row["contact_ids"]:
            if contact_id in seen:
                continue
            seen.add(contact_id)
            await crm_contact_store.create(
                CrmContact(
                    crm_contact_id=contact_id,
                    created_at=_now(),
                    updated_at=_now(),
                    first_name="Test",
                    last_name=contact_id[:8],
                )
            )


def _manifest_totals():
    ready = [r for r in MANIFEST_ROWS if r["bucket"] == "A_READY"]
    held = [r for r in MANIFEST_ROWS if r["bucket"] != "A_READY"]
    ready_contacts = sum(len(r["contact_ids"]) for r in ready)
    return ready, held, ready_contacts


async def test_manifest_is_fully_approved_all_rows_a_ready():
    buckets = {r["bucket"] for r in MANIFEST_ROWS}
    assert buckets == {"A_READY"}
    assert len(MANIFEST_ROWS) == 54


async def test_austin_forward_and_category_tags_never_appear_in_the_manifest():
    raw_values = {r["raw_value"] for r in MANIFEST_ROWS}
    assert "Austin Forward - 09.10.2026 - Austin" not in raw_values
    for tag in ("Investor Dinners", "Fireside Dinners", "Founder Dinners", "Biz Dev Dinners",
                "Regulus Dinners", "Sigma Librae Dinners", "Exodus Dinners"):
        assert tag not in raw_values


async def test_dry_run_never_writes_anything():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    report = await compute_dry_run_report(service)
    assert report.write_mode is False

    assert await service.client_store.list() == []


async def test_dry_run_reports_correct_ready_and_held_counts():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    ready, held, _ = _manifest_totals()
    report = await compute_dry_run_report(service)

    assert report.ready_rows == len(ready)
    assert report.held_rows == len(held)
    assert len(report.rows) == len(ready)


async def test_dry_run_reports_would_link_for_every_ready_contact_when_nothing_seeded():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    _, _, ready_contacts = _manifest_totals()
    report = await compute_dry_run_report(service)

    total_would_link = sum(r.would_link for r in report.rows)
    total_already_linked = sum(r.already_linked for r in report.rows)
    assert total_would_link == ready_contacts
    assert total_already_linked == 0


async def test_apply_only_writes_a_ready_rows_never_b_or_c():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    ready, held, ready_contacts = _manifest_totals()
    report = await apply_ready_rows(service)

    assert report.ready_rows == len(ready)
    assert report.held_rows == len(held)
    total_linked = sum(r.linked for r in report.rows)
    assert total_linked == ready_contacts
    assert sum(r.errors for r in report.rows) == 0

    all_engagements = []
    for client in await service.client_store.list():
        all_engagements.extend(await service.engagement_store.list_for_client(client.client_id))
    assert len(all_engagements) == len(ready)


async def test_apply_reuses_the_existing_hive_asmbld_client_not_a_new_one():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)
    existing_hive = await service.create_client({"name": "Hive ASMBLD"})

    await apply_ready_rows(service)

    clients = await service.client_store.list()
    hive_clients = [c for c in clients if c.name == "Hive ASMBLD"]
    assert len(hive_clients) == 1
    assert hive_clients[0].client_id == existing_hive.client_id


async def test_fd_series_rows_share_the_direct_events_pseudo_client_and_stay_distinct_engagements():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    await apply_ready_rows(service)

    direct_events_clients = [c for c in await service.client_store.list() if c.name == DIRECT_EVENTS_CLIENT_NAME]
    assert len(direct_events_clients) == 1
    fd_engagements = await service.engagement_store.list_for_client(direct_events_clients[0].client_id)
    fd_titles = {e.title for e in fd_engagements}
    fd_rows = [r for r in MANIFEST_ROWS if r["bucket"] == "A_READY" and r["client_name"] is None]
    assert fd_titles == {r["canonical_event_name"] for r in fd_rows}
    # every FD row stayed its own Engagement -- none collapsed together
    assert len(fd_engagements) == len(fd_rows)


async def test_second_apply_run_creates_zero_new_participants():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    first = await apply_ready_rows(service)
    second = await apply_ready_rows(service)

    _, _, ready_contacts = _manifest_totals()
    assert sum(r.linked for r in first.rows) == ready_contacts
    assert sum(r.linked for r in second.rows) == 0
    assert sum(r.already_linked for r in second.rows) == ready_contacts


async def test_dinners_attended_custom_field_is_never_read_or_touched_by_this_module():
    import ast
    import inspect

    from app.services import historical_dinner_migration

    tree = ast.parse(inspect.getsource(historical_dinner_migration))
    accessed_attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "custom_fields" not in accessed_attrs


async def test_a_row_held_back_from_a_ready_is_never_written(monkeypatch):
    """The manifest is fully approved today (0 held rows), but the skip
    mechanism itself must still work for any future manifest addition --
    verified here by monkeypatching in a synthetic held row rather than
    relying on real data that no longer contains one."""
    from app.services import historical_dinner_migration as module

    synthetic_held_row = {
        "raw_value": "Synthetic Held Dinner [01.01.2030] Nowhere",
        "canonical_event_name": "Synthetic Held Dinner",
        "event_date": "2030-01-01",
        "location": "Nowhere",
        "client_name": "Synthetic Candidate Client",
        "existing_client": False,
        "bucket": "B_NEEDS_CLIENT_CONFIRMATION",
        "confidence_issue": "test fixture",
        "contact_ids": [],
    }
    monkeypatch.setattr(module, "MANIFEST_ROWS", MANIFEST_ROWS + [synthetic_held_row])

    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    report = await module.apply_ready_rows(service)

    assert report.held_rows == 1
    client_names = {c.name for c in await service.client_store.list()}
    assert "Synthetic Candidate Client" not in client_names


async def test_new_historical_client_gets_inactive_status_not_the_active_default():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)

    await apply_ready_rows(service)

    clients = await service.client_store.list()
    # Hive ASMBLD reused a pre-existing Client (none seeded here, so it too
    # gets freshly created and should ALSO be INACTIVE in this scenario --
    # the dedicated reuse test below covers the "already existed" case).
    non_pseudo_clients = [c for c in clients if c.name != DIRECT_EVENTS_CLIENT_NAME]
    assert non_pseudo_clients
    for c in non_pseudo_clients:
        assert c.status == ClientStatus.INACTIVE, f"{c.name} should be INACTIVE, got {c.status}"
        assert c.relationship_classification is None
        assert c.owner is None
        assert c.next_action is None
        assert c.next_action_due is None


async def test_existing_client_reuse_never_touches_its_current_status():
    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)
    existing_hive = await service.create_client({"name": "Hive ASMBLD", "status": ClientStatus.ACTIVE.value})
    assert existing_hive.status == ClientStatus.ACTIVE

    await apply_ready_rows(service)

    reused = await service.get_client(existing_hive.client_id)
    assert reused.status == ClientStatus.ACTIVE  # untouched, not forced to INACTIVE


async def test_startup_soft_client_name_normalized_but_engagement_title_is_not():
    row = next(r for r in MANIFEST_ROWS if r["canonical_event_name"] == "Startup Soft")
    assert row["client_name"] == "StartupSoft"
    assert row["canonical_event_name"] == "Startup Soft"

    service, crm_contact_store = await _service()
    await _seed_all_manifest_contacts(crm_contact_store)
    await apply_ready_rows(service)

    clients = await service.client_store.list()
    assert "StartupSoft" in {c.name for c in clients}
    assert "Startup Soft" not in {c.name for c in clients}


async def test_dripping_springs_and_ristretto_are_migrated_using_their_stored_names():
    dripping = next(r for r in MANIFEST_ROWS if r["canonical_event_name"] == "Dripping Springs")
    ristretto = next(r for r in MANIFEST_ROWS if r["canonical_event_name"] == "Ristretto")
    assert dripping["bucket"] == "A_READY"
    assert dripping["location"] == "Austin"
    assert dripping["event_date"] == "2026-03-24"
    assert ristretto["bucket"] == "A_READY"
    assert ristretto["event_date"] == "2023-04-20"  # preserved exactly, not "corrected"
    assert ristretto["location"] == "Austin"
