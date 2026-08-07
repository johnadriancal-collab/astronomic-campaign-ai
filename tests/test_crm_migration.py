"""
Tests for the legacy-CRM-field reconciliation: seeding the custom field
definitions that have no core/thesis equivalent, and migrating values
already sitting under the direct-duplicate old keys into the matching
Investor Thesis field -- additively, never deleting/overwriting anything.
"""

from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact, CrmImportBatch, CustomFieldType
from app.services.crm_migration import (
    CUSTOM_FIELD_CORRECTIONS,
    DIRECT_DUPLICATE_MIGRATIONS,
    FUNDING_STAGE_ENGAGEMENT_SHAPED_VALUES,
    HOW_EARLY_KNOWN_PHRASES,
    LEGACY_FIELD_SEEDS,
    _retokenize_known_phrases,
    apply_custom_field_corrections,
    migrate_all_contacts,
    migrate_all_dinner_subscriptions,
    migrate_all_funding_stage_corruption,
    migrate_contact_dinner_subscriptions,
    migrate_contact_funding_stage_corruption,
    migrate_contact_legacy_fields,
    reconcile_legacy_fields,
    repair_all_contacts_comma_delimited_fields,
    repair_contact_comma_delimited_fields,
    seed_legacy_custom_fields,
    translate_legacy_import_batch,
)
from app.services.crm_service import CrmService


@pytest.fixture
def service():
    return CrmService()


def make_contact(**overrides) -> CrmContact:
    now = datetime.now(timezone.utc)
    defaults = dict(crm_contact_id="c1", created_at=now, updated_at=now)
    defaults.update(overrides)
    return CrmContact(**defaults)


# --- Seeding ---


@pytest.mark.asyncio
async def test_seed_creates_every_legacy_field_on_first_run(service):
    report = await seed_legacy_custom_fields(service)
    assert set(report["created"]) == {key for key, *_ in LEGACY_FIELD_SEEDS}
    assert report["skipped"] == []

    all_fields = await service.list_custom_fields()
    assert {f.field_key for f in all_fields} == {key for key, *_ in LEGACY_FIELD_SEEDS}


@pytest.mark.asyncio
async def test_seed_is_idempotent_on_second_run(service):
    await seed_legacy_custom_fields(service)
    second = await seed_legacy_custom_fields(service)
    assert second["created"] == []
    assert set(second["skipped"]) == {key for key, *_ in LEGACY_FIELD_SEEDS}

    # No duplicates were created.
    all_fields = await service.list_custom_fields()
    assert len(all_fields) == len(LEGACY_FIELD_SEEDS)


@pytest.mark.asyncio
async def test_seed_respects_a_field_the_user_already_created_manually(service):
    """If field_key already exists (e.g. the user created it themselves first),
    seeding must not touch or duplicate it."""
    existing = await service.create_custom_field(field_key="role", label="My Own Custom Role Label", field_type="text")
    await seed_legacy_custom_fields(service)

    fetched = await service.custom_field_store.get(existing.crm_custom_field_id)
    assert fetched.label == "My Own Custom Role Label"  # untouched


# --- Value migration (pure function) ---


def test_single_value_field_wrapped_into_a_list_on_the_matching_thesis_field():
    contact = make_contact(custom_fields={"deal_stage": "Seed (product in market, early customers or pilots)"})
    migrated, changed = migrate_contact_legacy_fields(contact)

    assert changed == ["deal_stage"]
    assert migrated.thesis_private_deal_stages == ["Seed (product in market, early customers or pilots)"]
    # Old value untouched, still there.
    assert migrated.custom_fields["deal_stage"] == "Seed (product in market, early customers or pilots)"


def test_list_value_passed_through_unwrapped():
    contact = make_contact(custom_fields={"investing_asset_types": ["Private equity", "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)"]})
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert migrated.thesis_private_asset_types == [
        "Private equity", "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)"
    ]
    assert changed == ["investing_asset_types"]


def test_dietary_restrictions_migrates_into_dietary_preferences_list_field():
    """2026-08-07: thesis_dietary_preferences converted from TEXT to list[str] --
    the old scalar custom_fields value gets wrapped in a single-item list (is_list=True),
    same as every other DIRECT_DUPLICATE_MIGRATIONS entry."""
    contact = make_contact(custom_fields={"dietary_restrictions": "Vegetarian"})
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert migrated.thesis_dietary_preferences == ["Vegetarian"]
    assert changed == ["dietary_restrictions"]


def test_check_size_keys_removed_from_direct_duplicate_migrations():
    """2026-08-06 Check Size consolidation: 'check_size_institutional' was a confirmed
    root-cause bug -- that 'old key' string was IDENTICAL to today's live, active
    check_size_institutional custom field, so every reconcile_legacy_fields() run
    silently re-populated the now-deprecated thesis_institutional_check_sizes from it.
    'check_size' (bare) never collided with a live key but is removed for the same
    reason: neither deprecated thesis check-size field should ever be written to again."""
    assert "check_size" not in DIRECT_DUPLICATE_MIGRATIONS
    assert "check_size_institutional" not in DIRECT_DUPLICATE_MIGRATIONS


def test_migrate_contact_legacy_fields_never_repopulates_deprecated_check_size_thesis_fields():
    """The exact collision this fix closes: a contact whose check_size_personal/
    check_size_institutional custom fields are populated (the real, current shape,
    via the Check Size CSV-import feature) must NOT have that data copied into the
    deprecated thesis_private_check_sizes/thesis_institutional_check_sizes fields."""
    contact = make_contact(
        custom_fields={
            "check_size_personal": ["$25k - $50k", "$50k - $100k"],
            "check_size_institutional": ["$1M - $2M"],
        }
    )
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert migrated.thesis_private_check_sizes == []
    assert migrated.thesis_institutional_check_sizes == []
    assert "check_size_personal" not in changed
    assert "check_size_institutional" not in changed
    # Custom field values themselves are completely untouched.
    assert migrated.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]
    assert migrated.custom_fields["check_size_institutional"] == ["$1M - $2M"]


def test_migration_never_overwrites_an_existing_thesis_value():
    contact = make_contact(
        thesis_private_deal_stages=["Series A (scaling phase, revenue traction, team expansion)"],
        custom_fields={"deal_stage": "Seed (product in market, early customers or pilots)"},
    )
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert migrated.thesis_private_deal_stages == ["Series A (scaling phase, revenue traction, team expansion)"]
    assert changed == []  # nothing migrated -- thesis field already had a value


def test_migration_leaves_old_custom_field_key_in_place_never_deletes():
    contact = make_contact(custom_fields={"dietary_restrictions": "Vegan"})
    migrated, _ = migrate_contact_legacy_fields(contact)
    assert migrated.custom_fields.get("dietary_restrictions") == "Vegan"  # still there, reversible


def test_migration_no_op_when_no_legacy_values_present():
    contact = make_contact(first_name="Ada")
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert changed == []
    assert migrated == contact


def test_migration_handles_multiple_direct_duplicates_on_one_contact():
    contact = make_contact(
        custom_fields={
            "deal_stage": "Seed (product in market, early customers or pilots)",
            "founder_diversity_preference": "I'm open to investing in anyone",
        }
    )
    migrated, changed = migrate_contact_legacy_fields(contact)
    assert set(changed) == {"deal_stage", "founder_diversity_preference"}
    assert migrated.thesis_private_deal_stages == ["Seed (product in market, early customers or pilots)"]
    assert migrated.thesis_private_demographic_preferences == ["I'm open to investing in anyone"]


# --- Batch migration + full reconcile ---


@pytest.mark.asyncio
async def test_migrate_all_contacts_only_saves_contacts_that_actually_changed(service):
    a = await service.create_contact({"first_name": "Ada"})  # nothing to migrate
    b = await service.create_contact({"first_name": "Grace", "custom_fields": {"dietary_restrictions": "Vegan"}})

    report = await migrate_all_contacts(service.contact_store)
    assert report == {"contacts_scanned": 2, "contacts_updated": 1, "fields_migrated": 1}

    assert (await service.get_contact(a.crm_contact_id)).thesis_dietary_preferences == []
    assert (await service.get_contact(b.crm_contact_id)).thesis_dietary_preferences == ["Vegan"]


@pytest.mark.asyncio
async def test_reconcile_legacy_fields_seeds_and_migrates_in_one_call(service):
    await service.create_contact({"first_name": "Grace", "custom_fields": {"dietary_restrictions": "Vegan"}})

    report = await reconcile_legacy_fields(service)
    assert "gender" in report["created"]
    assert report["contacts_updated"] == 1
    assert report["fields_migrated"] == 1


@pytest.mark.asyncio
async def test_reconcile_is_safe_to_run_twice(service):
    await service.create_contact({"custom_fields": {"dietary_restrictions": "Vegan"}})
    await reconcile_legacy_fields(service)
    second = await reconcile_legacy_fields(service)

    assert second["created"] == []  # already seeded
    assert second["fields_migrated"] == 0  # already migrated, nothing left to move
    all_fields = await service.list_custom_fields()
    assert len(all_fields) == len(LEGACY_FIELD_SEEDS)  # still no duplicates


# --- Custom field corrections (fixing pre-CSV-review guesses) ---


@pytest.mark.asyncio
async def test_corrections_fix_accredited_status_options(service):
    await seed_legacy_custom_fields(service)
    corrected = await apply_custom_field_corrections(service)
    assert "accredited_status" in corrected

    field = await service.custom_field_store.get_by_field_key("accredited_status")
    assert field.options == ["Yes", "No"]


@pytest.mark.asyncio
async def test_corrections_fix_age_range_to_single_select_with_options(service):
    await seed_legacy_custom_fields(service)
    corrected = await apply_custom_field_corrections(service)
    assert "age_range" in corrected

    field = await service.custom_field_store.get_by_field_key("age_range")
    assert field.field_type == CustomFieldType.SINGLE_SELECT
    assert field.options == [
        "18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased",
    ]


@pytest.mark.asyncio
async def test_corrections_fix_dinner_subscriptions_to_multi_select_with_14_options(service):
    await seed_legacy_custom_fields(service)
    corrected = await apply_custom_field_corrections(service)
    assert "dinner_subscriptions" in corrected

    field = await service.custom_field_store.get_by_field_key("dinner_subscriptions")
    assert field.field_type == CustomFieldType.MULTI_SELECT
    assert field.options == [
        "Investor Dinners", "Investor Dinners Unsubscribe", "Founder Dinners", "Founder Dinners Unsubscribe",
        "Newsletter", "Newsletter Unsubscribe", "Donor Dinners", "Donor Dinners Unsubscribe",
        "Unsubscribe (Do not Email)", "Not actively Investing", "Biz Dev Dinners", "Biz Dev Dinners Unsubscribe",
        "Fireside Dinners", "Fireside Dinners Unsubscribe",
    ]


@pytest.mark.asyncio
async def test_corrections_fix_investor_type_to_multi_select_with_real_vocab(service):
    await seed_legacy_custom_fields(service)
    await apply_custom_field_corrections(service)

    field = await service.custom_field_store.get_by_field_key("investor_type")
    assert field.field_type == CustomFieldType.MULTI_SELECT
    assert "Angel Investor" in field.options
    assert "Syndicate Lead" not in field.options  # never actually appeared in the real data


@pytest.mark.asyncio
async def test_corrections_are_idempotent(service):
    await seed_legacy_custom_fields(service)
    await apply_custom_field_corrections(service)
    second = await apply_custom_field_corrections(service)
    assert set(second) == set(CUSTOM_FIELD_CORRECTIONS.keys())  # runs again cleanly, no error

    field = await service.custom_field_store.get_by_field_key("investor_type")
    assert len(field.options) == 10  # still exactly 10, not duplicated


@pytest.mark.asyncio
async def test_corrections_noop_when_field_not_yet_seeded(service):
    """If seeding hasn't run yet, corrections must not crash -- just skip."""
    corrected = await apply_custom_field_corrections(service)
    assert corrected == []


@pytest.mark.asyncio
async def test_reconcile_legacy_fields_applies_corrections_too(service):
    report = await reconcile_legacy_fields(service)
    assert "investor_type" in report["corrected"]
    field = await service.custom_field_store.get_by_field_key("dinners_attended")
    assert field.field_type == CustomFieldType.MULTI_SELECT


# --- Dinner Subscriptions value migration (text -> multi-select) ---


def test_dinner_subscriptions_final_value_unchanged_by_migration():
    contact = make_contact(custom_fields={"dinner_subscriptions": "Investor Dinners, Fireside Dinners"})
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert updated.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners"]


def test_dinner_subscriptions_legacy_value_maps_to_correct_final_value():
    contact = make_contact(custom_fields={"dinner_subscriptions": "Sigma Librae Dinners"})
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert updated.custom_fields["dinner_subscriptions"] == ["Founder Dinners"]


def test_dinner_subscriptions_multiple_legacy_values_combined():
    contact = make_contact(
        custom_fields={
            "dinner_subscriptions": (
                "Investor Dinners, Exodus Dinners, Founder Dinners, "
                "Couples dinners with matching founder/investor couples, Biz Dev Dinners"
            )
        }
    )
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    # Exodus Dinners -> Founder Dinners (already present, deduped); Couples... -> Investor Dinners (already present)
    assert updated.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners", "Biz Dev Dinners"]


def test_dinner_subscriptions_duplicate_resulting_values_are_deduplicated():
    contact = make_contact(
        custom_fields={"dinner_subscriptions": "Regulus Dinners, Sigma Librae Dinners, Exodus Dinners"}
    )
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert updated.custom_fields["dinner_subscriptions"] == ["Founder Dinners"]  # all three collapse into one, once


def test_dinner_subscriptions_delete_only_value_removes_the_key_entirely():
    contact = make_contact(
        custom_fields={"dinner_subscriptions": "Retreats, Parent dinners, Astronomic General Subscriber"}
    )
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert "dinner_subscriptions" not in updated.custom_fields  # empty/not-set, not an empty list


def test_dinner_subscriptions_mixed_valid_legacy_and_deleted_values():
    contact = make_contact(
        custom_fields={
            "dinner_subscriptions": (
                "Investor Dinners, Exodus Dinners, Founder Dinners, "
                "Couples dinners with matching founder/investor couples, "
                "Mansion dinners with matching founders/investors, Parent dinners, Retreats, Biz Dev Dinners"
            )
        }
    )
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert updated.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners", "Biz Dev Dinners"]


def test_dinner_subscriptions_unrecognized_value_preserved_not_discarded():
    contact = make_contact(custom_fields={"dinner_subscriptions": "Investor Dinners, Some Future Dinner Series"})
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is True
    assert updated.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Some Future Dinner Series"]


def test_dinner_subscriptions_already_migrated_list_value_is_a_noop():
    """Idempotency signal: a list value means it's already been migrated (or was
    created fresh under the new multi_select type) -- must not be touched again."""
    contact = make_contact(custom_fields={"dinner_subscriptions": ["Investor Dinners", "Founder Dinners"]})
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is False
    assert updated is contact
    assert updated.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners"]


def test_dinner_subscriptions_missing_value_is_a_noop():
    contact = make_contact(custom_fields={})
    updated, changed = migrate_contact_dinner_subscriptions(contact)
    assert changed is False
    assert "dinner_subscriptions" not in updated.custom_fields


def test_dinner_subscriptions_never_touches_other_custom_fields():
    contact = make_contact(
        custom_fields={"dinner_subscriptions": "Retreats", "gender": "Female", "notes": "some notes"}
    )
    updated, _ = migrate_contact_dinner_subscriptions(contact)
    assert updated.custom_fields["gender"] == "Female"
    assert updated.custom_fields["notes"] == "some notes"
    assert "dinner_subscriptions" not in updated.custom_fields


@pytest.mark.asyncio
async def test_migrate_all_dinner_subscriptions_only_saves_contacts_that_changed(service):
    a = await service.create_contact({"first_name": "Ada"})  # no dinner_subscriptions at all
    b = await service.create_contact(
        {"first_name": "Grace", "custom_fields": {"dinner_subscriptions": "Investor Dinners, Retreats"}}
    )
    c = await service.create_contact(
        {"first_name": "Hedy", "custom_fields": {"dinner_subscriptions": ["Founder Dinners"]}}  # already migrated
    )

    report = await migrate_all_dinner_subscriptions(service.contact_store)
    assert report == {"dinner_subscriptions_contacts_scanned": 3, "dinner_subscriptions_contacts_updated": 1}

    assert "dinner_subscriptions" not in (await service.get_contact(a.crm_contact_id)).custom_fields
    assert (await service.get_contact(b.crm_contact_id)).custom_fields["dinner_subscriptions"] == ["Investor Dinners"]
    assert (await service.get_contact(c.crm_contact_id)).custom_fields["dinner_subscriptions"] == ["Founder Dinners"]


@pytest.mark.asyncio
async def test_migrate_all_dinner_subscriptions_is_idempotent(service):
    await service.create_contact(
        {"custom_fields": {"dinner_subscriptions": "Investor Dinners, Sigma Librae Dinners"}}
    )
    first = await migrate_all_dinner_subscriptions(service.contact_store)
    second = await migrate_all_dinner_subscriptions(service.contact_store)
    assert first["dinner_subscriptions_contacts_updated"] == 1
    assert second["dinner_subscriptions_contacts_updated"] == 0  # already a list -- no-op the second time


@pytest.mark.asyncio
async def test_reconcile_legacy_fields_also_normalizes_dinner_subscriptions(service):
    await service.create_contact(
        {"custom_fields": {"dinner_subscriptions": "Mansion dinners with matching founders/investors"}}
    )
    report = await reconcile_legacy_fields(service)
    assert "dinner_subscriptions" in report["corrected"]
    assert report["dinner_subscriptions_contacts_updated"] == 1

    field = await service.custom_field_store.get_by_field_key("dinner_subscriptions")
    assert field.field_type == CustomFieldType.MULTI_SELECT


# --- Legacy -> canonical value translation for CSV import ---


def make_batch(rows: list[dict], headers: list[str] | None = None) -> CrmImportBatch:
    return CrmImportBatch(
        import_batch_id="b1", filename="p.csv", uploaded_at=datetime.now(timezone.utc),
        headers=headers or (list(rows[0].keys()) if rows else []), rows=rows, row_count=len(rows),
    )


def test_deal_stage_translates_abbreviated_values_to_canonical_text():
    batch = make_batch([{"Deal Stage": "Friends & family, Pre-seed, Seed, Series A"}])
    translated = translate_legacy_import_batch(batch)
    value = translated.rows[0]["Deal Stage"]
    assert value == (
        "Friends & Family (idea or concept stage, often pre-incorporation);"
        "Pre-Seed (early development, pre-revenue or minimal traction);"
        "Seed (product in market, early customers or pilots);"
        "Series A (scaling phase, revenue traction, team expansion)"
    )


def test_deal_stage_d_and_e_shorthand_map_to_series_b_or_later():
    batch = make_batch([{"Deal Stage": "Series B, Series C,D,E"}])
    translated = translate_legacy_import_batch(batch)
    value = translated.rows[0]["Deal Stage"]
    parts = value.split(";")
    assert parts == [
        "Series B or later (growth or expansion stage, institutional rounds)",
    ] * 4  # Series B, Series C, D, E all fold into the same canonical bucket


def test_asset_types_translate_to_canonical_with_parenthetical_examples_restored():
    batch = make_batch([{"Investing in these types of assets": "Collectibles, Venture capital"}])
    translated = translate_legacy_import_batch(batch)
    value = translated.rows[0]["Investing in these types of assets"]
    assert value == (
        "Collectibles (e.g., art, wine, watches);"
        "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)"
    )


def test_business_models_translate_to_canonical():
    batch = make_batch([{"Investing in these business models:": "Marketplaces, Software as a Service (SaaS)"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Investing in these business models:"] == (
        "Marketplaces (e.g., Airbnb, Uber-style platforms);Software as a Service (SaaS)"
    )


def test_demographic_preference_translates_founders_wording_to_fundraisers():
    batch = make_batch([{"Founder Diversity Preference": "Open to investing in anyone, Female founders"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Founder Diversity Preference"] == (
        "I'm open to investing in anyone;I prefer female fundraisers"
    )


def test_meeting_preference_translates_to_canonical():
    batch = make_batch([{"Would like to meet founders by": "Email intro, Zoom Call"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Would like to meet founders by"] == "In an email intro;I'd do a Zoom call"


def test_translate_leaves_unrelated_columns_untouched():
    batch = make_batch([{"Deal Stage": "Seed", "First Name": "Ada", "Email": "ada@example.com"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["First Name"] == "Ada"
    assert translated.rows[0]["Email"] == "ada@example.com"


def test_translate_skips_empty_cells():
    batch = make_batch([{"Deal Stage": "", "Founder Diversity Preference": "   "}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Deal Stage"] == ""
    assert translated.rows[0]["Founder Diversity Preference"] == "   "


def test_unrecognized_legacy_token_preserved_verbatim_not_dropped():
    batch = make_batch([{"Deal Stage": "Seed, Some Totally New Stage"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Deal Stage"] == (
        "Seed (product in market, early customers or pilots);Some Totally New Stage"
    )


# --- How early do you invest? -- known-phrase retokenization ---


def test_retokenize_handles_two_comma_containing_phrases_plus_simple_ones():
    raw = ("Great team, no revenue, Great team, some revenue, $10k-$50k MRR / GMV, "
           "$50k-$100k MRR / GMV, $100k-$1M MRR / GMV, $1M+ MRR / GMV")
    result = _retokenize_known_phrases(raw, HOW_EARLY_KNOWN_PHRASES)
    assert result == [
        "Great team, no revenue", "Great team, some revenue", "$10k-$50k MRR / GMV",
        "$50k-$100k MRR / GMV", "$100k-$1M MRR / GMV", "$1M+ MRR / GMV",
    ]


def test_retokenize_single_comma_containing_phrase_alone():
    assert _retokenize_known_phrases("Great team, some revenue", HOW_EARLY_KNOWN_PHRASES) == ["Great team, some revenue"]


def test_retokenize_does_not_confuse_the_two_similar_phrases():
    result = _retokenize_known_phrases("Great team, some revenue, $50k-$100k MRR / GMV", HOW_EARLY_KNOWN_PHRASES)
    assert result == ["Great team, some revenue", "$50k-$100k MRR / GMV"]
    assert "Great team, no revenue" not in result


def test_translate_batch_applies_how_early_retokenization():
    batch = make_batch([{"How early do you invest?": "Great team, no revenue, Great team, some revenue, $10k-$50k MRR / GMV"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["How early do you invest?"] == (
        "Great team, no revenue;Great team, some revenue;$10k-$50k MRR / GMV"
    )


# --- Extended translation: Investor Type / Dinners Attended comma->semicolon ---


def test_investor_type_comma_converted_to_semicolon_no_wording_change():
    batch = make_batch([{"Investor type": "Private Equity, Venture Capital"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Investor type"] == "Private Equity;Venture Capital"


def test_dinners_attended_comma_converted_to_semicolon():
    batch = make_batch([{"Dinners Attended": "Investor Dinners, GeneSilico [08.21.2025] Austin"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Dinners Attended"] == "Investor Dinners;GeneSilico [08.21.2025] Austin"


def test_investor_type_single_value_untouched_content():
    batch = make_batch([{"Investor type": "Angel Investor"}])
    translated = translate_legacy_import_batch(batch)
    assert translated.rows[0]["Investor type"] == "Angel Investor"


# --- Targeted repair for already-committed contacts ---


def make_repair_contact(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(crm_contact_id="c1", created_at=now, updated_at=now)
    defaults.update(overrides)
    return CrmContact(**defaults)


def test_repair_splits_multi_value_investor_type_from_source_snapshot():
    contact = make_repair_contact(
        custom_fields={"investor_type": ["Private Equity, Venture Capital"]},
        source_snapshot={"Investor type": "Private Equity, Venture Capital"},
    )
    updated, repaired = repair_contact_comma_delimited_fields(contact)
    assert repaired == ["investor_type"]
    assert updated.custom_fields["investor_type"] == ["Private Equity", "Venture Capital"]


def test_repair_leaves_genuinely_single_value_contact_untouched():
    contact = make_repair_contact(
        custom_fields={"investor_type": ["Angel Investor"]},
        source_snapshot={"Investor type": "Angel Investor"},
    )
    updated, repaired = repair_contact_comma_delimited_fields(contact)
    assert repaired == []
    assert updated is contact  # untouched -- same object, not even a no-op copy


def test_repair_does_not_touch_other_fields():
    contact = make_repair_contact(
        first_name="Ada", company="Acme",
        custom_fields={"investor_type": ["Private Equity, Venture Capital"], "gender": "Female"},
        source_snapshot={"Investor type": "Private Equity, Venture Capital"},
        thesis_dietary_preferences=["Vegan"],
    )
    updated, _ = repair_contact_comma_delimited_fields(contact)
    assert updated.first_name == "Ada"
    assert updated.company == "Acme"
    assert updated.custom_fields["gender"] == "Female"  # sibling custom field untouched
    assert updated.thesis_dietary_preferences == ["Vegan"]


def test_repair_handles_both_fields_on_one_contact():
    contact = make_repair_contact(
        custom_fields={
            "investor_type": ["Angel Investor, Family Office"],
            "dinners_attended": ["Investor Dinners, Fireside Dinners"],
        },
        source_snapshot={
            "Investor type": "Angel Investor, Family Office",
            "Dinners Attended": "Investor Dinners, Fireside Dinners",
        },
    )
    updated, repaired = repair_contact_comma_delimited_fields(contact)
    assert set(repaired) == {"investor_type", "dinners_attended"}
    assert updated.custom_fields["investor_type"] == ["Angel Investor", "Family Office"]
    assert updated.custom_fields["dinners_attended"] == ["Investor Dinners", "Fireside Dinners"]


def test_repair_is_idempotent():
    contact = make_repair_contact(
        custom_fields={"investor_type": ["Angel Investor", "Family Office"]},
        source_snapshot={"Investor type": "Angel Investor, Family Office"},
    )
    updated, repaired = repair_contact_comma_delimited_fields(contact)
    assert repaired == []  # already correct -- no-op, not re-saved


def test_repair_missing_source_snapshot_column_is_a_noop():
    contact = make_repair_contact(custom_fields={"investor_type": ["Angel Investor"]}, source_snapshot={})
    updated, repaired = repair_contact_comma_delimited_fields(contact)
    assert repaired == []


@pytest.mark.asyncio
async def test_repair_all_contacts_reports_accurate_counts(service):
    a = await service.create_contact({
        "custom_fields": {"investor_type": ["Private Equity, Venture Capital"]},
        "source_snapshot": {"Investor type": "Private Equity, Venture Capital"},
    })
    b = await service.create_contact({
        "custom_fields": {"dinners_attended": ["Investor Dinners, Fireside Dinners"]},
        "source_snapshot": {"Dinners Attended": "Investor Dinners, Fireside Dinners"},
    })
    c = await service.create_contact({
        "custom_fields": {"investor_type": ["Angel Investor"]},
        "source_snapshot": {"Investor type": "Angel Investor"},
    })  # genuinely single-value -- must not be touched

    report = await repair_all_contacts_comma_delimited_fields(service.contact_store)
    assert report == {
        "contacts_scanned": 3, "contacts_touched": 2,
        "investor_type_repaired": 1, "dinners_attended_repaired": 1,
    }

    assert (await service.get_contact(a.crm_contact_id)).custom_fields["investor_type"] == ["Private Equity", "Venture Capital"]
    assert (await service.get_contact(c.crm_contact_id)).custom_fields["investor_type"] == ["Angel Investor"]


# --- Funding Stage corruption cleanup (2026-08-06 discovery) ---
#
# LIVE_ENGAGEMENT_STAGE_OPTIONS mirrors production's real, corrected option list
# (CUSTOM_FIELD_CORRECTIONS["engagement_stage"] -- "Replied" added after the audit
# confirmed it's a legitimate outreach stage; "(No Stage)" deliberately excluded,
# since it's a null/unset placeholder, not a real stage).

LIVE_ENGAGEMENT_STAGE_OPTIONS = {"Cold", "Interested", "Unresponsive", "Replied"}


def test_funding_stage_interested_clears_and_sets_engagement_stage():
    contact = make_contact(funding_stage="Interested", source_snapshot={"Stage": "Interested"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared_and_engagement_set"
    assert updated.funding_stage is None
    assert updated.custom_fields["engagement_stage"] == "Interested"


def test_funding_stage_cold_clears_and_sets_engagement_stage():
    contact = make_contact(funding_stage="Cold", source_snapshot={"Stage": "Cold"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared_and_engagement_set"
    assert updated.funding_stage is None
    assert updated.custom_fields["engagement_stage"] == "Cold"


def test_funding_stage_unresponsive_clears_and_sets_engagement_stage():
    contact = make_contact(funding_stage="Unresponsive", source_snapshot={"Stage": "Unresponsive"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared_and_engagement_set"
    assert updated.funding_stage is None
    assert updated.custom_fields["engagement_stage"] == "Unresponsive"


def test_funding_stage_replied_clears_and_sets_engagement_stage():
    """"Replied" is a genuine value found in the CSVs (17 real occurrences) confirmed as
    a legitimate outreach stage -- CUSTOM_FIELD_CORRECTIONS added it as a real option, so
    it now clears funding_stage AND is preserved in engagement_stage, not just dropped."""
    contact = make_contact(funding_stage="Replied", source_snapshot={"Stage": "Replied"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared_and_engagement_set"
    assert updated.funding_stage is None
    assert updated.custom_fields["engagement_stage"] == "Replied"


def test_funding_stage_no_stage_placeholder_clears_but_leaves_engagement_stage_blank():
    """"(No Stage)" (1 real occurrence) is a null/unset placeholder, not a real stage --
    deliberately NOT added as an option. funding_stage is still cleared (proven corrupted:
    it exactly matches this contact's own raw Stage value), but nothing is written to
    engagement_stage -- information is never fabricated to fill a gap."""
    contact = make_contact(funding_stage="(No Stage)", source_snapshot={"Stage": "(No Stage)"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared"
    assert updated.funding_stage is None
    assert "engagement_stage" not in updated.custom_fields


def test_funding_stage_legitimate_value_never_touched():
    """The exact 29-contact case from the audit: a real funding-round term whose Stage
    column holds something completely different -- proves this value came from a
    legitimate source, not the Stage->funding_stage bug."""
    contact = make_contact(funding_stage="Seed", source_snapshot={"Stage": "Interested"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "legitimate"
    assert updated.funding_stage == "Seed"
    assert updated is contact  # untouched, not even a copy


def test_funding_stage_ambiguous_when_engagement_shaped_but_does_not_match_own_stage():
    """funding_stage LOOKS like an engagement value, but this contact's own Stage column
    doesn't match it -- can't mechanically prove the corruption, so it's flagged and left
    alone rather than guessed at."""
    contact = make_contact(funding_stage="Cold", source_snapshot={"Stage": "Interested"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "ambiguous"
    assert updated.funding_stage == "Cold"


def test_funding_stage_ambiguous_when_no_source_snapshot_stage_at_all():
    contact = make_contact(funding_stage="Interested", source_snapshot={})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "ambiguous"
    assert updated.funding_stage == "Interested"


def test_funding_stage_not_populated_is_a_noop():
    contact = make_contact(funding_stage=None, source_snapshot={"Stage": "Interested"})
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "not_populated"
    assert updated is contact


def test_funding_stage_cleared_without_overwriting_existing_engagement_stage():
    contact = make_contact(
        funding_stage="Cold",
        source_snapshot={"Stage": "Cold"},
        custom_fields={"engagement_stage": "Unresponsive"},  # already correctly set to something else
    )
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared"
    assert updated.funding_stage is None
    assert updated.custom_fields["engagement_stage"] == "Unresponsive"  # never overwritten


def test_funding_stage_cleared_never_touches_any_other_field():
    contact = make_contact(
        funding_stage="Interested",
        source_snapshot={"Stage": "Interested"},
        first_name="Ada", company="Acme", industry="SaaS",
        custom_fields={"gender": "Female"},
    )
    updated, outcome = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome == "cleared_and_engagement_set"
    assert updated.first_name == "Ada"
    assert updated.company == "Acme"
    assert updated.industry == "SaaS"
    assert updated.custom_fields["gender"] == "Female"


def test_funding_stage_migration_is_idempotent_on_a_single_contact():
    contact = make_contact(funding_stage="Interested", source_snapshot={"Stage": "Interested"})
    once, outcome1 = migrate_contact_funding_stage_corruption(contact, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    twice, outcome2 = migrate_contact_funding_stage_corruption(once, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert outcome1 == "cleared_and_engagement_set"
    assert outcome2 == "not_populated"  # funding_stage is already None -- no-op the second time
    assert twice.funding_stage is None
    assert twice.custom_fields["engagement_stage"] == "Interested"


@pytest.mark.asyncio
async def test_migrate_all_funding_stage_corruption_counts_every_outcome(service):
    await service.create_contact({"funding_stage": "Interested", "source_snapshot": {"Stage": "Interested"}})
    await service.create_contact({"funding_stage": "Replied", "source_snapshot": {"Stage": "Replied"}})
    await service.create_contact({"funding_stage": "(No Stage)", "source_snapshot": {"Stage": "(No Stage)"}})
    await service.create_contact({"funding_stage": "Seed", "source_snapshot": {"Stage": "Interested"}})
    await service.create_contact({"funding_stage": "Cold", "source_snapshot": {"Stage": "Interested"}})  # ambiguous
    await service.create_contact({"first_name": "No funding stage at all"})

    report = await migrate_all_funding_stage_corruption(service.contact_store, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert report == {
        "funding_stage_contacts_scanned": 6,
        "funding_stage_legitimate": 1,
        "funding_stage_ambiguous": 1,
        "funding_stage_cleared": 3,
        "funding_stage_engagement_stage_set": 2,  # Interested + Replied; "(No Stage)" cleared but not set
    }


@pytest.mark.asyncio
async def test_migrate_all_funding_stage_corruption_is_idempotent(service):
    await service.create_contact({"funding_stage": "Interested", "source_snapshot": {"Stage": "Interested"}})
    first = await migrate_all_funding_stage_corruption(service.contact_store, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    second = await migrate_all_funding_stage_corruption(service.contact_store, LIVE_ENGAGEMENT_STAGE_OPTIONS)
    assert first["funding_stage_cleared"] == 1
    assert second["funding_stage_cleared"] == 0  # already cleared -- no-op the second time


@pytest.mark.asyncio
async def test_reconcile_legacy_fields_also_clears_corrupted_funding_stage(service):
    """End-to-end through the real entry point: apply_custom_field_corrections() must run
    BEFORE the funding_stage cleanup so "Replied" is already a valid option by the time
    engagement_stage gets filled -- proves the two fixes compose correctly, not just in
    isolation."""
    await service.create_contact({"funding_stage": "Interested", "source_snapshot": {"Stage": "Interested"}})
    await service.create_contact({"funding_stage": "Replied", "source_snapshot": {"Stage": "Replied"}})
    await service.create_contact({"funding_stage": "(No Stage)", "source_snapshot": {"Stage": "(No Stage)"}})

    report = await reconcile_legacy_fields(service)
    assert report["funding_stage_cleared"] == 3
    assert report["funding_stage_engagement_stage_set"] == 2

    contacts = {c.source_snapshot["Stage"]: c for c in (await service.list_contacts()).items}
    assert contacts["Interested"].funding_stage is None
    assert contacts["Interested"].custom_fields["engagement_stage"] == "Interested"
    assert contacts["Replied"].funding_stage is None
    assert contacts["Replied"].custom_fields["engagement_stage"] == "Replied"
    assert contacts["(No Stage)"].funding_stage is None
    assert "engagement_stage" not in contacts["(No Stage)"].custom_fields


def test_funding_stage_engagement_shaped_values_matches_the_audit():
    assert FUNDING_STAGE_ENGAGEMENT_SHAPED_VALUES == {"Interested", "Cold", "Unresponsive", "Replied", "(No Stage)"}


@pytest.mark.asyncio
async def test_engagement_stage_correction_adds_replied_without_dropping_existing_options(service):
    """CUSTOM_FIELD_CORRECTIONS must ADD "Replied", never silently replace the existing
    Cold/Interested/Unresponsive options that were already correct."""
    await seed_legacy_custom_fields(service)
    await apply_custom_field_corrections(service)
    fields = await service.list_custom_fields()
    engagement_field = next(f for f in fields if f.field_key == "engagement_stage")
    assert set(engagement_field.options) == {"Cold", "Interested", "Unresponsive", "Replied"}
