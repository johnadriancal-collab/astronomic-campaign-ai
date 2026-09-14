from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_contact_list_member_store import MemoryCrmContactListMemberStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.austin_forward_reconciliation import (
    ExistingRowOutcome,
    NewRowOutcome,
    apply_frozen_cohort_reconciliation,
    compute_dry_run_report,
)
from app.services.austin_forward_reconciliation_cohort import (
    CSV_DUPLICATE_COLLAPSED,
    CSV_TOTAL_ROWS,
    DINNER_ATTENDED_VALUE,
    DINNER_SUBSCRIPTION_VALUE,
    EXISTING_CONTACTS,
    EXISTING_CONTACTS_SIZE,
    INSUFFICIENT_IDENTITY_EXCLUDED,
    NAME_ONLY_CANDIDATES_EXCLUDED,
    NEW_CONTACTS,
    NEW_CONTACTS_SIZE,
    NOTES_HEADER,
    POST_EXIT_FOUNDER_ROLE,
    PROVENANCE_SOURCE,
    TARGET_LIST_ID,
    TARGET_LOCATION,
    TOTAL_COHORT_SIZE,
)


def _now():
    return datetime.now(timezone.utc)


def _bare_contact(crm_contact_id: str, **overrides) -> CrmContact:
    defaults = dict(
        crm_contact_id=crm_contact_id,
        created_at=_now(),
        updated_at=_now(),
        custom_fields={},
    )
    defaults.update(overrides)
    return CrmContact(**defaults)


async def _stores():
    contact_store = MemoryCrmContactStore()
    list_member_store = MemoryCrmContactListMemberStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    return contact_store, list_member_store, activity_log, activity_store


# --- cohort structural integrity ------------------------------------------------


def test_cohort_module_has_no_store_or_asyncio_imports():
    import ast

    with open("app/services/austin_forward_reconciliation_cohort.py") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "repositor" not in alias.name and alias.name != "asyncio"
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "repositor" not in node.module and node.module != "asyncio"


def test_cohort_totals_are_166():
    assert EXISTING_CONTACTS_SIZE == 55
    assert NEW_CONTACTS_SIZE == 111
    assert TOTAL_COHORT_SIZE == 166
    assert len(EXISTING_CONTACTS) == 55
    assert len(NEW_CONTACTS) == 111


def test_cohort_math_matches_csv_audit():
    assert CSV_TOTAL_ROWS == 188
    assert CSV_DUPLICATE_COLLAPSED == 1
    assert NAME_ONLY_CANDIDATES_EXCLUDED == 4
    assert INSUFFICIENT_IDENTITY_EXCLUDED == 17
    assert CSV_TOTAL_ROWS - CSV_DUPLICATE_COLLAPSED - NAME_ONLY_CANDIDATES_EXCLUDED - INSUFFICIENT_IDENTITY_EXCLUDED == TOTAL_COHORT_SIZE


def test_no_duplicate_crm_contact_ids_in_existing_cohort():
    ids = [row.crm_contact_id for row in EXISTING_CONTACTS]
    assert len(ids) == len(set(ids))


def test_no_duplicate_new_contact_people():
    keys = [(row.email, row.linkedin_url, row.full_name) for row in NEW_CONTACTS]
    assert len(keys) == len(set(keys))


def test_pef_never_appears_in_any_cohort_row():
    for row in list(EXISTING_CONTACTS) + list(NEW_CONTACTS):
        assert "PEF" not in row.roles


def test_never_inferred_relationship_tags_absent_from_every_row():
    never_inferred = {"Client", "Galaxy Member", "NOT AN INVESTOR", "Facilitator", "Venture Partner"}
    for row in list(EXISTING_CONTACTS) + list(NEW_CONTACTS):
        assert never_inferred.isdisjoint(row.roles)


def test_post_exit_founder_cohort_present_and_strict():
    post_exit = [r for r in list(EXISTING_CONTACTS) + list(NEW_CONTACTS) if POST_EXIT_FOUNDER_ROLE in r.roles]
    # 37 total candidates were identified; Chris Taylor is a name-only manual-review
    # case excluded from the automatic cohort, so 36 remain here.
    assert len(post_exit) == 36
    for row in post_exit:
        assert "Founder" in row.roles or "Post-Exit Founder" in row.roles  # founder status always co-present


def test_event_role_never_written_as_permanent_role():
    for row in list(EXISTING_CONTACTS) + list(NEW_CONTACTS):
        assert "Host" not in row.roles
        assert "Guest" not in row.roles
        assert "Sponsor" not in row.roles
        assert "Sponsors" not in row.roles
        assert row.role_in_event in ("Host", "Guest", "Sponsors")


def test_every_new_contact_has_email_or_linkedin():
    for row in NEW_CONTACTS:
        assert row.email or row.linkedin_url


# --- dry-run: existing Contact deltas ---------------------------------------------


@pytest.mark.asyncio
async def test_existing_contact_fully_blank_gets_every_delta():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))

    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)

    assert result.outcome == ExistingRowOutcome.WOULD_UPDATE
    assert result.dinners_attended_add is True
    assert result.dinner_subscriptions_add is True
    assert result.list_membership_add is True
    assert result.city_fill is True
    assert result.state_fill is True
    assert result.country_fill is True
    assert set(result.roles_added) == set(row.roles)
    if row.accomplishments.strip():
        assert result.notes_action == "insert_blank"


@pytest.mark.asyncio
async def test_existing_contact_already_fully_satisfied_is_a_true_no_op():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    contact = _bare_contact(
        row.crm_contact_id,
        email=row.matched_email_norm,
        city=TARGET_LOCATION["city"],
        state=TARGET_LOCATION["state"],
        country=TARGET_LOCATION["country"],
        custom_fields={
            "dinners_attended": [DINNER_ATTENDED_VALUE],
            "dinner_subscriptions": [DINNER_SUBSCRIPTION_VALUE],
            "role": list(row.roles),
            "notes": row.accomplishments.strip() or None,
            "field_provenance": {"notes": {"source": PROVENANCE_SOURCE}} if row.accomplishments.strip() else {},
        },
    )
    await contact_store.create(contact)
    await list_member_store.add(_membership(row.crm_contact_id))

    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == ExistingRowOutcome.ALREADY_SATISFIED
    assert result.dinners_attended_add is False
    assert result.dinner_subscriptions_add is False
    assert result.list_membership_add is False
    assert not result.roles_added
    assert result.notes_action is None


def _membership(crm_contact_id: str):
    from app.models.crm import CrmContactListMembership

    return CrmContactListMembership(list_id=TARGET_LIST_ID, crm_contact_id=crm_contact_id, added_at=_now())


@pytest.mark.asyncio
async def test_nonblank_existing_location_is_never_overwritten():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(
        _bare_contact(row.crm_contact_id, email=row.matched_email_norm, city="San Francisco", state="California", country="United States")
    )
    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    assert result.city_fill is False
    assert result.state_fill is False
    assert result.country_fill is False


@pytest.mark.asyncio
async def test_existing_roles_are_preserved_never_removed():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(
        _bare_contact(row.crm_contact_id, email=row.matched_email_norm, custom_fields={"role": ["Client"]})
    )
    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    # "Client" is never in a cohort row's roles (never auto-inferred), so it must never
    # appear in roles_added, and the write path must preserve it untouched.
    assert "Client" not in result.roles_added


@pytest.mark.asyncio
async def test_missing_contact_is_drift_not_forced():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == ExistingRowOutcome.DRIFT_CONTACT_MISSING


@pytest.mark.asyncio
async def test_archived_contact_is_drift_not_forced():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm, archived=True))
    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == ExistingRowOutcome.DRIFT_CONTACT_ARCHIVED


@pytest.mark.asyncio
async def test_identity_changed_since_match_is_drift_not_forced():
    contact_store, list_member_store, _, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(_bare_contact(row.crm_contact_id, email="someone-completely-different@example.com", linkedin_url=None))
    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.existing_results if r.crm_contact_id == row.crm_contact_id)
    assert result.outcome == ExistingRowOutcome.DRIFT_IDENTITY_CHANGED


# --- Notes handling ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_blank_insert_has_no_header_prefix():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in EXISTING_CONTACTS if r.accomplishments.strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))

    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    assert updated.custom_fields["notes"] == row.accomplishments.strip()
    assert NOTES_HEADER not in updated.custom_fields["notes"]


@pytest.mark.asyncio
async def test_notes_append_to_existing_uses_header_and_preserves_prior_text():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in EXISTING_CONTACTS if r.accomplishments.strip())
    await contact_store.create(
        _bare_contact(row.crm_contact_id, email=row.matched_email_norm, custom_fields={"notes": "Writes $250k-$500k checks"})
    )

    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    notes = updated.custom_fields["notes"]
    assert notes.startswith("Writes $250k-$500k checks")
    assert NOTES_HEADER in notes
    assert row.accomplishments.strip() in notes


@pytest.mark.asyncio
async def test_notes_never_inserted_twice_on_rerun():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in EXISTING_CONTACTS if r.accomplishments.strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))

    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    first_notes = (await contact_store.get(row.crm_contact_id)).custom_fields["notes"]

    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    second_notes = (await contact_store.get(row.crm_contact_id)).custom_fields["notes"]

    assert first_notes == second_notes
    assert second_notes.count(row.accomplishments.strip()) == 1


@pytest.mark.asyncio
async def test_notes_provenance_recorded_gates_idempotency_even_without_visible_marker():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in EXISTING_CONTACTS if r.accomplishments.strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    assert updated.custom_fields["field_provenance"]["notes"]["source"] == PROVENANCE_SOURCE


# --- write path: persistence + Activity ---------------------------------------------


@pytest.mark.asyncio
async def test_write_path_unions_multiselects_never_replaces():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(
        _bare_contact(
            row.crm_contact_id,
            email=row.matched_email_norm,
            custom_fields={"dinners_attended": ["Some Other Dinner [01.01.2025] Austin"]},
        )
    )
    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    updated = await contact_store.get(row.crm_contact_id)
    da = updated.custom_fields["dinners_attended"]
    assert "Some Other Dinner [01.01.2025] Austin" in da
    assert DINNER_ATTENDED_VALUE in da


@pytest.mark.asyncio
async def test_write_path_adds_list_membership_via_idempotent_store():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = EXISTING_CONTACTS[0]
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    members = await list_member_store.list_contact_ids_for_list(TARGET_LIST_ID)
    assert row.crm_contact_id in members
    assert members.count(row.crm_contact_id) == 1


@pytest.mark.asyncio
async def test_activity_log_entry_is_structural_only_no_accomplishments_text():
    contact_store, list_member_store, activity_log, activity_store = await _stores()
    row = next(r for r in EXISTING_CONTACTS if r.accomplishments.strip())
    await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))
    await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)

    events = await activity_store.list()
    event = next(e for e in events if e.entity_id == row.crm_contact_id)
    assert event.metadata["source"] == PROVENANCE_SOURCE
    for key in event.metadata["fields_updated"]:
        assert row.accomplishments not in key
    assert row.matched_email_norm not in str(event.metadata)


@pytest.mark.asyncio
async def test_new_contact_is_created_with_every_target_field():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in NEW_CONTACTS if r.accomplishments.strip() and r.roles)
    report = await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    result = next(r for r in report.new_results if r.full_name == row.full_name)
    assert result.outcome == NewRowOutcome.CREATED
    created = await contact_store.get(result.crm_contact_id)
    assert created.first_name == row.first_name
    assert created.last_name == row.last_name
    assert created.city == TARGET_LOCATION["city"]
    assert created.state == TARGET_LOCATION["state"]
    assert created.country == TARGET_LOCATION["country"]
    assert DINNER_ATTENDED_VALUE in created.custom_fields["dinners_attended"]
    assert DINNER_SUBSCRIPTION_VALUE in created.custom_fields["dinner_subscriptions"]
    assert set(row.roles) == set(created.custom_fields.get("role") or [])
    assert created.custom_fields["notes"] == row.accomplishments.strip()
    members = await list_member_store.list_contact_ids_for_list(TARGET_LIST_ID)
    assert result.crm_contact_id in members


@pytest.mark.asyncio
async def test_new_contact_candidate_that_now_exists_is_not_duplicated():
    contact_store, list_member_store, activity_log, _ = await _stores()
    row = next(r for r in NEW_CONTACTS if r.email)
    # simulate someone independently creating this exact person between the
    # investigation's dry run and this write
    await contact_store.create(_bare_contact("independently-created-id", email=row.email, first_name=row.first_name, last_name=row.last_name))

    report = await compute_dry_run_report(contact_store, list_member_store)
    result = next(r for r in report.new_results if r.full_name == row.full_name)
    assert result.outcome == NewRowOutcome.DRIFT_ALREADY_EXISTS


# --- full idempotency: second run is a true no-op -----------------------------------


@pytest.mark.asyncio
async def test_second_full_run_makes_zero_mutations():
    contact_store, list_member_store, activity_log, activity_store = await _stores()
    for row in EXISTING_CONTACTS:
        await contact_store.create(_bare_contact(row.crm_contact_id, email=row.matched_email_norm))

    first_report = await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    assert first_report.counts.existing_would_update == 55
    assert first_report.counts.new_would_create == 111

    updated_ats_after_first = {row.crm_contact_id: (await contact_store.get(row.crm_contact_id)).updated_at for row in EXISTING_CONTACTS}
    activity_count_after_first = len(await activity_store.list())
    member_count_after_first = len(await list_member_store.list_contact_ids_for_list(TARGET_LIST_ID))

    second_report = await apply_frozen_cohort_reconciliation(contact_store, list_member_store, activity_log)
    assert second_report.counts.existing_would_update == 0
    assert second_report.counts.existing_already_satisfied == 55
    assert second_report.counts.new_would_create == 0
    assert second_report.counts.new_drift_already_exists == 111

    for row in EXISTING_CONTACTS:
        assert (await contact_store.get(row.crm_contact_id)).updated_at == updated_ats_after_first[row.crm_contact_id]
    assert len(await activity_store.list()) == activity_count_after_first
    assert len(await list_member_store.list_contact_ids_for_list(TARGET_LIST_ID)) == member_count_after_first
    # no duplicate Contacts were created by the second run either
    assert len(await contact_store.list()) == 55 + 111
