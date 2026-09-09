"""
Fidelity checks for the Investor Thesis fields against Astronomic's real
form content, and for the custom-field type contract.
"""

from datetime import datetime, timezone

from app.models.crm import (
    CHECK_SIZE_OPTIONS,
    DEAL_STAGE_OPTIONS,
    DEMOGRAPHIC_PREFERENCE_OPTIONS,
    EMAIL_STATUS_OPTIONS,
    INDUSTRY_OPTIONS,
    INVESTOR_MODE_OPTIONS,
    CrmContact,
    CrmCustomFieldDefinition,
    CustomFieldType,
    derive_investor_mode,
    get_contact_export_fields,
)


def test_investor_mode_options_match_the_real_form_exactly():
    assert INVESTOR_MODE_OPTIONS == ["Privately", "Institutionally", "Both"]


# --- EMAIL_STATUS_OPTIONS ---------------------------------------------------
#
# Derived from a full production tally (2026-09-02, 2,727 contacts), not
# invented. "User Managed" is included deliberately (trusted/sendable,
# not a deliverability status). "Valid"/"valid" are deliberately excluded
# -- legacy values, not migrated as part of introducing this dropdown.
# email_status itself stays a plain, unvalidated `str | None` field (see
# CrmContact below) -- this list is UI-only, matching INVESTOR_MODE_OPTIONS's
# own precedent of zero server-side enum enforcement.


def test_email_status_options_match_the_product_decision_exactly():
    assert EMAIL_STATUS_OPTIONS == [
        "User Managed",
        "Verified",
        "Unverified",
        "Invalid",
        "Unavailable",
        "Email No Longer Verified",
        "New Data Available",
        "Extrapolated",
    ]


def test_email_status_options_deliberately_excludes_legacy_valid_values():
    assert "Valid" not in EMAIL_STATUS_OPTIONS
    assert "valid" not in EMAIL_STATUS_OPTIONS


def test_email_status_field_has_no_server_side_enum_enforcement():
    """Deliberate absence, not an oversight -- confirms the smallest-safe-
    change scope: this is a UI-only dropdown. CrmContact must still accept
    an arbitrary string (a legacy value, or anything else) for email_status
    without validation error, same as before this change."""
    contact = CrmContact(crm_contact_id="c1", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), email_status="valid")
    assert contact.email_status == "valid"
    contact_arbitrary = CrmContact(
        crm_contact_id="c2",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        email_status="Anything At All, Not Even Close To A Real Option",
    )
    assert contact_arbitrary.email_status == "Anything At All, Not Even Close To A Real Option"


# --- INDUSTRY_OPTIONS taxonomy -------------------------------------------
#
# investment_industry (the CRM custom field) is a genuinely open field --
# its live CrmCustomFieldDefinition.options is deliberately empty, per its
# own description ("New values are accepted automatically -- no fixed
# option list"). INDUSTRY_OPTIONS is instead the CANONICAL vocabulary
# enforced by CSV import (crm_classification_rules.py), email-intake
# extraction/aliases, and Astro AI's natural-language parser -- all three
# import this constant directly, so a change here propagates automatically.
# frontend/lib/crm-thesis-options.ts maintains its OWN, independently
# hardcoded copy (no shared source at build time) for the CRM's investor-
# thesis editing form -- these two regression tests exist specifically so
# an addition to one is never silently forgotten on the other.


def test_industry_options_includes_crypto_and_professional_services():
    """Added for Luma's new 'primary investment or industry areas of
    focus' registration question -- both are genuinely new categories,
    confirmed to have no existing near-duplicate in this list."""
    assert "Crypto / Web3" in INDUSTRY_OPTIONS
    assert "Professional / Business Services" in INDUSTRY_OPTIONS


def test_industry_options_has_no_duplicates_case_insensitive():
    lowered = [o.lower() for o in INDUSTRY_OPTIONS]
    assert len(lowered) == len(set(lowered)), "INDUSTRY_OPTIONS must never contain a duplicate or near-duplicate entry"


def test_industry_options_count_matches_frontend_copy():
    """frontend/lib/crm-thesis-options.ts hardcodes its own copy of this
    exact list (no shared source at build time -- see that file's own
    comment). This is a coarse parity guard: it can't read the TypeScript
    file directly, but pins the expected count here so a future addition
    to one list that isn't mirrored to the other is at least flagged by a
    mismatched count, prompting a check of both files."""
    assert len(INDUSTRY_OPTIONS) == 31


def test_industry_options_includes_other():
    """"Other" was added 2026-09-08 after a Luma <-> CRM schema-alignment
    audit found it in 18 legitimate historical Luma answers -- see
    normalize_industry_focus_labels's docstring."""
    assert "Other" in INDUSTRY_OPTIONS


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


# --- derive_investor_mode(): Corporate Venture / Fund Manager / Other -------
# (added to investor_type's options 2026-09-08, classified 2026-09-08 --
# see PRIVATE_INVESTOR_TYPES/INSTITUTIONAL_INVESTOR_TYPES's own comment)


def test_derive_investor_mode_corporate_venture_is_institutional():
    assert derive_investor_mode(["Corporate Venture"]) == "Institutionally"


def test_derive_investor_mode_fund_manager_general_partner_is_institutional():
    assert derive_investor_mode(["Fund Manager / General Partner"]) == "Institutionally"


def test_derive_investor_mode_other_alone_gives_no_signal():
    """"Other" is deliberately classified into neither set -- there's no
    way to know what archetype a bare "Other" answer means, so it must
    never guess."""
    assert derive_investor_mode(["Other"]) is None


def test_derive_investor_mode_other_combined_with_private_type_stays_privately():
    """"Other" contributes no signal of its own -- combined with a real
    private-type answer, the result is still just "Privately", never
    upgraded to "Both" by Other's mere presence."""
    assert derive_investor_mode(["Angel Investor", "Other"]) == "Privately"


def test_derive_investor_mode_other_combined_with_institutional_type_stays_institutionally():
    assert derive_investor_mode(["Venture Capital", "Other"]) == "Institutionally"


def test_derive_investor_mode_corporate_venture_plus_private_type_is_both():
    assert derive_investor_mode(["Corporate Venture", "Angel Investor"]) == "Both"


def test_derive_investor_mode_fund_manager_plus_private_type_is_both():
    assert derive_investor_mode(["Fund Manager / General Partner", "Private Investor"]) == "Both"


def test_derive_investor_mode_corporate_venture_and_fund_manager_together_stay_institutionally():
    """Two institutional-only archetypes together are still just
    "Institutionally", not "Both" -- no private signal present."""
    assert derive_investor_mode(["Corporate Venture", "Fund Manager / General Partner"]) == "Institutionally"


def test_derive_investor_mode_corporate_venture_combines_with_existing_institutional_types():
    """A new institutional archetype alongside a pre-existing one is still
    just "Institutionally" -- confirms the new values integrate with the
    existing classification, not just in isolation."""
    assert derive_investor_mode(["Corporate Venture", "Family Office", "Fund LP"]) == "Institutionally"


def test_derive_investor_mode_all_three_new_values_with_no_other_types_only_other_gives_no_signal():
    """Corporate Venture and Fund Manager both carry institutional signal;
    Other carries none -- the combination is still just "Institutionally"."""
    assert derive_investor_mode(["Corporate Venture", "Fund Manager / General Partner", "Other"]) == "Institutionally"


# --- regression: existing classifications are completely unaffected --------


def test_derive_investor_mode_existing_private_types_unchanged():
    for investor_type in [
        "Angel Investor", "I sponsor deals that I find", "Invest with group of Angels",
        "Participate in syndicated investments", "Private Investor",
    ]:
        assert derive_investor_mode([investor_type]) == "Privately", investor_type


def test_derive_investor_mode_existing_institutional_types_unchanged():
    for investor_type in ["Family Office", "Fund LP", "Institutional Investor", "Private Equity", "Venture Capital"]:
        assert derive_investor_mode([investor_type]) == "Institutionally", investor_type


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
    contact = CrmContact(crm_contact_id="c1", created_at=now, updated_at=now, thesis_dietary_preferences=["Vegan"])
    assert contact.thesis_dietary_preferences == ["Vegan"]


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


# --- profile_photo_url URL-joining (2026-09-09 deployment-readiness audit) --
#
# Confirms CrmContact.profile_photo_url never produces a double slash (or a
# missing one) regardless of whether PROFILE_PHOTO_CDN_BASE_URL is
# configured with or without a trailing slash -- both must resolve to the
# EXACT SAME URL. Uses Astronomic's actual intended production domain
# (photos.astronomicconnect.com) rather than a placeholder, per the audit
# that added these.


def _make_contact_with_photo_key(key: str = "avatars/11111111-1111-1111-1111-111111111111.jpg") -> CrmContact:
    now = datetime.now(timezone.utc)
    return CrmContact(crm_contact_id="c1", created_at=now, updated_at=now, profile_photo_key=key)


def test_profile_photo_url_with_no_trailing_slash_on_the_base_url(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com")
    contact = _make_contact_with_photo_key()
    assert contact.profile_photo_url == (
        "https://photos.astronomicconnect.com/avatars/11111111-1111-1111-1111-111111111111.jpg"
    )


def test_profile_photo_url_with_a_trailing_slash_on_the_base_url_resolves_identically(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com/")
    contact = _make_contact_with_photo_key()
    assert contact.profile_photo_url == (
        "https://photos.astronomicconnect.com/avatars/11111111-1111-1111-1111-111111111111.jpg"
    )
    assert "//avatars" not in contact.profile_photo_url  # no double slash


def test_profile_photo_url_with_multiple_trailing_slashes_still_resolves_correctly(monkeypatch):
    """Defensive: even a base URL with more than one accidental trailing
    slash (e.g. a copy-paste mistake in the Railway variable) still
    produces a single, correct join -- rstrip("/") removes all of them."""
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com///")
    contact = _make_contact_with_photo_key()
    assert contact.profile_photo_url == (
        "https://photos.astronomicconnect.com/avatars/11111111-1111-1111-1111-111111111111.jpg"
    )


def test_profile_photo_url_is_none_when_no_cdn_base_url_is_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", None)
    contact = _make_contact_with_photo_key()
    assert contact.profile_photo_url is None


def test_profile_photo_url_is_none_when_contact_has_no_photo_key_regardless_of_cdn_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com")
    now = datetime.now(timezone.utc)
    contact = CrmContact(crm_contact_id="c1", created_at=now, updated_at=now)
    assert contact.profile_photo_url is None
