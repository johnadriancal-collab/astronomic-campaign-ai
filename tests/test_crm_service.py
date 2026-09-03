"""
Tests for CrmService: manual contact CRUD, the dedup hierarchy, the two
merge rules (external fields overwrite only when incoming is non-empty;
thesis/custom fields never overwrite an existing value), custom field
definitions, search/filter, and Investor Thesis field handling.
"""

import pytest

from app.models.crm import CrmContact, CrmImportRowStatus, CustomFieldType
from app.services.crm_service import CrmContactListNotFound, CrmContactNotFound, CrmDuplicateFieldKeyError, CrmService


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


# --- thesis_investor_mode must NOT recompute on unrelated edits (2026-08-12 fix) ---
# Root cause: before this fix, update_contact() recomputed thesis_investor_mode from
# custom_fields["investor_type"] on EVERY call where manual_override was False,
# regardless of whether the patch touched investor_type at all. For an ITF-created
# contact -- whose custom_fields never has an "investor_type" key at all, since ITF
# derives thesis_investor_mode from a different question entirely -- this meant ANY
# unrelated edit (Notes, Investment Industry, Check Size, Company, ...), including via
# the frontend's normal full-object save, silently erased a valid thesis_investor_mode
# by recomputing derive_investor_mode(None) -> None.


@pytest.mark.asyncio
async def test_patching_investment_industry_does_not_touch_investor_mode(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(
        contact.crm_contact_id,
        {"custom_fields": {"investment_industry": ["Aerospace & Defense"]}},
    )
    assert updated.thesis_investor_mode == "Privately"
    assert updated.custom_fields["investor_type"] == ["Angel Investor"]  # preserved, not touched either


@pytest.mark.asyncio
async def test_patching_unrelated_scalar_field_does_not_touch_investor_mode(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}, "company": "Acme"})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(contact.crm_contact_id, {"company": "New Co"})
    assert updated.thesis_investor_mode == "Privately"
    assert updated.company == "New Co"


@pytest.mark.asyncio
async def test_patching_unrelated_custom_field_does_not_touch_investor_mode(service):
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"notes": "Met at conference"}})
    assert updated.thesis_investor_mode == "Privately"
    assert updated.custom_fields["notes"] == "Met at conference"
    assert updated.custom_fields["investor_type"] == ["Angel Investor"]


@pytest.mark.asyncio
async def test_patching_investor_type_itself_still_recomputes_investor_mode(service):
    """The one thing that SHOULD still trigger recomputation -- confirms the fix didn't
    accidentally disable the feature entirely, only scope it to when it's warranted."""
    contact = await service.create_contact({"custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"investor_type": ["Angel Investor", "Venture Capital"]}}
    )
    assert updated.thesis_investor_mode == "Both"


@pytest.mark.asyncio
async def test_manual_override_true_plus_investor_type_change_keeps_manual_value(service):
    contact = await service.create_contact(
        {
            "custom_fields": {"investor_type": ["Angel Investor"]},
            "thesis_investor_mode_manual_override": True,
            "thesis_investor_mode": "Institutionally",
        }
    )
    assert contact.thesis_investor_mode == "Institutionally"

    updated = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"investor_type": ["Angel Investor", "Family Office"]}}
    )
    assert updated.thesis_investor_mode == "Institutionally"  # untouched -- override still True
    assert updated.thesis_investor_mode_manual_override is True


@pytest.mark.asyncio
async def test_itf_contact_with_no_investor_type_can_be_edited_without_losing_investor_mode(service):
    """The exact real-world shape of contact 5aa964d0-7923-49c6-95bc-c0f8f11dea6a: an
    ITF-created contact with thesis_investor_mode set directly (never through
    investor_type) and NO "investor_type" key in custom_fields at all. Before this fix,
    this edit would have silently nulled thesis_investor_mode.

    Uses create_contact_from_import (not create_contact) deliberately: that's the
    actual code path real ITF submissions go through (via CrmImportService.import_one_row
    -> create_contact_from_import), and unlike create_contact, it does NOT recompute
    thesis_investor_mode at creation time -- it applies whatever value the caller (ITF's
    "Do you invest privately or institutionally?" question, in the real case) already
    supplied. This is what makes contact 5aa964d0-7923-49c6-95bc-c0f8f11dea6a's exact
    real shape (thesis_investor_mode="Privately", no investor_type key at all)
    reproducible in a test."""
    contact = await service.create_contact_from_import(
        {
            "thesis_investor_mode": "Privately",
            "custom:check_size_personal": ["$1k - $10k"],
        }
    )
    assert contact.thesis_investor_mode == "Privately"
    assert "investor_type" not in contact.custom_fields

    updated = await service.update_contact(
        contact.crm_contact_id,
        {"custom_fields": {"investment_industry": ["Aerospace & Defense", "AgTech & Food Production"]}},
    )
    assert updated.thesis_investor_mode == "Privately"  # preserved
    assert updated.custom_fields["investment_industry"] == ["Aerospace & Defense", "AgTech & Food Production"]
    assert updated.custom_fields["check_size_personal"] == ["$1k - $10k"]  # preserved


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
async def test_list_contacts_filters_by_check_size_across_personal_and_institutional(service):
    """2026-08-06 Check Size consolidation: the check_size filter now reads
    check_size_personal/check_size_institutional (custom fields, the canonical
    destination), not the deprecated thesis_private_check_sizes/
    thesis_institutional_check_sizes fields -- confirms the filter actually
    finds contacts whose real data lives where CSV import and the UI put it."""
    await service.create_contact({
        "first_name": "A", "custom_fields": {"check_size_personal": ["$25k - $50k", "$50k - $100k"]},
    })
    await service.create_contact({
        "first_name": "B", "custom_fields": {"check_size_institutional": ["$1M - $2M"]},
    })
    await service.create_contact({"first_name": "C", "custom_fields": {"check_size_personal": ["$10M+"]}})

    results = await service.list_contacts(check_size="$25k - $50k")
    assert {c.first_name for c in results.items} == {"A"}

    results = await service.list_contacts(check_size="$1M - $2M")
    assert {c.first_name for c in results.items} == {"B"}


@pytest.mark.asyncio
async def test_list_contacts_check_size_filter_never_matches_deprecated_thesis_fields():
    """A value that exists ONLY in the deprecated thesis fields (never migrated
    to the canonical custom field) must NOT match -- this filter deliberately
    stopped reading thesis_private_check_sizes/thesis_institutional_check_sizes."""
    service = CrmService()
    await service.create_contact({"first_name": "Legacy", "thesis_private_check_sizes": ["$1M - $2M"]})

    results = await service.list_contacts(check_size="$1M - $2M")
    assert results.items == []


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


@pytest.mark.asyncio
async def test_classify_match_email_with_matching_name_is_existing(service):
    james = await service.create_contact({"first_name": "James", "last_name": "Feldkamp", "email": "jfeldkamp@alarycapital.com"})
    status, match, matched_on = await service.classify_match(
        {"first_name": "James", "last_name": "Feldkamp", "email": "jfeldkamp@alarycapital.com"}
    )
    assert status == CrmImportRowStatus.EXISTING
    assert match.crm_contact_id == james.crm_contact_id
    assert matched_on == "email"


@pytest.mark.asyncio
async def test_classify_match_email_with_conflicting_name_is_possible_duplicate(service):
    """The exact repeat-upload scenario: James Feldkamp already exists (created by an
    earlier import). A LATER row -- from the same or a future CSV -- shares his email
    but is actually a different person (Shawn Riely, per a real source-data error). This
    must be flagged for human review, never silently merged into James's record."""
    james = await service.create_contact({"first_name": "James", "last_name": "Feldkamp", "email": "jfeldkamp@alarycapital.com"})
    status, match, matched_on = await service.classify_match(
        {"first_name": "Shawn", "last_name": "Riely", "email": "jfeldkamp@alarycapital.com"}
    )
    assert status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert match.crm_contact_id == james.crm_contact_id
    assert matched_on == "email_conflicting_identity"


@pytest.mark.asyncio
async def test_classify_match_email_with_partial_name_match_is_not_a_conflict(service):
    """The Carlos Oviedo case: same first name, different last name. Treated as
    the SAME identity (a plausible nickname/data-entry variant), not a conflict --
    unlike Feldkamp/Riely where NEITHER name matches at all. The separate
    never-overwrite-populated-field rule already protects his real last name
    from being replaced by this row's."""
    carlos = await service.create_contact({"first_name": "Carlos", "last_name": "Oviedo", "email": "carlos@carloscardenas.com"})
    status, match, matched_on = await service.classify_match(
        {"first_name": "Carlos", "last_name": "Cardenas", "email": "carlos@carloscardenas.com"}
    )
    assert status == CrmImportRowStatus.EXISTING
    assert match.crm_contact_id == carlos.crm_contact_id
    assert matched_on == "email"


@pytest.mark.asyncio
async def test_classify_match_email_match_with_no_name_on_either_side_is_still_existing(service):
    """No name data to conflict on at all -- falls back to the ordinary EXISTING match."""
    contact = await service.create_contact({"email": "known@example.com"})
    status, match, matched_on = await service.classify_match({"email": "known@example.com"})
    assert status == CrmImportRowStatus.EXISTING
    assert match.crm_contact_id == contact.crm_contact_id
    assert matched_on == "email"


# --- Merge rules (apply_import_mapping) ---


@pytest.mark.asyncio
async def test_new_contact_gets_every_non_empty_mapped_field(service):
    contact = await service.create_contact_from_import({"first_name": "Ada", "company": "Acme", "thesis_dietary_preferences": "Vegetarian"})
    assert contact.first_name == "Ada"
    assert contact.company == "Acme"
    assert contact.thesis_dietary_preferences == "Vegetarian"


@pytest.mark.asyncio
async def test_external_field_filled_in_when_currently_empty(service):
    contact = await service.create_contact({"first_name": "Ada"})  # company unset
    merged = service.apply_import_mapping(contact, {"company": "New Co"}, is_new=False)
    assert merged.company == "New Co"


@pytest.mark.asyncio
async def test_external_field_never_overwritten_by_a_conflicting_non_empty_value(service):
    """Changed behavior: a re-imported contact whose CSV row has a genuinely
    different LinkedIn URL/Company/etc. than what's already on file must NOT
    have that existing value silently replaced -- a source-data conflict is a
    question for a human, not something import should resolve by picking a side."""
    contact = await service.create_contact({"company": "Old Co"})
    merged = service.apply_import_mapping(contact, {"company": "New Co"}, is_new=False)
    assert merged.company == "Old Co"  # preserved, not overwritten


@pytest.mark.asyncio
async def test_carlos_oviedo_linkedin_and_company_conflict_preserved(service):
    """The exact real-world case that triggered this change: Carlos Oviedo's
    correct LinkedIn/Company must survive a newer CSV export whose row for his
    email contains a different, likely-mismatched LinkedIn/Company."""
    contact = await service.create_contact({
        "email": "carlos@carloscardenas.com",
        "linkedin_url": "http://www.linkedin.com/in/carlosoviedoc",
        "company": "ISITA®",
    })
    merged = service.apply_import_mapping(
        contact,
        {"linkedin_url": "http://www.linkedin.com/in/carloscardenastx", "company": "Austin Wealth Management"},
        is_new=False,
    )
    assert merged.linkedin_url == "http://www.linkedin.com/in/carlosoviedoc"
    assert merged.company == "ISITA®"


@pytest.mark.asyncio
async def test_external_field_blank_incoming_never_erases_existing_value(service):
    """The exact example from the spec: existing Company must survive a blank incoming Company."""
    contact = await service.create_contact({"company": "Acme Technologies"})
    merged = service.apply_import_mapping(contact, {"company": ""}, is_new=False)
    assert merged.company == "Acme Technologies"


@pytest.mark.asyncio
async def test_thesis_field_never_overwritten_when_existing_value_present(service):
    contact = await service.create_contact({"thesis_referral_emails": "referred by Jane"})
    merged = service.apply_import_mapping(contact, {"thesis_referral_emails": "referred by John"}, is_new=False)
    assert merged.thesis_referral_emails == "referred by Jane"  # preserved, not silently replaced


@pytest.mark.asyncio
async def test_thesis_field_filled_in_when_currently_empty(service):
    contact = await service.create_contact({"first_name": "Ada"})  # thesis_referral_emails unset
    merged = service.apply_import_mapping(contact, {"thesis_referral_emails": "referred by John"}, is_new=False)
    assert merged.thesis_referral_emails == "referred by John"


@pytest.mark.asyncio
async def test_thesis_list_field_never_overwritten_when_existing_present(service):
    contact = await service.create_contact({"thesis_private_industries": ["Biotech & Life Sciences"]})
    merged = service.apply_import_mapping(
        contact, {"thesis_private_industries": ["SaaS / Software Infrastructure"]}, is_new=False
    )
    assert merged.thesis_private_industries == ["Biotech & Life Sciences"]


@pytest.mark.asyncio
async def test_dietary_preferences_union_merges_instead_of_overwriting(service):
    """2026-08-07 requirement: existing Vegetarian + new Gluten-Free = both, never one
    replacing the other -- the exact example from the spec."""
    contact = await service.create_contact({"thesis_dietary_preferences": ["Vegetarian"]})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences": ["Gluten-Free"]}, is_new=False)
    assert merged.thesis_dietary_preferences == ["Vegetarian", "Gluten-Free"]


@pytest.mark.asyncio
async def test_dietary_preferences_union_merge_deduplicates_exact_repeats(service):
    contact = await service.create_contact({"thesis_dietary_preferences": ["Vegetarian", "Gluten-Free"]})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences": ["Gluten-Free"]}, is_new=False)
    assert merged.thesis_dietary_preferences == ["Vegetarian", "Gluten-Free"]  # no duplicate added


@pytest.mark.asyncio
async def test_dietary_preferences_filled_in_when_currently_empty(service):
    contact = await service.create_contact({"first_name": "Ada"})  # thesis_dietary_preferences unset
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences": ["Vegan"]}, is_new=False)
    assert merged.thesis_dietary_preferences == ["Vegan"]


@pytest.mark.asyncio
async def test_dietary_preferences_never_loses_a_value_across_repeated_imports(service):
    """Explicit spec requirement: never remove an existing dietary preference just
    because a later CSV row doesn't repeat it."""
    contact = await service.create_contact({"thesis_dietary_preferences": ["Vegetarian"]})
    merged1 = service.apply_import_mapping(contact, {"thesis_dietary_preferences": ["Nut-Free"]}, is_new=False)
    merged2 = service.apply_import_mapping(merged1, {"thesis_dietary_preferences": ["Alcohol-Free"]}, is_new=False)
    assert merged2.thesis_dietary_preferences == ["Vegetarian", "Nut-Free", "Alcohol-Free"]


@pytest.mark.asyncio
async def test_dietary_preferences_other_stores_first_unrecognized_value_directly(service):
    contact = await service.create_contact({"first_name": "Ada"})  # thesis_dietary_preferences_other unset
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences_other": "Cayenne-Free"}, is_new=False)
    assert merged.thesis_dietary_preferences_other == "Cayenne-Free"


@pytest.mark.asyncio
async def test_dietary_preferences_other_appends_a_new_value_with_semicolon(service):
    contact = await service.create_contact({"thesis_dietary_preferences_other": "Cayenne-Free"})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences_other": "Beets-Free"}, is_new=False)
    assert merged.thesis_dietary_preferences_other == "Cayenne-Free; Beets-Free"


@pytest.mark.asyncio
async def test_dietary_preferences_other_does_not_duplicate_an_existing_value(service):
    contact = await service.create_contact({"thesis_dietary_preferences_other": "Cayenne-Free; Beets-Free"})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences_other": "Beets-Free"}, is_new=False)
    assert merged.thesis_dietary_preferences_other == "Cayenne-Free; Beets-Free"  # unchanged, no duplicate


@pytest.mark.asyncio
async def test_dietary_preferences_other_never_overwritten_by_a_completely_different_value(service):
    """Appended, not replaced -- the existing value must always survive in the merged result."""
    contact = await service.create_contact({"thesis_dietary_preferences_other": "Cayenne-Free"})
    merged = service.apply_import_mapping(contact, {"thesis_dietary_preferences_other": "Arugula-Free"}, is_new=False)
    assert "Cayenne-Free" in merged.thesis_dietary_preferences_other
    assert "Arugula-Free" in merged.thesis_dietary_preferences_other


@pytest.mark.asyncio
async def test_custom_field_follows_the_same_never_overwrite_rule(service):
    contact = await service.create_contact({})
    contact = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"fav_team": "Longhorns"}})

    merged = service.apply_import_mapping(contact, {"custom:fav_team": "Aggies"}, is_new=False)
    assert merged.custom_fields["fav_team"] == "Longhorns"  # preserved


@pytest.mark.asyncio
async def test_multi_select_custom_field_merges_instead_of_replacing(service):
    """The Carlos Oviedo case: existing selections are never dropped, and new
    incoming selections not already present are added -- union, not replace."""
    contact = await service.create_contact({})
    contact = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"dinner_subscriptions": ["Investor Dinners", "Founder Dinners", "Biz Dev Dinners"]}}
    )

    merged = service.apply_import_mapping(
        contact, {"custom:dinner_subscriptions": ["Investor Dinners", "Founder Dinners", "Not actively Investing"]}, is_new=False
    )
    assert merged.custom_fields["dinner_subscriptions"] == [
        "Investor Dinners", "Founder Dinners", "Biz Dev Dinners", "Not actively Investing",
    ]


@pytest.mark.asyncio
async def test_multi_select_custom_field_merge_deduplicates_exact_repeats(service):
    contact = await service.create_contact({})
    contact = await service.update_contact(
        contact.crm_contact_id, {"custom_fields": {"dinners_attended": ["Investor Dinners", "Fireside Dinners"]}}
    )
    merged = service.apply_import_mapping(
        contact, {"custom:dinners_attended": ["Investor Dinners", "Fireside Dinners"]}, is_new=False
    )
    assert merged.custom_fields["dinners_attended"] == ["Investor Dinners", "Fireside Dinners"]  # no duplicates added


@pytest.mark.asyncio
async def test_multi_select_custom_field_merge_fills_from_empty(service):
    """No existing value at all -- incoming list is simply set, same as before."""
    contact = await service.create_contact({})
    merged = service.apply_import_mapping(contact, {"custom:dinners_attended": ["Investor Dinners"]}, is_new=False)
    assert merged.custom_fields["dinners_attended"] == ["Investor Dinners"]


@pytest.mark.asyncio
async def test_investment_industry_union_merges_itf_values_into_existing_contact(service):
    """The generic multi-select custom-field union-merge rule (already proven above
    for dinner_subscriptions/dinners_attended) applies to investment_industry too,
    with zero field-specific code -- an existing CSV-derived Investment Industry
    selection is never dropped when an ITF submission adds new industries."""
    contact = await service.create_contact({})
    contact = await service.update_contact(
        contact.crm_contact_id,
        {"custom_fields": {"investment_industry": ["Information Technology (IT)", "Real Estate"]}},
    )
    merged = service.apply_import_mapping(
        contact,
        {"custom:investment_industry": ["Cybersecurity", "Real Estate"]},  # "Real Estate" is an exact repeat
        is_new=False,
    )
    assert merged.custom_fields["investment_industry"] == [
        "Information Technology (IT)", "Real Estate", "Cybersecurity",
    ]


@pytest.mark.asyncio
async def test_multi_select_custom_field_never_removes_existing_values(service):
    """Even if the incoming list is a STRICT SUBSET of the existing one (e.g. an
    older/incomplete CSV re-imported after a richer one), nothing already stored is
    ever dropped -- merge is purely additive."""
    contact = await service.create_contact({})
    contact = await service.update_contact(
        contact.crm_contact_id,
        {"custom_fields": {"dinner_subscriptions": ["Investor Dinners", "Founder Dinners", "Fireside Dinners"]}},
    )
    merged = service.apply_import_mapping(contact, {"custom:dinner_subscriptions": ["Investor Dinners"]}, is_new=False)
    assert merged.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners", "Fireside Dinners"]


@pytest.mark.asyncio
async def test_single_select_custom_field_blank_incoming_never_erases_existing_value(service):
    """Chris Degree Connection / Age Range / Gender-style fields: a blank CSV cell
    must never erase an existing non-empty value -- classification rules never emit
    a key at all for a blank cell, but this guards the merge layer directly too."""
    contact = await service.create_contact({})
    contact = await service.update_contact(contact.crm_contact_id, {"custom_fields": {"age_range": "31-40"}})
    merged = service.apply_import_mapping(contact, {"custom:age_range": ""}, is_new=False)
    assert merged.custom_fields["age_range"] == "31-40"


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


@pytest.mark.asyncio
async def test_deprecated_check_size_thesis_fields_are_never_written_via_import_mapping(service):
    """2026-08-06 Check Size consolidation: thesis_private_check_sizes/
    thesis_institutional_check_sizes were removed from THESIS_FIELD_NAMES, so
    apply_import_mapping() now refuses to write to them even if a mapped_fields
    dict explicitly targets them directly (e.g. a hand-crafted column_mapping) --
    the same "unmapped/unknown target -- ignored" path as a made-up field name."""
    contact = await service.create_contact({"first_name": "Ada"})
    merged = service.apply_import_mapping(
        contact,
        {"thesis_private_check_sizes": ["$1M - $2M"], "thesis_institutional_check_sizes": ["$10M+"]},
        is_new=False,
    )
    assert merged.thesis_private_check_sizes == []
    assert merged.thesis_institutional_check_sizes == []

    # Even for a brand-new contact (is_new=True normally sets every mapped field
    # directly, nothing to protect) -- these two remain unwritable, since the gate
    # is "is this field name recognized at all," checked before the is_new branch.
    created = service.apply_import_mapping(
        CrmContact(crm_contact_id="c-new", created_at=contact.created_at, updated_at=contact.updated_at),
        {"first_name": "Nova", "thesis_private_check_sizes": ["$1M - $2M"]},
        is_new=True,
    )
    assert created.first_name == "Nova"
    assert created.thesis_private_check_sizes == []


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


# --- Lists: named, persistent groupings of existing contacts ---


@pytest.mark.asyncio
async def test_create_and_get_contact_list(service):
    created = await service.create_contact_list(name="Austin Family Offices", description="Prospecting")
    assert created.contact_count == 0
    fetched = await service.get_contact_list(created.list_id)
    assert fetched.name == "Austin Family Offices"
    assert fetched.description == "Prospecting"


@pytest.mark.asyncio
async def test_create_contact_list_without_description(service):
    created = await service.create_contact_list(name="AI Investors")
    assert created.description is None


@pytest.mark.asyncio
async def test_get_missing_list_raises(service):
    with pytest.raises(CrmContactListNotFound):
        await service.get_contact_list("does-not-exist")


@pytest.mark.asyncio
async def test_list_contact_lists_includes_contact_count(service):
    contact = await service.create_contact({"first_name": "Ada"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    summaries = await service.list_contact_lists()
    summary = next(s for s in summaries if s.list_id == contact_list.list_id)
    assert summary.contact_count == 1


@pytest.mark.asyncio
async def test_update_contact_list_renames_and_edits_description(service):
    contact_list = await service.create_contact_list(name="Old Name")
    updated = await service.update_contact_list(contact_list.list_id, {"name": "New Name", "description": "New desc"})
    assert updated.name == "New Name"
    assert updated.description == "New desc"


@pytest.mark.asyncio
async def test_update_contact_list_ignores_disallowed_keys(service):
    contact_list = await service.create_contact_list(name="Name")
    updated = await service.update_contact_list(contact_list.list_id, {"list_id": "hacked", "name": "Still Name"})
    assert updated.list_id == contact_list.list_id
    assert updated.name == "Still Name"


@pytest.mark.asyncio
async def test_update_missing_list_raises(service):
    with pytest.raises(CrmContactListNotFound):
        await service.update_contact_list("does-not-exist", {"name": "X"})


@pytest.mark.asyncio
async def test_bulk_add_to_list_reports_added_and_already_member(service):
    c1 = await service.create_contact({"first_name": "Ada"})
    c2 = await service.create_contact({"first_name": "Grace"})
    contact_list = await service.create_contact_list(name="Test List")

    first = await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c2.crm_contact_id])
    assert first.added == 2
    assert first.already_member == 0
    assert first.not_found == 0

    second = await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c2.crm_contact_id])
    assert second.added == 0
    assert second.already_member == 2


@pytest.mark.asyncio
async def test_bulk_add_to_list_mixed_new_and_already_member(service):
    c1 = await service.create_contact({"first_name": "Ada"})
    c2 = await service.create_contact({"first_name": "Grace"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])

    result = await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c2.crm_contact_id])
    assert result.added == 1
    assert result.already_member == 1


@pytest.mark.asyncio
async def test_bulk_add_to_list_reports_not_found_without_creating_membership(service):
    contact_list = await service.create_contact_list(name="Test List")
    result = await service.bulk_add_to_list(contact_list.list_id, ["does-not-exist"])
    assert result.not_found == 1
    assert result.added == 0
    page = await service.get_list_contacts(contact_list.list_id)
    assert page.total == 0


@pytest.mark.asyncio
async def test_bulk_add_to_list_deduplicates_repeated_ids_in_one_request(service):
    c1 = await service.create_contact({"first_name": "Ada"})
    contact_list = await service.create_contact_list(name="Test List")
    result = await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c1.crm_contact_id])
    assert result.added == 1
    assert result.already_member == 0


@pytest.mark.asyncio
async def test_bulk_add_to_missing_list_raises(service):
    with pytest.raises(CrmContactListNotFound):
        await service.bulk_add_to_list("does-not-exist", [])


@pytest.mark.asyncio
async def test_same_contact_can_join_multiple_lists(service):
    contact = await service.create_contact({"first_name": "Jane"})
    list_a = await service.create_contact_list(name="Austin Family Offices")
    list_b = await service.create_contact_list(name="AI Investors")

    await service.bulk_add_to_list(list_a.list_id, [contact.crm_contact_id])
    await service.bulk_add_to_list(list_b.list_id, [contact.crm_contact_id])

    page_a = await service.get_list_contacts(list_a.list_id)
    page_b = await service.get_list_contacts(list_b.list_id)
    assert [c.crm_contact_id for c in page_a.items] == [contact.crm_contact_id]
    assert [c.crm_contact_id for c in page_b.items] == [contact.crm_contact_id]


@pytest.mark.asyncio
async def test_get_list_contacts_reflects_live_contact_data_not_a_copy(service):
    """The crux of 'never store full contact copies' -- editing a contact after
    it's added to a list must change what the list shows, with no re-add needed."""
    contact = await service.create_contact({"first_name": "Ada", "company": "Old Co"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    await service.update_contact(contact.crm_contact_id, {"company": "New Co"})

    page = await service.get_list_contacts(contact_list.list_id)
    assert page.items[0].company == "New Co"


@pytest.mark.asyncio
async def test_adding_to_a_list_does_not_change_the_contact(service):
    """List membership is separate from the contact record -- adding to a list
    must never touch updated_at or any field on the CrmContact itself."""
    contact = await service.create_contact({"first_name": "Ada"})
    before = await service.get_contact(contact.crm_contact_id)
    contact_list = await service.create_contact_list(name="Test List")

    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    after = await service.get_contact(contact.crm_contact_id)
    assert after.updated_at == before.updated_at
    assert after == before


@pytest.mark.asyncio
async def test_removing_from_a_list_does_not_change_the_contact(service):
    contact = await service.create_contact({"first_name": "Ada"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    before = await service.get_contact(contact.crm_contact_id)

    await service.remove_contact_from_list(contact_list.list_id, contact.crm_contact_id)

    after = await service.get_contact(contact.crm_contact_id)
    assert after.updated_at == before.updated_at
    assert after == before


@pytest.mark.asyncio
async def test_remove_contact_from_list(service):
    contact = await service.create_contact({"first_name": "Ada"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    await service.remove_contact_from_list(contact_list.list_id, contact.crm_contact_id)

    page = await service.get_list_contacts(contact_list.list_id)
    assert page.total == 0


@pytest.mark.asyncio
async def test_remove_contact_not_a_member_is_a_noop(service):
    contact_list = await service.create_contact_list(name="Test List")
    await service.remove_contact_from_list(contact_list.list_id, "never-added")  # must not raise


@pytest.mark.asyncio
async def test_bulk_remove_from_list_reports_count(service):
    c1 = await service.create_contact({"first_name": "Ada"})
    c2 = await service.create_contact({"first_name": "Grace"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id, c2.crm_contact_id])

    result = await service.bulk_remove_from_list(contact_list.list_id, [c1.crm_contact_id, "never-a-member"])
    assert result.removed == 1

    page = await service.get_list_contacts(contact_list.list_id)
    assert [c.crm_contact_id for c in page.items] == [c2.crm_contact_id]


@pytest.mark.asyncio
async def test_delete_contact_list_removes_list_and_memberships(service):
    contact = await service.create_contact({"first_name": "Ada"})
    contact_list = await service.create_contact_list(name="Test List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    await service.delete_contact_list(contact_list.list_id)

    with pytest.raises(CrmContactListNotFound):
        await service.get_contact_list(contact_list.list_id)
    # The contact itself must still exist, untouched.
    still_there = await service.get_contact(contact.crm_contact_id)
    assert still_there.crm_contact_id == contact.crm_contact_id
    assert still_there.archived is False


@pytest.mark.asyncio
async def test_delete_contact_list_only_affects_its_own_memberships(service):
    contact = await service.create_contact({"first_name": "Ada"})
    list_a = await service.create_contact_list(name="A")
    list_b = await service.create_contact_list(name="B")
    await service.bulk_add_to_list(list_a.list_id, [contact.crm_contact_id])
    await service.bulk_add_to_list(list_b.list_id, [contact.crm_contact_id])

    await service.delete_contact_list(list_a.list_id)

    page_b = await service.get_list_contacts(list_b.list_id)
    assert [c.crm_contact_id for c in page_b.items] == [contact.crm_contact_id]


@pytest.mark.asyncio
async def test_delete_missing_list_raises(service):
    with pytest.raises(CrmContactListNotFound):
        await service.delete_contact_list("does-not-exist")


@pytest.mark.asyncio
async def test_get_list_contacts_paginates(service):
    contact_list = await service.create_contact_list(name="Test List")
    ids = []
    for i in range(5):
        contact = await service.create_contact({"first_name": f"C{i}"})
        ids.append(contact.crm_contact_id)
    await service.bulk_add_to_list(contact_list.list_id, ids)

    page1 = await service.get_list_contacts(contact_list.list_id, page=1, page_size=2)
    assert page1.total == 5
    assert len(page1.items) == 2

    page3 = await service.get_list_contacts(contact_list.list_id, page=3, page_size=2)
    assert len(page3.items) == 1


@pytest.mark.asyncio
async def test_get_contacts_for_missing_list_raises(service):
    with pytest.raises(CrmContactListNotFound):
        await service.get_list_contacts("does-not-exist")


# --- Activity Log: contacts ---


@pytest.mark.asyncio
async def test_manual_create_contact_emits_contact_created_event(service):
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace"})
    events = await service.activity_log.store.list()
    assert len(events) == 1
    assert events[0].event_type == "contact.created"
    assert events[0].entity_id == contact.crm_contact_id
    assert events[0].entity_name == "Ada Lovelace"


@pytest.mark.asyncio
async def test_create_contact_from_import_does_not_emit_contact_created(service):
    """create_contact_from_import() is CSV import's/ITF's own create path --
    it must never ALSO emit a manual contact.created event, since those
    callers already produce their own distinct event (import.completed /
    itf.contact_created) for the exact same write."""
    await service.create_contact_from_import({"first_name": "Imported", "last_name": "Person"})
    events = await service.activity_log.store.list()
    assert events == []


@pytest.mark.asyncio
async def test_update_contact_emits_contact_updated_event(service):
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace"})
    await service.update_contact(contact.crm_contact_id, {"city": "London"})
    events = await service.activity_log.store.list()
    event_types = [e.event_type for e in events]
    assert event_types == ["contact.updated", "contact.created"]  # newest first


@pytest.mark.asyncio
async def test_archive_contact_emits_contact_archived_not_generic_updated(service):
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace"})
    await service.archive_contact(contact.crm_contact_id)
    events = await service.activity_log.store.list()
    assert events[0].event_type == "contact.archived"


@pytest.mark.asyncio
async def test_unarchiving_emits_contact_unarchived(service):
    """Unarchiving goes through the same generic update_contact() path (PATCH
    {"archived": false}) as any other edit -- the before/after diff inside
    _record_contact_update_activity is what distinguishes it from a plain
    contact.updated, per the approved 'option B' design (no separate
    unarchive_contact() method was added)."""
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace"})
    await service.archive_contact(contact.crm_contact_id)
    await service.update_contact(contact.crm_contact_id, {"archived": False})
    events = await service.activity_log.store.list()
    assert events[0].event_type == "contact.unarchived"


@pytest.mark.asyncio
async def test_logging_never_mutates_the_contact_or_its_updated_at(service):
    """The activity log is a separate store entirely -- recording an event
    must never touch CrmContact itself beyond whatever the real action
    already legitimately changed."""
    contact = await service.create_contact({"first_name": "Ada", "last_name": "Lovelace"})
    before = await service.get_contact(contact.crm_contact_id)
    # A read-only action (listing events) must be a complete no-op against the contact.
    await service.activity_log.list_events()
    after = await service.get_contact(contact.crm_contact_id)
    assert before == after
    assert before.updated_at == after.updated_at


# --- Activity Log: lists ---


@pytest.mark.asyncio
async def test_create_contact_list_emits_list_created_event(service):
    contact_list = await service.create_contact_list(name="Austin Family Offices")
    events = await service.activity_log.store.list()
    assert len(events) == 1
    assert events[0].event_type == "list.created"
    assert events[0].entity_id == contact_list.list_id
    assert events[0].entity_name == "Austin Family Offices"


@pytest.mark.asyncio
async def test_rename_list_emits_list_updated_event(service):
    contact_list = await service.create_contact_list(name="Old Name")
    await service.update_contact_list(contact_list.list_id, {"name": "New Name"})
    events = await service.activity_log.store.list()
    assert events[0].event_type == "list.updated"
    assert "New Name" in events[0].summary
    assert events[0].metadata["renamed"] is True


@pytest.mark.asyncio
async def test_delete_list_emits_list_deleted_event(service):
    contact_list = await service.create_contact_list(name="Temporary List")
    await service.delete_contact_list(contact_list.list_id)
    events = await service.activity_log.store.list()
    assert events[0].event_type == "list.deleted"
    assert events[0].entity_name == "Temporary List"


@pytest.mark.asyncio
async def test_deleting_a_list_does_not_delete_its_historical_activity_events(service):
    """The Activity Log is append-only and independent of CrmContactListStore --
    deleting the list must never delete list.created/list.updated events
    recorded before the deletion."""
    contact_list = await service.create_contact_list(name="Temp List")
    await service.update_contact_list(contact_list.list_id, {"description": "note"})
    await service.delete_contact_list(contact_list.list_id)

    events = await service.activity_log.store.list()
    event_types = {e.event_type for e in events}
    assert event_types == {"list.created", "list.updated", "list.deleted"}
    assert len(events) == 3


@pytest.mark.asyncio
async def test_bulk_add_creates_exactly_one_event_regardless_of_contact_count(service):
    contact_list = await service.create_contact_list(name="Big List")
    ids = [(await service.create_contact({"first_name": f"C{i}"})).crm_contact_id for i in range(50)]

    result = await service.bulk_add_to_list(contact_list.list_id, ids)
    assert result.added == 50

    events = await service.activity_log.store.list()
    add_events = [e for e in events if e.event_type == "list.contacts_added"]
    assert len(add_events) == 1
    assert add_events[0].metadata["added"] == 50
    assert "50" in add_events[0].summary


@pytest.mark.asyncio
async def test_bulk_add_all_already_members_creates_no_misleading_event(service):
    """If every selected contact is already a member, nothing actually
    changed -- logging 'N contacts added' would misrepresent what happened,
    so no list.contacts_added event fires at all."""
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    events_before = await service.activity_log.store.list()
    add_events_before = [e for e in events_before if e.event_type == "list.contacts_added"]
    assert len(add_events_before) == 1  # the real first add

    result = await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    assert result.added == 0
    assert result.already_member == 1

    events_after = await service.activity_log.store.list()
    add_events_after = [e for e in events_after if e.event_type == "list.contacts_added"]
    assert len(add_events_after) == 1  # unchanged -- the duplicate add produced no new event


@pytest.mark.asyncio
async def test_bulk_remove_creates_exactly_one_event_regardless_of_contact_count(service):
    contact_list = await service.create_contact_list(name="List")
    ids = [(await service.create_contact({"first_name": f"C{i}"})).crm_contact_id for i in range(20)]
    await service.bulk_add_to_list(contact_list.list_id, ids)

    result = await service.bulk_remove_from_list(contact_list.list_id, ids)
    assert result.removed == 20

    events = await service.activity_log.store.list()
    remove_events = [e for e in events if e.event_type == "list.contacts_removed"]
    assert len(remove_events) == 1
    assert remove_events[0].metadata["removed"] == 20


@pytest.mark.asyncio
async def test_bulk_remove_of_nothing_creates_no_event(service):
    contact_list = await service.create_contact_list(name="List")
    result = await service.bulk_remove_from_list(contact_list.list_id, ["not-a-member"])
    assert result.removed == 0

    events = await service.activity_log.store.list()
    remove_events = [e for e in events if e.event_type == "list.contacts_removed"]
    assert remove_events == []


@pytest.mark.asyncio
async def test_logging_does_not_change_list_membership_state(service):
    """Recording events must never itself alter list membership or contact
    records -- verified by fetching state before/after a pure read of the log."""
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    before = await service.get_contact_list(contact_list.list_id)
    await service.activity_log.list_events()
    after = await service.get_contact_list(contact_list.list_id)
    assert before.contact_count == after.contact_count == 1


# --- actor attribution (Phase 2, admin/service OPERATOR token) -----------
#
# `actor` is purely additive -- every list-mutation call site above this
# section omits it and keeps getting actor=None, matching every existing
# assertion in this file (none of them checks `.actor`, and none needs to
# change now that the parameter exists).


@pytest.mark.asyncio
async def test_create_contact_list_actor_defaults_to_none(service):
    await service.create_contact_list(name="List")

    events = await service.activity_log.store.list()
    created = next(e for e in events if e.event_type == "list.created")
    assert created.actor is None


@pytest.mark.asyncio
async def test_create_contact_list_records_the_given_actor(service):
    await service.create_contact_list(name="List", actor="claude_operator")

    events = await service.activity_log.store.list()
    created = next(e for e in events if e.event_type == "list.created")
    assert created.actor == "claude_operator"


@pytest.mark.asyncio
async def test_update_contact_list_records_the_given_actor(service):
    contact_list = await service.create_contact_list(name="List")
    await service.update_contact_list(contact_list.list_id, {"name": "Renamed"}, actor="claude_operator")

    events = await service.activity_log.store.list()
    updated = next(e for e in events if e.event_type == "list.updated")
    assert updated.actor == "claude_operator"


@pytest.mark.asyncio
async def test_update_contact_list_actor_defaults_to_none(service):
    contact_list = await service.create_contact_list(name="List")
    await service.update_contact_list(contact_list.list_id, {"name": "Renamed"})

    events = await service.activity_log.store.list()
    updated = next(e for e in events if e.event_type == "list.updated")
    assert updated.actor is None


@pytest.mark.asyncio
async def test_bulk_add_to_list_records_the_given_actor(service):
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id], actor="claude_operator")

    events = await service.activity_log.store.list()
    added = next(e for e in events if e.event_type == "list.contacts_added")
    assert added.actor == "claude_operator"


@pytest.mark.asyncio
async def test_bulk_add_to_list_actor_defaults_to_none(service):
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    events = await service.activity_log.store.list()
    added = next(e for e in events if e.event_type == "list.contacts_added")
    assert added.actor is None


@pytest.mark.asyncio
async def test_bulk_remove_from_list_records_the_given_actor(service):
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    await service.bulk_remove_from_list(contact_list.list_id, [contact.crm_contact_id], actor="claude_operator")

    events = await service.activity_log.store.list()
    removed = next(e for e in events if e.event_type == "list.contacts_removed")
    assert removed.actor == "claude_operator"


@pytest.mark.asyncio
async def test_bulk_remove_from_list_actor_defaults_to_none(service):
    contact_list = await service.create_contact_list(name="List")
    contact = await service.create_contact({"first_name": "Solo"})
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    await service.bulk_remove_from_list(contact_list.list_id, [contact.crm_contact_id])

    events = await service.activity_log.store.list()
    removed = next(e for e in events if e.event_type == "list.contacts_removed")
    assert removed.actor is None
