"""
Tests for CrmService: manual contact CRUD, the dedup hierarchy, the two
merge rules (external fields overwrite only when incoming is non-empty;
thesis/custom fields never overwrite an existing value), custom field
definitions, search/filter, and Investor Thesis field handling.
"""

import pytest

from app.models.crm import CrmImportRowStatus, CustomFieldType
from app.services.crm_service import CrmContactNotFound, CrmDuplicateFieldKeyError, CrmService


@pytest.fixture
def service():
    return CrmService()


# --- Manual create/edit ---


@pytest.mark.asyncio
async def test_create_and_get_contact(service):
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"})
    fetched = await service.get_contact(contact.crm_contact_id)
    assert fetched.first_name == "Ada"
    assert fetched.created_at == fetched.updated_at


@pytest.mark.asyncio
async def test_create_contact_without_apollo_id_is_fine(service):
    """A CRM contact never needs an Apollo footprint -- unlike Lead."""
    contact = await service.create_contact({"first_name": "Someone", "last_name": "Met At A Conference"})
    assert contact.apollo_contact_id is None
    assert contact.crm_contact_id


@pytest.mark.asyncio
async def test_create_contact_rejects_duplicate_email(service):
    await service.create_contact({"email": "dup@example.com"})
    with pytest.raises(ValueError):
        await service.create_contact({"email": "DUP@example.com "})  # normalized match


@pytest.mark.asyncio
async def test_get_missing_contact_raises(service):
    with pytest.raises(CrmContactNotFound):
        await service.get_contact("does-not-exist")


@pytest.mark.asyncio
async def test_update_contact_sets_only_given_fields(service):
    contact = await service.create_contact({"first_name": "Ada", "company": "Acme"})
    updated = await service.update_contact(contact.crm_contact_id, {"company": "New Co"})
    assert updated.company == "New Co"
    assert updated.first_name == "Ada"  # untouched
    assert updated.updated_at > updated.created_at


@pytest.mark.asyncio
async def test_manual_edit_can_clear_a_field_directly(service):
    """Manual editing is NOT the import merge path -- a human can blank out a field on purpose."""
    contact = await service.create_contact({"company": "Acme"})
    updated = await service.update_contact(contact.crm_contact_id, {"company": ""})
    assert updated.company == ""


# --- thesis_investor_mode automation (derived from investor_type) ---


@pytest.mark.asyncio
async def test_create_contact_auto_derives_investor_mode_from_investor_type(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"
    assert contact.thesis_investor_mode_manual_override is False


@pytest.mark.asyncio
async def test_create_contact_with_no_investor_type_leaves_mode_unset(service):
    contact = await service.create_contact({"first_name": "Ada"})
    assert contact.thesis_investor_mode is None


@pytest.mark.asyncio
async def test_update_contact_recalculates_mode_when_investor_type_changes(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"investor_type": ["Angel Investor", "Venture Capital"]}}
    )
    assert updated.thesis_investor_mode == "Both"


@pytest.mark.asyncio
async def test_update_contact_removes_mode_when_last_private_type_removed(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"investor_type": []}})
    assert updated.thesis_investor_mode is None


@pytest.mark.asyncio
async def test_manual_override_prevents_automatic_recalculation(service):
    """A human's explicit Privately/Institutionally/Both choice survives an Investor Type change
    once thesis_investor_mode_manual_override is True -- the whole point of the flag."""
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    overridden = await service.update_contact(
        contact.crm_contact_id,
        {"thesis_investor_mode_manual_override": True, "thesis_investor_mode": "Institutionally"},
    )
    assert overridden.thesis_investor_mode == "Institutionally"

    # Investor Type changes again -- automation must NOT touch the manually-set value.
    unchanged = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"investor_type": ["Angel Investor", "Family Office"]}}
    )
    assert unchanged.thesis_investor_mode == "Institutionally"
    assert unchanged.thesis_investor_mode_manual_override is True


@pytest.mark.asyncio
async def test_turning_override_back_off_resumes_automation(service):
    contact = await service.create_contact(
        {"custom_fields": {"investor_type": ["Angel Investor"]}, "thesis_investor_mode_manual_override": True}
    )
    resumed = await service.update_contact(
        contact.crm_contact_id, {"thesis_investor_mode_manual_override": False}
    )
    assert resumed.thesis_investor_mode == "Privately"


@pytest.mark.asyncio
async def test_archive_is_soft_delete(service):
    contact = await service.create_contact({"first_name": "Ada"})
    archived = await service.archive_contact(contact.crm_contact_id)
    assert archived.archived is True
    # Still fetchable -- never hard-deleted.
    assert (await service.get_contact(contact.crm_contact_id)).archived is True


@pytest.mark.asyncio
async def test_list_contacts_hides_archived_by_default(service):
    a = await service.create_contact({"first_name": "Kept"})
    b = await service.create_contact({"first_name": "Archived"})
    await service.archive_contact(b.crm_contact_id)

    visible = await service.list_contacts()
    assert {c.crm_contact_id for c in visible.items} == {a.crm_contact_id}
    assert visible.total == 1

    everyone = await service.list_contacts(include_archived=True)
    assert {c.crm_contact_id for c in everyone.items} == {a.crm_contact_id, b.crm_contact_id}
    assert everyone.total == 2


# --- Search/filter ---


@pytest.mark.asyncio
async def test_list_contacts_filters_by_city_and_investor_mode(service):
    # thesis_investor_mode_manual_override=True is required here: with automation on
    # (the default), thesis_investor_mode is derived from investor_type and a direct
    # value would otherwise be overwritten -- see the automation tests above.
    await service.create_contact(
        {"first_name": "A", "city": "Austin", "thesis_investor_mode": "Privately", "thesis_investor_mode_manual_override": True}
    )
    await service.create_contact(
        {"first_name": "B", "city": "Denver", "thesis_investor_mode": "Both", "thesis_investor_mode_manual_override": True}
    )

    results = await service.list_contacts(city="Austin")
    assert [c.first_name for c in results.items] == ["A"]

    results = await service.list_contacts(investor_mode="Both")
    assert [c.first_name for c in results.items] == ["B"]


@pytest.mark.asyncio
async def test_list_contacts_filters_by_thesis_industry_across_private_and_institutional(service):
    await service.create_contact({"first_name": "A", "thesis_private_industries": ["SaaS / Software Infrastructure"]})
    await service.create_contact({"first_name": "B", "thesis_institutional_industries": ["Fintech (Finance & Insurance)"]})
    await service.create_contact({"first_name": "C", "thesis_private_industries": ["Biotech & Life Sciences"]})

    results = await service.list_contacts(industry="SaaS")
    assert {c.first_name for c in results.items} == {"A"}

    results = await service.list_contacts(industry="Fintech")
    assert {c.first_name for c in results.items} == {"B"}


@pytest.mark.asyncio
async def test_list_contacts_free_text_search(service):
    await service.create_contact({"first_name": "Ada", "company": "Analytical Engines Inc"})
    await service.create_contact({"first_name": "Grace", "company": "Compilers LLC"})

    results = await service.list_contacts(q="analytical")
    assert [c.first_name for c in results.items] == ["Ada"]


@pytest.mark.asyncio
async def test_list_contacts_free_text_search_covers_linkedin_and_thesis_lists(service):
    await service.create_contact({"first_name": "Ada", "linkedin_url": "https://linkedin.com/in/unique-ada-handle"})
    await service.create_contact({"first_name": "Grace", "thesis_private_industries": ["SaaS / Software Infrastructure"]})
    await service.create_contact({"first_name": "Irrelevant"})

    assert [c.first_name for c in (await service.list_contacts(q="unique-ada-handle")).items] == ["Ada"]
    assert [c.first_name for c in (await service.list_contacts(q="SaaS")).items] == ["Grace"]


# --- Pagination ---


@pytest.mark.asyncio
async def test_list_contacts_paginates_and_reports_total(service):
    for i in range(5):
        await service.create_contact({"first_name": f"Contact{i}"})

    page1 = await service.list_contacts(page=1, page_size=2)
    assert len(page1.items) == 2
    assert page1.total == 5
    assert page1.page == 1
    assert page1.page_size == 2

    page3 = await service.list_contacts(page=3, page_size=2)
    assert len(page3.items) == 1  # last, partial page
    assert page3.total == 5


@pytest.mark.asyncio
async def test_list_contacts_page_beyond_range_returns_empty_items_not_error(service):
    await service.create_contact({"first_name": "Solo"})
    result = await service.list_contacts(page=99, page_size=10)
    assert result.items == []
    assert result.total == 1


@pytest.mark.asyncio
async def test_list_contacts_pagination_applies_after_filtering(service):
    await service.create_contact({"first_name": "A", "city": "Austin"})
    await service.create_contact({"first_name": "B", "city": "Austin"})
    await service.create_contact({"first_name": "C", "city": "Denver"})

    result = await service.list_contacts(city="Austin", page=1, page_size=1)
    assert result.total == 2  # filtered count, not the full 3
    assert len(result.items) == 1


# --- Custom fields ---


@pytest.mark.asyncio
async def test_create_custom_field_and_reject_duplicate_key(service):
    field = await service.create_custom_field(
        field_key="dietary_preference", label="Dietary Preference", field_type=CustomFieldType.SINGLE_SELECT,
        options=["Vegetarian", "Vegan", "No restrictions"],
    )
    assert field.active is True
    with pytest.raises(CrmDuplicateFieldKeyError):
        await service.create_custom_field(field_key="dietary_preference", label="Dupe", field_type=CustomFieldType.TEXT)


@pytest.mark.asyncio
async def test_deactivating_custom_field_does_not_touch_existing_contact_data(service):
    field = await service.create_custom_field(field_key="fav_team", label="Favorite Team", field_type=CustomFieldType.TEXT)
    contact = await service.create_contact({"first_name": "Ada"})
    contact = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"fav_team": "Longhorns"}})

    await service.update_custom_field(field.crm_custom_field_id, {"active": False})

    fetched = await service.get_contact(contact.crm_contact_id)
    assert fetched.custom_fields == {"fav_team": "Longhorns"}  # untouched by deactivation

    active_only = await service.list_custom_fields(include_inactive=False)
    assert active_only == []


@pytest.mark.asyncio
async def test_new_custom_field_does_not_break_existing_contacts(service):
    """Adding a definition never requires a migration -- old rows simply lack the key."""
    contact = await service.create_contact({"first_name": "Ada"})
    await service.create_custom_field(field_key="new_field", label="New Field", field_type=CustomFieldType.TEXT)

    fetched = await service.get_contact(contact.crm_contact_id)
    assert fetched.custom_fields.get("new_field") is None  # no KeyError, no migration needed


# --- Dedup hierarchy (classify_match) ---


@pytest.mark.asyncio
async def test_classify_match_email_tier(service):
    existing = await service.create_contact({"email": "ada@example.com"})
    status, match, matched_on = await service.classify_match({"email": "ADA@EXAMPLE.COM"})
    assert status == CrmImportRowStatus.EXISTING
    assert match.crm_contact_id == existing.crm_contact_id
    assert matched_on == "email"


@pytest.mark.asyncio
async def test_classify_match_apollo_contact_id_tier(service):
    existing = await service.create_contact({"apollo_contact_id": "apollo-123"})
    status, match, matched_on = await service.classify_match({"apollo_contact_id": "apollo-123"})
    assert status == CrmImportRowStatus.EXISTING
    assert matched_on == "apollo_contact_id"
    assert match.crm_contact_id == existing.crm_contact_id


@pytest.mark.asyncio
async def test_classify_match_linkedin_tier(service):
    existing = await service.create_contact({"linkedin_url": "https://www.linkedin.com/in/ada/"})
    status, match, matched_on = await service.classify_match({"linkedin_url": "linkedin.com/in/ada"})
    assert status == CrmImportRowStatus.EXISTING
    assert matched_on == "linkedin_url"
    assert match.crm_contact_id == existing.crm_contact_id


@pytest.mark.asyncio
async def test_classify_match_name_company_fallback_is_possible_duplicate_not_existing(service):
    existing = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    status, match, matched_on = await service.classify_match({"first_name": "ada", "last_name": "LOVELACE", "company": "ACME"})
    assert status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert matched_on == "name_company"
    assert match.crm_contact_id == existing.crm_contact_id


@pytest.mark.asyncio
async def test_classify_match_no_fuzzy_matching(service):
    """A near-miss (typo) on the fallback tier must NOT match -- no fuzzy matching anywhere."""
    await service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    status, match, _ = await service.classify_match({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme Inc"})
    assert status == CrmImportRowStatus.NEW
    assert match is None


@pytest.mark.asyncio
async def test_classify_match_new_when_nothing_matches(service):
    status, match, matched_on = await service.classify_match({"email": "brandnew@example.com"})
    assert status == CrmImportRowStatus.NEW
    assert match is None
    assert matched_on is None


# --- Merge rules (apply_import_mapping) ---


@pytest.mark.asyncio
async def test_new_contact_gets_every_non_empty_mapped_field(service):
    contact = await service.create_contact_from_import({"first_name": "Ada", "company": "Acme", "thesis_dietary_preferences": "Vegetarian"})
    assert contact.first_name == "Ada"
    assert contact.company == "Acme"
    assert contact.thesis_dietary_preferences == "Vegetarian"


@pytest.mark.asyncio
async def test_external_field_overwritten_when_incoming_non_empty(service):
    contact = await service.create_contact({"company": "Old Co"})
    merged = service.apply_import_mapping(contact, {"company": "New Co"}, is_new=False)
    assert merged.company == "New Co"


@pytest.mark.asyncio
async def test_external_field_blank_incoming_never_erases_existing_value(service):
    """The exact example from the spec: existing Company must survive a blank incoming Company."""
    contact = await service.create_contact({"company": "Acme Technologies"})
    merged = service.apply_import_mapping(contact, {"company": ""}, is_new=False)
    assert merged.company == "Acme Technologies"


@pytest.mark.asyncio
async def test_thesis_field_never_overwritten_when_existing_value_present(service):
    contact = await service.create_contact({"thesis_dietary_preferences": "Vegetarian"})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences": "Vegan"}, is_new=False)
    assert merged.thesis_dietary_preferences == "Vegetarian"  # preserved, not silently replaced


@pytest.mark.asyncio
async def test_thesis_field_filled_in_when_currently_empty(service):
    contact = await service.create_contact({"first_name": "Ada"})  # thesis_dietary_preferences unset
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences": "Vegan"}, is_new=False)
    assert merged.thesis_dietary_preferences == "Vegan"


@pytest.mark.asyncio
async def test_thesis_list_field_never_overwritten_when_existing_present(service):
    contact = await service.create_contact({"thesis_private_industries": ["Biotech & Life Sciences"]})
    merged = service.apply_import_mapping(
        contact, {"thesis_private_industries": ["SaaS / Software Infrastructure"]}, is_new=False
    )
    assert merged.thesis_private_industries == ["Biotech & Life Sciences"]


@pytest.mark.asyncio
async def test_custom_field_follows_the_same_never_overwrite_rule(service):
    contact = await service.create_contact({})
    contact = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"fav_team": "Longhorns"}})

    merged = service.apply_import_mapping(contact, {"custom:fav_team": "Aggies"}, is_new=False)
    assert merged.custom_fields["fav_team"] == "Longhorns"  # preserved


@pytest.mark.asyncio
async def test_multiple_custom_fields_in_one_row_all_survive(service):
    """Regression guard: an earlier bug overwrote all but the last custom field in one merge call."""
    contact = await service.create_contact({})
    merged = service.apply_import_mapping(
        contact, {"custom:a": "1", "custom:b": "2", "custom:c": "3"}, is_new=False
    )
    assert merged.custom_fields == {"a": "1", "b": "2", "c": "3"}


@pytest.mark.asyncio
async def test_source_snapshot_always_replaced_not_merged(service):
    contact = await service.create_contact({"source_snapshot": {"old": "data"}})
    merged = service.apply_import_mapping(contact, {"source_snapshot": {"new": "data"}}, is_new=False)
    assert merged.source_snapshot == {"new": "data"}


@pytest.mark.asyncio
async def test_unknown_mapped_field_name_is_ignored_not_guessed_at(service):
    contact = await service.create_contact({"first_name": "Ada"})
    merged = service.apply_import_mapping(contact, {"totally_made_up_field": "value"}, is_new=False)
    assert not hasattr(merged, "totally_made_up_field")
    assert merged.first_name == "Ada"


# --- Regression: PATCH must merge custom_fields, never wipe siblings ---


@pytest.mark.asyncio
async def test_update_contact_merges_custom_fields_does_not_wipe_siblings(service):
    """Found live during QA: sending {"custom_fields": {"one_key": val}} must not erase
    every other custom field already on the contact."""
    contact = await service.create_contact({
        "custom_fields": {"gender": "Male", "investor_type": ["Angel Investor", "Family Office"]}
    })
    updated = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"notes": "new note"}})

    assert updated.custom_fields == {
        "gender": "Male",
        "investor_type": ["Angel Investor", "Family Office"],
        "notes": "new note",
    }


@pytest.mark.asyncio
async def test_update_contact_custom_fields_merge_can_still_overwrite_a_shared_key(service):
    contact = await service.create_contact({"custom_fields": {"gender": "Male"}})
    updated = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"gender": "Female"}})
    assert updated.custom_fields == {"gender": "Female"}
