from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact, NOTES_PERSONAL_NOTES_MERGE_HEADER
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.notes_personal_notes_merge import (
    RowOutcome,
    apply_frozen_cohort_merge,
    compute_dry_run_report,
)
from app.services.notes_personal_notes_merge_cohort import (
    COHORT,
    COHORT_SIZE,
    PROVENANCE_MERGE_KEY,
    PROVENANCE_MERGE_SOURCE,
)


def _now():
    return datetime.now(timezone.utc)


def _bare_contact(crm_contact_id: str, **overrides) -> CrmContact:
    defaults = dict(crm_contact_id=crm_contact_id, created_at=_now(), updated_at=_now(), custom_fields={})
    defaults.update(overrides)
    return CrmContact(**defaults)


async def _stores():
    contact_store = MemoryCrmContactStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    return contact_store, activity_log, activity_store


# --- cohort structural integrity ---------------------------------------------------


def test_cohort_module_has_no_store_or_asyncio_imports():
    import ast

    with open("app/services/notes_personal_notes_merge_cohort.py") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "repositor" not in alias.name and alias.name != "asyncio"
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "repositor" not in node.module and node.module != "asyncio"


def test_cohort_size_is_9():
    assert COHORT_SIZE == 9
    assert len(COHORT) == 9


def test_cohort_every_row_has_nonblank_personal_notes():
    for row in COHORT:
        assert row.frozen_personal_notes.strip()


def test_cohort_no_duplicate_contact_ids():
    ids = [row.crm_contact_id for row in COHORT]
    assert len(ids) == len(set(ids))


def test_cohort_split_matches_np1_audit_six_blank_three_populated():
    blank = [r for r in COHORT if not (r.frozen_notes or "").strip()]
    populated = [r for r in COHORT if (r.frozen_notes or "").strip()]
    assert len(blank) == 6
    assert len(populated) == 3


# --- dry-run planning ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_blank_notes_produces_copy_merge():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields={"personal_notes": row.frozen_personal_notes}))

    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.WOULD_MERGE
    assert result.merge_type == "copy"


@pytest.mark.asyncio
async def test_both_populated_produces_append_merge():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if (r.frozen_notes or "").strip())
    await contact_store.create(
        _bare_contact(row.crm_contact_id, custom_fields={"notes": row.frozen_notes, "personal_notes": row.frozen_personal_notes})
    )

    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.WOULD_MERGE
    assert result.merge_type == "append"


@pytest.mark.asyncio
async def test_missing_contact_is_drift_not_forced():
    contact_store, _, _ = await _stores()
    row = COHORT[0]
    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.DRIFT_CONTACT_MISSING


@pytest.mark.asyncio
async def test_archived_contact_is_drift_not_forced():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(
        _bare_contact(row.crm_contact_id, custom_fields={"personal_notes": row.frozen_personal_notes}, archived=True)
    )
    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.DRIFT_CONTACT_ARCHIVED


@pytest.mark.asyncio
async def test_drift_in_notes_fails_safe():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(
        _bare_contact(
            row.crm_contact_id,
            custom_fields={"notes": "Someone typed a real note in the meantime", "personal_notes": row.frozen_personal_notes},
        )
    )
    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.DRIFT_NOTES_CHANGED


@pytest.mark.asyncio
async def test_drift_in_personal_notes_fails_safe():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields={"personal_notes": "A completely different value now"}))
    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.DRIFT_PERSONAL_NOTES_CHANGED


@pytest.mark.asyncio
async def test_duplicate_content_protection_when_notes_already_contains_personal_notes():
    contact_store, _, _ = await _stores()
    row = next(r for r in COHORT if (r.frozen_notes or "").strip())
    # Notes already contains the personal_notes text by some other means (e.g. a manual
    # edit) -- must not be appended a second time even without the provenance marker.
    already_merged_notes = f"{row.frozen_notes}\n\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\n{row.frozen_personal_notes}"
    await contact_store.create(
        _bare_contact(row.crm_contact_id, custom_fields={"notes": already_merged_notes, "personal_notes": row.frozen_personal_notes})
    )
    report = await compute_dry_run_report(contact_store)
    result = next(r for r in report.results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == RowOutcome.ALREADY_SATISFIED


# --- write path ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_merge_sets_notes_to_stripped_personal_notes_with_no_header():
    contact_store, activity_log, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields={"personal_notes": row.frozen_personal_notes}))

    await apply_frozen_cohort_merge(contact_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    assert updated.custom_fields["notes"] == row.frozen_personal_notes.strip()
    assert NOTES_PERSONAL_NOTES_MERGE_HEADER not in updated.custom_fields["notes"]


@pytest.mark.asyncio
async def test_append_merge_preserves_existing_notes_byte_for_byte_before_appended_block():
    contact_store, activity_log, _ = await _stores()
    row = next(r for r in COHORT if (r.frozen_notes or "").strip())
    await contact_store.create(
        _bare_contact(row.crm_contact_id, custom_fields={"notes": row.frozen_notes, "personal_notes": row.frozen_personal_notes})
    )

    await apply_frozen_cohort_merge(contact_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    new_notes = updated.custom_fields["notes"]
    # the EXACT original text, unmodified, must be the literal prefix
    assert new_notes.startswith(row.frozen_notes)
    assert new_notes == f"{row.frozen_notes}\n\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\n{row.frozen_personal_notes.strip()}"


@pytest.mark.asyncio
async def test_personal_notes_itself_is_never_modified():
    contact_store, activity_log, _ = await _stores()
    for row in COHORT:
        cf = {"personal_notes": row.frozen_personal_notes}
        if row.frozen_notes:
            cf["notes"] = row.frozen_notes
        await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields=cf))

    await apply_frozen_cohort_merge(contact_store, activity_log)

    for row in COHORT:
        updated = await contact_store.get(row.crm_contact_id)
        assert updated.custom_fields["personal_notes"] == row.frozen_personal_notes


@pytest.mark.asyncio
async def test_existing_notes_field_provenance_is_preserved_not_overwritten():
    contact_store, activity_log, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    pre_existing_provenance = {"source": "some_other_source", "imported_at": "2026-01-01T00:00:00+00:00"}
    await contact_store.create(
        _bare_contact(
            row.crm_contact_id,
            custom_fields={
                "personal_notes": row.frozen_personal_notes,
                "field_provenance": {"notes": pre_existing_provenance},
            },
        )
    )
    await apply_frozen_cohort_merge(contact_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    assert updated.custom_fields["field_provenance"]["notes"] == pre_existing_provenance


@pytest.mark.asyncio
async def test_austin_forward_notes_provenance_specifically_survives_migration():
    """The exact scenario this design was built to protect -- a Contact whose Notes
    carries one of the 161 real Austin Forward field_provenance entries must keep it
    byte-for-byte after a Personal Notes merge onto the SAME notes field."""
    contact_store, activity_log, _ = await _stores()
    row = next(r for r in COHORT if (r.frozen_notes or "").strip())
    austin_forward_provenance = {"source": "austin_forward_guest_list_2026_09_10", "imported_at": "2026-09-14T05:57:41+00:00"}
    await contact_store.create(
        _bare_contact(
            row.crm_contact_id,
            custom_fields={
                "notes": row.frozen_notes,
                "personal_notes": row.frozen_personal_notes,
                "field_provenance": {"notes": austin_forward_provenance},
            },
        )
    )
    await apply_frozen_cohort_merge(contact_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    assert updated.custom_fields["field_provenance"]["notes"] == austin_forward_provenance
    assert updated.custom_fields["field_provenance"]["notes"]["source"] == "austin_forward_guest_list_2026_09_10"
    # the merge's OWN provenance lives under its own separate key, never colliding
    assert updated.custom_fields["field_provenance"][PROVENANCE_MERGE_KEY]["source"] == PROVENANCE_MERGE_SOURCE


@pytest.mark.asyncio
async def test_merge_provenance_key_is_recorded_and_never_collides_with_notes_key():
    contact_store, activity_log, _ = await _stores()
    row = next(r for r in COHORT if not (r.frozen_notes or "").strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields={"personal_notes": row.frozen_personal_notes}))
    await apply_frozen_cohort_merge(contact_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    prov = updated.custom_fields["field_provenance"]
    assert PROVENANCE_MERGE_KEY in prov
    assert prov[PROVENANCE_MERGE_KEY]["source"] == PROVENANCE_MERGE_SOURCE
    assert prov[PROVENANCE_MERGE_KEY]["original_field"] == "personal_notes"
    # "notes" was blank before -- no pre-existing notes provenance to collide with, and
    # this migration doesn't fabricate one for the notes key itself.
    assert "notes" not in prov


@pytest.mark.asyncio
async def test_activity_entry_is_structural_only_no_notes_text():
    contact_store, activity_log, activity_store = await _stores()
    row = next(r for r in COHORT if (r.frozen_notes or "").strip())
    await contact_store.create(
        _bare_contact(row.crm_contact_id, custom_fields={"notes": row.frozen_notes, "personal_notes": row.frozen_personal_notes})
    )
    await apply_frozen_cohort_merge(contact_store, activity_log)

    events = await activity_store.list()
    event = next(e for e in events if e.entity_id == row.crm_contact_id)
    assert event.metadata["source"] == PROVENANCE_MERGE_SOURCE
    assert row.frozen_notes not in str(event.metadata)
    assert row.frozen_personal_notes not in str(event.metadata)
    assert set(event.metadata["fields_updated"]) == {"custom:notes", "custom:field_provenance"}


# --- idempotency: second run is a true no-op ------------------------------------------


@pytest.mark.asyncio
async def test_second_full_run_makes_zero_mutations():
    contact_store, activity_log, activity_store = await _stores()
    for row in COHORT:
        cf = {"personal_notes": row.frozen_personal_notes}
        if row.frozen_notes:
            cf["notes"] = row.frozen_notes
        await contact_store.create(_bare_contact(row.crm_contact_id, custom_fields=cf))

    first_report = await apply_frozen_cohort_merge(contact_store, activity_log)
    assert first_report.counts.would_merge == 9

    updated_ats_after_first = {row.crm_contact_id: (await contact_store.get(row.crm_contact_id)).updated_at for row in COHORT}
    personal_notes_after_first = {row.crm_contact_id: (await contact_store.get(row.crm_contact_id)).custom_fields["personal_notes"] for row in COHORT}
    activity_count_after_first = len(await activity_store.list())

    second_report = await apply_frozen_cohort_merge(contact_store, activity_log)
    assert second_report.counts.would_merge == 0
    assert second_report.counts.already_satisfied == 9

    for row in COHORT:
        updated = await contact_store.get(row.crm_contact_id)
        assert updated.updated_at == updated_ats_after_first[row.crm_contact_id]
        assert updated.custom_fields["personal_notes"] == personal_notes_after_first[row.crm_contact_id]
    assert len(await activity_store.list()) == activity_count_after_first
