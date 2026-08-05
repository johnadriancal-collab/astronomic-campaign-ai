"""
Fidelity checks for the Investor Thesis fields against Astronomic's real
form content, and for the custom-field type contract.
"""

from datetime import datetime, timezone

from app.models.crm import (
    CHECK_SIZE_OPTIONS,
    DEAL_STAGE_OPTIONS,
    DEMOGRAPHIC_PREFERENCE_OPTIONS,
    INVESTOR_MODE_OPTIONS,
    CrmContact,
    CrmCustomFieldDefinition,
    CustomFieldType,
)


def test_investor_mode_options_match_the_real_form_exactly():
    assert INVESTOR_MODE_OPTIONS == ["Privately", "Institutionally", "Both"]


def test_check_size_options_cover_full_range_from_the_real_form():
    assert CHECK_SIZE_OPTIONS[0] == "$1k - $10k"
    assert CHECK_SIZE_OPTIONS[-1] == "$10M+"
    assert len(CHECK_SIZE_OPTIONS) == 11


def test_deal_stage_options_include_friends_and_family_through_pre_ipo():
    assert DEAL_STAGE_OPTIONS[0].startswith("Friends & Family")
    assert DEAL_STAGE_OPTIONS[-1].startswith("Pre-IPO")


def test_demographic_preference_options_preserve_exact_wording():
    assert "I'm open to investing in anyone" in DEMOGRAPHIC_PREFERENCE_OPTIONS
    assert "I prefer LGBTQ+ fundraisers" in DEMOGRAPHIC_PREFERENCE_OPTIONS


def test_private_and_institutional_sections_are_structurally_identical():
    """Section 2 and Section 3 of the real form ask the same 7 questions -- the model
    must expose the same field for each, just with a different prefix."""
    now = datetime.now(timezone.utc)
    contact = CrmContact(crm_contact_id="c1", created_at=now, updated_at=now)
    for suffix in ["asset_types", "business_models", "industries", "check_sizes", "deal_stages",
                   "meeting_preferences", "demographic_preferences"]:
        assert hasattr(contact, f"thesis_private_{suffix}")
        assert hasattr(contact, f"thesis_institutional_{suffix}")


def test_q1_through_q4_are_not_duplicated_as_thesis_fields():
    """First Name/Last Name/Email/LinkedIn (the form's Q1-4) reuse the core identity
    fields -- there must be no separate thesis_first_name etc."""
    now = datetime.now(timezone.utc)
    contact = CrmContact(crm_contact_id="c1", created_at=now, updated_at=now)
    for bad_field in ["thesis_first_name", "thesis_last_name", "thesis_email", "thesis_linkedin_url"]:
        assert not hasattr(contact, bad_field)


def test_dietary_preference_is_a_thesis_field_not_duplicated_as_a_seed_custom_field():
    """Q24 of the real form -- lives on the model, not pre-seeded as a custom field."""
    now = datetime.now(timezone.utc)
    contact = CrmContact(crm_contact_id="c1", created_at=now, updated_at=now, thesis_dietary_preferences="Vegan")
    assert contact.thesis_dietary_preferences == "Vegan"


def test_no_custom_fields_are_pre_seeded():
    """Every custom field is one the user creates themselves -- nothing invented on their behalf."""
    from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore

    store = MemoryCrmCustomFieldStore()
    import asyncio

    assert asyncio.run(store.list()) == []


def test_custom_field_definition_supports_all_seven_required_types():
    now = datetime.now(timezone.utc)
    for field_type in CustomFieldType:
        definition = CrmCustomFieldDefinition(
            crm_custom_field_id="f1", field_key="k", label="L", field_type=field_type,
            created_at=now, updated_at=now,
        )
        assert definition.field_type == field_type
    assert {t.value for t in CustomFieldType} == {
        "text", "long_text", "number", "date", "boolean", "single_select", "multi_select",
    }
