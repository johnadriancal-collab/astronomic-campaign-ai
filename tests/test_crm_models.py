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
    derive_investor_mode,
    get_contact_export_fields,
)


def test_investor_mode_options_match_the_real_form_exactly():
    assert INVESTOR_MODE_OPTIONS == ["Privately", "Institutionally", "Both"]


# --- derive_investor_mode() ---


def test_derive_investor_mode_private_only():
    assert derive_investor_mode(["Angel Investor"]) == "Privately"


def test_derive_investor_mode_institutional_only():
    assert derive_investor_mode(["Venture Capital"]) == "Institutionally"


def test_derive_investor_mode_both():
    assert derive_investor_mode(["Angel Investor", "Family Office"]) == "Both"


def test_derive_investor_mode_neither():
    assert derive_investor_mode(["Lawyer"]) is None


def test_derive_investor_mode_empty_or_none_is_neither():
    assert derive_investor_mode([]) is None
    assert derive_investor_mode(None) is None


def test_derive_investor_mode_multiple_private_types_still_privately():
    assert derive_investor_mode(
        ["Angel Investor", "Private Investor", "Invest with group of Angels"]
    ) == "Privately"


def test_derive_investor_mode_multiple_institutional_types_still_institutionally():
    assert derive_investor_mode(["Family Office", "Fund LP", "Venture Capital"]) == "Institutionally"


def test_derive_investor_mode_mixed_multiple_types_is_both():
    assert derive_investor_mode(
        ["Angel Investor", "I sponsor deals that I find", "Family Office", "Institutional Investor"]
    ) == "Both"


def test_crm_contact_defaults_to_not_manually_overridden():
    contact = CrmContact(crm_contact_id="1", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    assert contact.thesis_investor_mode_manual_override is False


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


# --- get_contact_export_fields() (dynamic CSV export schema) ---


def test_export_fields_excludes_source_snapshot_and_custom_fields():
    keys = {f.key for f in get_contact_export_fields()}
    assert "source_snapshot" not in keys
    assert "custom_fields" not in keys


def test_export_fields_covers_every_other_model_field():
    """The whole point of computing this via introspection: every declared
    CrmContact field (minus the two deliberate exclusions) must show up,
    with no manual list to fall out of sync."""
    expected = set(CrmContact.model_fields.keys()) - {"source_snapshot", "custom_fields"}
    assert {f.key for f in get_contact_export_fields()} == expected


def test_export_fields_classifies_plain_list_fields_as_list():
    """Regression check: list[str] fields (no `| None` wrapper) must not be
    mistaken for an Optional-wrapped scalar and collapsed to their item type."""
    by_key = {f.key: f.kind for f in get_contact_export_fields()}
    assert by_key["technologies"] == "list"
    assert by_key["thesis_private_check_sizes"] == "list"
    assert by_key["thesis_private_asset_types"] == "list"


def test_export_fields_classifies_optional_list_fields_as_list():
    by_key = {f.key: f.kind for f in get_contact_export_fields()}
    # thesis_institutional_* fields are also plain list[str] with a default_factory,
    # exercised separately from the private-section fields above.
    assert by_key["thesis_institutional_industries"] == "list"


def test_export_fields_classifies_booleans_regardless_of_optionality():
    by_key = {f.key: f.kind for f in get_contact_export_fields()}
    assert by_key["archived"] == "boolean"  # plain bool
    assert by_key["thesis_also_invests_institutionally"] == "boolean"  # bool | None


def test_export_fields_classifies_plain_and_optional_scalars():
    by_key = {f.key: f.kind for f in get_contact_export_fields()}
    assert by_key["crm_contact_id"] == "scalar"  # plain str
    assert by_key["email"] == "scalar"  # str | None
    assert by_key["created_at"] == "scalar"  # datetime


def test_export_fields_preserves_model_declaration_order():
    field_keys = [name for name in CrmContact.model_fields if name not in {"source_snapshot", "custom_fields"}]
    assert [f.key for f in get_contact_export_fields()] == field_keys


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
