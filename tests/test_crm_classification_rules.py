"""
Tests for the centralized CSV-import classification layer
(app/services/crm_classification_rules.py). classify_industry was the
first rule; classify_investor_mode and classify_role are the second and
third, added to the same registry without any pipeline restructuring --
these tests double as a template for future rules (Check Size
standardization, etc.).
"""

import pytest

from app.services.crm_classification_rules import (
    apply_classification_rules,
    build_classification_context,
    classify_accredited_status,
    classify_age_range,
    classify_check_size,
    classify_chris_degree_connection,
    classify_dinner_subscriptions,
    classify_dinners_attended,
    classify_engagement_stage,
    classify_gender,
    classify_how_early_do_you_invest,
    classify_industry,
    classify_investor_mode,
    classify_legacy_thesis_columns,
    classify_role,
)

NO_CONTEXT: dict = {}


# --- classify_industry (unchanged behavior, now takes a context arg it ignores) ---


def test_industry_comes_from_industry_column_only():
    row = {"Industry": "Information Technology & Services", "Main Industry": "Healthcare", "Sub-industry": "Biotech"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["industry"] == "Information Technology & Services"


def test_investment_industry_merges_main_then_sub_preserving_order():
    row = {"Main Industry": "Healthcare, Fintech", "Sub-industry": "Biotech, Digital Health"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["custom:investment_industry"] == ["Healthcare", "Fintech", "Biotech", "Digital Health"]


def test_investment_industry_dedups_exact_duplicate_values():
    row = {"Main Industry": "Healthcare, Gaming", "Sub-industry": "Gaming, Biotech"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["custom:investment_industry"] == ["Healthcare", "Gaming", "Biotech"]


def test_new_unseen_industry_values_pass_through_unchanged():
    row = {"Industry": "Artisanal Cheese Production", "Main Industry": "Space Tourism", "Sub-industry": "Zero-G Hospitality"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["industry"] == "Artisanal Cheese Production"
    assert result["custom:investment_industry"] == ["Space Tourism", "Zero-G Hospitality"]


def test_missing_columns_produce_no_keys():
    row = {"First Name": "Ada", "Last Name": "Lovelace"}
    result = classify_industry(row, NO_CONTEXT)
    assert result == {}


def test_only_main_industry_present():
    row = {"Main Industry": "Healthcare"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["custom:investment_industry"] == ["Healthcare"]
    assert "industry" not in result


def test_only_sub_industry_present():
    row = {"Sub-industry": "Biotech, Digital Health"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["custom:investment_industry"] == ["Biotech", "Digital Health"]


def test_header_variants_are_matched_case_and_spacing_insensitive():
    row = {"industry": "Media Production", "main industry": "Sports", "sub industry": "Athlete Wellness"}
    result = classify_industry(row, NO_CONTEXT)
    assert result["industry"] == "Media Production"
    assert result["custom:investment_industry"] == ["Sports", "Athlete Wellness"]


def test_blank_values_are_treated_as_missing():
    row = {"Industry": "  ", "Main Industry": "", "Sub-industry": "Biotech"}
    result = classify_industry(row, NO_CONTEXT)
    assert "industry" not in result
    assert result["custom:investment_industry"] == ["Biotech"]


# --- classify_investor_mode ---


def test_investor_type_stored_as_is_comma_split_and_trimmed():
    row = {"Investor type": "Fund LP,  Institutional Investor "}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["custom:investor_type"] == ["Fund LP", "Institutional Investor"]


def test_private_investor_type_derives_privately():
    row = {"Investor type": "Angel Investor"}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["thesis_investor_mode"] == "Privately"


def test_institutional_investor_type_derives_institutionally():
    row = {"Investor type": "Venture Capital"}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["thesis_investor_mode"] == "Institutionally"


def test_mixed_private_and_institutional_derives_both():
    row = {"Investor type": "Angel Investor, Venture Capital"}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["thesis_investor_mode"] == "Both"


def test_no_private_or_institutional_signal_leaves_mode_unset_never_guesses():
    row = {"Investor type": "Hedge Fund"}  # a real value with no signal in either bucket
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["custom:investor_type"] == ["Hedge Fund"]
    assert "thesis_investor_mode" not in result


def test_missing_investor_type_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result == {}


def test_investor_type_never_filtered_against_any_list():
    """The raw value must never be changed by this automation -- only the
    derived mode field is computed from it."""
    row = {"Investor type": "Some Brand New Investor Category Nobody Approved"}
    result = classify_investor_mode(row, NO_CONTEXT)
    assert result["custom:investor_type"] == ["Some Brand New Investor Category Nobody Approved"]


# --- classify_role ---


ROLE_CONTEXT = {"role_options": {"Investor", "Founder", "CEO"}}


def test_role_keeps_only_approved_tags():
    row = {"Role": "Investor, Founder, VP"}
    result = classify_role(row, ROLE_CONTEXT)
    assert result["custom:role"] == ["Investor", "Founder"]


def test_role_drops_unapproved_tags_without_inventing_a_replacement():
    row = {"Role": "President, Director"}
    result = classify_role(row, ROLE_CONTEXT)
    assert result == {}  # nothing approved survives -- no key at all, never a guess


def test_role_preserves_multiple_legitimate_roles():
    row = {"Role": "CEO, Founder, Investor"}
    result = classify_role(row, ROLE_CONTEXT)
    assert result["custom:role"] == ["CEO", "Founder", "Investor"]


def test_role_with_no_approved_options_in_context_drops_everything():
    row = {"Role": "Investor, Founder"}
    result = classify_role(row, {"role_options": set()})
    assert result == {}


def test_role_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_role(row, ROLE_CONTEXT)
    assert result == {}


def test_role_never_falls_back_to_options_missing_from_context():
    row = {"Role": "Investor"}
    result = classify_role(row, NO_CONTEXT)  # no "role_options" key at all
    assert result == {}


# --- classify_dinner_subscriptions ---


def test_dinner_subscriptions_final_values_pass_through_unchanged():
    row = {"Dinner Subscriptions": "Investor Dinners, Fireside Dinners"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners"]


def test_dinner_subscriptions_legacy_value_collapses_and_dedupes():
    # User's own example: legacy Sigma Librae Dinners maps to Founder Dinners,
    # and the delete-only Retreats disappears -- nothing else survives it.
    row = {"Dinner Subscriptions": "Investor Dinners, Sigma Librae Dinners, Retreats"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners"]


def test_dinner_subscriptions_legacy_mansion_dinners_maps_to_investor_dinners():
    # User's other own example.
    row = {"Dinner Subscriptions": "Mansion dinners with matching founders/investors, Fireside Dinners"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners"]


def test_dinner_subscriptions_delete_only_produces_no_key():
    row = {"Dinner Subscriptions": "Retreats, Parent dinners, Astronomic General Subscriber"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result == {}


def test_dinner_subscriptions_unrecognized_value_is_preserved_not_discarded():
    row = {"Dinner Subscriptions": "Investor Dinners, Some Brand New Dinner Series"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Some Brand New Dinner Series"]


def test_dinner_subscriptions_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result == {}


def test_dinner_subscriptions_header_variant_singular_matched():
    row = {"Dinner Subscription": "Investor Dinners"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners"]


def test_dinner_subscriptions_small_group_dinners_maps_to_investor_dinners():
    # User's explicit decision for the one unrecognized value found in the
    # 2026-08-06 two-CSV audit (Nicole Bentz's row) -- map, don't add a new option.
    row = {"Dinner Subscriptions": "Small group dinners, Fireside Dinners"}
    result = classify_dinner_subscriptions(row, NO_CONTEXT)
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners"]


# --- classify_dinners_attended ---


def test_dinners_attended_dated_entries_preserved_verbatim():
    # Real values from Alex Pepe's row in Contacts 2 (ITF).csv -- must survive
    # exactly, in order, with no normalization or collapsing into categories.
    row = {
        "Dinners Attended": (
            "Investor Dinners, Fireside Dinners, Savvy [2.25.2025] Austin, "
            "VacayMyWay [08.12.2025] Austin, Alpha Rose [08.13.2025] Austin, Biz Dev Dinners, "
            "Ensitech [11.13.2025] Austin, SharpsAI [12.04.2025] Austin, "
            "Civilization Fund [01.19.2026] Austin, Predict RX [03.10.2026] Austin, "
            "Submersive [04.30.2026] Austin"
        )
    }
    result = classify_dinners_attended(row, NO_CONTEXT)
    assert result["custom:dinners_attended"] == [
        "Investor Dinners", "Fireside Dinners", "Savvy [2.25.2025] Austin",
        "VacayMyWay [08.12.2025] Austin", "Alpha Rose [08.13.2025] Austin", "Biz Dev Dinners",
        "Ensitech [11.13.2025] Austin", "SharpsAI [12.04.2025] Austin",
        "Civilization Fund [01.19.2026] Austin", "Predict RX [03.10.2026] Austin",
        "Submersive [04.30.2026] Austin",
    ]


def test_dinners_attended_never_normalizes_dated_names():
    # Unlike Dinner Subscriptions, a dated dinner name is never rewritten,
    # collapsed, or dropped -- there is no legacy/delete map for this field.
    row = {"Dinners Attended": "Retreats, Astronomic General Subscriber [01.01.2025] Austin"}
    result = classify_dinners_attended(row, NO_CONTEXT)
    assert result["custom:dinners_attended"] == ["Retreats", "Astronomic General Subscriber [01.01.2025] Austin"]


def test_dinners_attended_deduplicates_exact_repeats_preserving_order():
    row = {"Dinners Attended": "Investor Dinners, Fireside Dinners, Investor Dinners"}
    result = classify_dinners_attended(row, NO_CONTEXT)
    assert result["custom:dinners_attended"] == ["Investor Dinners", "Fireside Dinners"]


def test_dinners_attended_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_dinners_attended(row, NO_CONTEXT)
    assert result == {}


def test_dinners_attended_header_variant_singular_matched():
    row = {"Dinner Attended": "Investor Dinners"}
    result = classify_dinners_attended(row, NO_CONTEXT)
    assert result["custom:dinners_attended"] == ["Investor Dinners"]


# --- classify_chris_degree_connection ---

CHRIS_DEGREE_CONTEXT = {"chris_degree_connection_options": {"1st degree", "2nd degree", "3rd degree", "N/A"}}


def test_chris_degree_connection_valid_value_passes_through():
    row = {"Chris Degree Connection": "1st degree"}
    result = classify_chris_degree_connection(row, CHRIS_DEGREE_CONTEXT)
    assert result["custom:chris_degree_connection"] == "1st degree"


def test_chris_degree_connection_every_defined_option_passes():
    for option in ["1st degree", "2nd degree", "3rd degree", "N/A"]:
        row = {"Chris Degree Connection": option}
        result = classify_chris_degree_connection(row, CHRIS_DEGREE_CONTEXT)
        assert result["custom:chris_degree_connection"] == option


def test_chris_degree_connection_unrecognized_value_produces_no_key():
    row = {"Chris Degree Connection": "5th degree"}
    result = classify_chris_degree_connection(row, CHRIS_DEGREE_CONTEXT)
    assert result == {}


def test_chris_degree_connection_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_chris_degree_connection(row, CHRIS_DEGREE_CONTEXT)
    assert result == {}


def test_chris_degree_connection_no_context_options_drops_everything():
    row = {"Chris Degree Connection": "1st degree"}
    result = classify_chris_degree_connection(row, NO_CONTEXT)
    assert result == {}


# --- classify_age_range ---

AGE_RANGE_CONTEXT = {
    "age_range_options": {"18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased"}
}


def test_age_range_valid_value_passes_through():
    row = {"Age Range": "61-70"}
    result = classify_age_range(row, AGE_RANGE_CONTEXT)
    assert result["custom:age_range"] == "61-70"


def test_age_range_unrecognized_value_produces_no_key():
    row = {"Age Range": "100-110"}
    result = classify_age_range(row, AGE_RANGE_CONTEXT)
    assert result == {}


def test_age_range_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_age_range(row, AGE_RANGE_CONTEXT)
    assert result == {}


# --- classify_gender ---

GENDER_CONTEXT = {"gender_options": {"Male", "Female"}}


def test_gender_valid_value_passes_through():
    row = {"Gender": "Male"}
    result = classify_gender(row, GENDER_CONTEXT)
    assert result["custom:gender"] == "Male"


def test_gender_unrecognized_value_produces_no_key():
    row = {"Gender": "Not a real option"}
    result = classify_gender(row, GENDER_CONTEXT)
    assert result == {}


def test_gender_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_gender(row, GENDER_CONTEXT)
    assert result == {}


# --- classify_engagement_stage ---
#
# The bug this rule fixes: HEADER_ALIASES used to map BOTH "stage" and
# "funding stage" to the same core `funding_stage` field. Neither real CSV
# in the 2026-08-06 audit even has a "Funding Stage" column -- their "Stage"
# column holds outreach/engagement values (Interested/Cold/Unresponsive/
# Replied/"(No Stage)"), which belongs in engagement_stage, never
# funding_stage. "Replied" is a real, live option (CUSTOM_FIELD_CORRECTIONS
# added it after the audit confirmed 17 genuine occurrences); "(No Stage)"
# deliberately is NOT -- it's a null/unset placeholder, not a real stage.

ENGAGEMENT_STAGE_CONTEXT = {"engagement_stage_options": {"Cold", "Interested", "Unresponsive", "Replied"}}


def test_engagement_stage_valid_value_passes_through():
    row = {"Stage": "Interested"}
    result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
    assert result["custom:engagement_stage"] == "Interested"


def test_engagement_stage_every_defined_option_passes():
    for option in ["Cold", "Interested", "Unresponsive", "Replied"]:
        row = {"Stage": option}
        result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
        assert result["custom:engagement_stage"] == option


def test_engagement_stage_no_stage_placeholder_produces_no_key():
    # "(No Stage)" is a null/unset placeholder (1 real occurrence in the audit), not a
    # real stage -- deliberately never added as an option, so it's dropped here too,
    # never guessed at, never corrupts the single-select dropdown.
    row = {"Stage": "(No Stage)"}
    result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
    assert result == {}


def test_engagement_stage_unrecognized_value_produces_no_key():
    row = {"Stage": "Some Brand New Stage Nobody Approved"}
    result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
    assert result == {}


def test_engagement_stage_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
    assert result == {}


def test_engagement_stage_never_reads_funding_stage_column():
    """The exact collision this rule prevents: a "Funding Stage" column with
    a real funding-round value must NOT be picked up by classify_engagement_stage."""
    row = {"Funding Stage": "Series A"}
    result = classify_engagement_stage(row, ENGAGEMENT_STAGE_CONTEXT)
    assert result == {}


# --- classify_check_size ---
#
# check_size_personal <- CSV `Check Size`, check_size_institutional <- CSV
# `Check Size (Institutional)`. Confirmed via the 2026-08-06 two-CSV audit
# that these are genuinely distinct columns (64/119 rows where both are
# populated in the real "Contacts 2 (ITF).csv" have DIFFERENT values, 20
# rows have an institutional value with no personal value at all) -- so
# they are two independent rules' worth of behavior, never derived from
# each other. Both fields are multi_select with the same 11 canonical
# bucket options in production.

CHECK_SIZE_OPTIONS = {
    "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
    "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+",
}
CHECK_SIZE_CONTEXT = {
    "check_size_personal_options": CHECK_SIZE_OPTIONS,
    "check_size_institutional_options": CHECK_SIZE_OPTIONS,
}


def test_check_size_personal_single_clean_bucket_passes_through():
    row = {"Check Size": "$25k - $50k"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k"]
    assert "custom:check_size_institutional" not in result


def test_check_size_personal_multiple_buckets_comma_space_split():
    # The exact real Alex Pepe case from the 2026-08-06 audit.
    row = {"Check Size": "$25k - $50k, $50k - $100k"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


def test_check_size_institutional_reads_its_own_column_only():
    row = {"Check Size (Institutional)": "$1M - $2M, $2M - $5M"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_institutional"] == ["$1M - $2M", "$2M - $5M"]
    assert "custom:check_size_personal" not in result


def test_check_size_personal_and_institutional_are_independent_never_copied():
    # Real shape from the audit: a contact with DIFFERENT populated values
    # in both columns -- institutional must never be derived from personal.
    row = {"Check Size": "$1k - $10k, $10k - $25k", "Check Size (Institutional)": "$500k - $1M, $1M - $2M"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$1k - $10k", "$10k - $25k"]
    assert result["custom:check_size_institutional"] == ["$500k - $1M", "$1M - $2M"]


def test_check_size_normalizes_hyphen_variants():
    for dash in ["-", "–", "—", "−"]:  # hyphen, en dash, em dash, minus sign
        row = {"Check Size": f"$25k {dash} $50k"}
        result = classify_check_size(row, CHECK_SIZE_CONTEXT)
        assert result["custom:check_size_personal"] == ["$25k - $50k"], f"dash variant {dash!r} failed"


def test_check_size_normalizes_whitespace_variants():
    for raw in ["$25k-$50k", "$25k - $50k", "$25k  -  $50k", " $25k-$50k "]:
        row = {"Check Size": raw}
        result = classify_check_size(row, CHECK_SIZE_CONTEXT)
        assert result["custom:check_size_personal"] == ["$25k - $50k"], f"whitespace variant {raw!r} failed"


def test_check_size_normalizes_casing():
    row = {"Check Size": "$25K - $50K"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k"]


def test_check_size_dollar_amount_with_thousands_comma_is_not_split():
    # The critical edge case: a single value like "$5,000-$10,000" must
    # NOT be shredded by a naive split(",") into "$5" and "000-$10,000".
    # It doesn't match any of the 11 canonical buckets, so it's correctly
    # dropped -- but it must be evaluated as ONE token, not several.
    row = {"Check Size": "$5,000-$10,000"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result == {}


def test_check_size_thousands_comma_alongside_a_real_multi_value_list():
    # A mix: one thousands-separated free-text value plus a genuine
    # multi-select list, comma-space-separated as usual. Confirms the
    # ", " split isn't fooled by a bare "," elsewhere in the cell.
    row = {"Check Size": "$50,000-$200,000 avg., $25k - $50k, $50k - $100k"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


def test_check_size_free_text_never_guessed_into_a_bucket():
    for raw in ["Depends on Asset Allocation", "up to 50k", "It depends", "NA", "TBD", "Varies", "Open"]:
        row = {"Check Size": raw}
        result = classify_check_size(row, CHECK_SIZE_CONTEXT)
        assert result == {}, f"free text {raw!r} must never be guessed into a bucket"


def test_check_size_range_spanning_multiple_buckets_never_guessed():
    # "$25k-$100k" spans two real buckets ($25k-$50k and $50k-$100k) --
    # must be dropped, never mapped to either (or both).
    row = {"Check Size": "$25k-$100k"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result == {}


def test_check_size_long_free_text_tail_after_real_buckets_is_dropped_not_merged():
    # Real shape from the audit (Oliver Carmack): several genuine buckets
    # followed by a long free-text sentence in the same cell.
    row = {
        "Check Size": (
            "$25k - $50k, $50k - $100k, $100k - $250k, "
            "We tend to concentrate our investments as a firm into ticket sizes $100M plus"
        )
    }
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k", "$50k - $100k", "$100k - $250k"]


def test_check_size_deduplicates_repeated_bucket():
    row = {"Check Size": "$25k - $50k, $25k-$50k, $50k - $100k"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result["custom:check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


def test_check_size_missing_columns_produce_no_keys():
    row = {"First Name": "Ada"}
    result = classify_check_size(row, CHECK_SIZE_CONTEXT)
    assert result == {}


def test_check_size_no_context_options_drops_everything():
    row = {"Check Size": "$25k - $50k"}
    result = classify_check_size(row, NO_CONTEXT)
    assert result == {}


def test_check_size_every_defined_option_passes():
    for option in CHECK_SIZE_OPTIONS:
        row = {"Check Size": option}
        result = classify_check_size(row, CHECK_SIZE_CONTEXT)
        assert result["custom:check_size_personal"] == [option]


# --- classify_accredited_status (2026-08-06 broader-audit Phase 1) ---

ACCREDITED_STATUS_CONTEXT = {"accredited_status_options": {"Yes", "No"}}


def test_accredited_status_valid_value_passes_through():
    row = {"Accredited Status": "Yes"}
    result = classify_accredited_status(row, ACCREDITED_STATUS_CONTEXT)
    assert result["custom:accredited_status"] == "Yes"


def test_accredited_status_every_defined_option_passes():
    for option in ["Yes", "No"]:
        row = {"Accredited Status": option}
        result = classify_accredited_status(row, ACCREDITED_STATUS_CONTEXT)
        assert result["custom:accredited_status"] == option


def test_accredited_status_unrecognized_value_produces_no_key():
    row = {"Accredited Status": "Maybe"}
    result = classify_accredited_status(row, ACCREDITED_STATUS_CONTEXT)
    assert result == {}


def test_accredited_status_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_accredited_status(row, ACCREDITED_STATUS_CONTEXT)
    assert result == {}


def test_accredited_status_no_context_options_drops_everything():
    row = {"Accredited Status": "Yes"}
    result = classify_accredited_status(row, NO_CONTEXT)
    assert result == {}


# --- classify_legacy_thesis_columns (2026-08-06 broader-audit Phase 2) ---
#
# Wires crm_migration.py's own LEGACY_THESIS_COLUMN_VALUE_MAPS /
# _translate_comma_joined_column into the live classification-rule
# pipeline -- reused verbatim, not reimplemented. Deliberately excludes
# "Founder Diversity Preference" (still an open duplicate-destination
# question against the gender-specific dining column, per the broader
# audit) even though it's a key in LEGACY_THESIS_COLUMN_VALUE_MAPS.


def test_legacy_deal_stage_translates_abbreviated_to_canonical():
    row = {"Deal Stage": "Friends & family, Pre-seed, Seed, Series A"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_deal_stages"] == [
        "Friends & Family (idea or concept stage, often pre-incorporation)",
        "Pre-Seed (early development, pre-revenue or minimal traction)",
        "Seed (product in market, early customers or pilots)",
        "Series A (scaling phase, revenue traction, team expansion)",
    ]


def test_legacy_deal_stage_series_cde_shorthand_collapses_and_dedupes():
    # Real Alex Pepe shape: "Series C,D,E" has no repeated "Series" -- after the
    # comma-split, "D"/"E" arrive as bare tokens that all resolve to the SAME
    # canonical "Series B or later" bucket as "Series B"/"Series C" -- must
    # collapse to one entry, not four duplicates.
    row = {"Deal Stage": "Series B, Series C,D,E"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_deal_stages"] == [
        "Series B or later (growth or expansion stage, institutional rounds)",
    ]


def test_legacy_asset_types_translates_and_preserves_internal_commas():
    # "Collectibles (e.g., art, wine, watches)" contains its own internal commas --
    # produced only AFTER the raw abbreviated text ("Collectibles") is comma-split
    # and translated, so splitting the semicolon-joined result can never shred it.
    row = {"Investing in these types of assets": "Collectibles, Real estate, Venture capital"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_asset_types"] == [
        "Collectibles (e.g., art, wine, watches)",
        "Real estate (direct ownership, syndications, REITs)",
        "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)",
    ]


def test_legacy_business_models_translates_abbreviated_to_canonical():
    row = {"Investing in these business models:": "Marketplaces, Software as a Service (SaaS)"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_business_models"] == [
        "Marketplaces (e.g., Airbnb, Uber-style platforms)",
        "Software as a Service (SaaS)",
    ]


def test_legacy_meeting_preferences_translates_abbreviated_to_canonical():
    row = {"Would like to meet founders by": "Email intro, Zoom Call"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_meeting_preferences"] == [
        "In an email intro", "I'd do a Zoom call",
    ]


def test_legacy_thesis_columns_unrecognized_token_preserved_verbatim():
    row = {"Deal Stage": "Some Brand New Stage"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_deal_stages"] == ["Some Brand New Stage"]


def test_legacy_thesis_columns_never_touches_founder_diversity_preference():
    row = {"Founder Diversity Preference": "Open to investing in anyone"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result == {}


def test_legacy_thesis_columns_missing_columns_produce_no_keys():
    row = {"First Name": "Ada"}
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result == {}


def test_legacy_thesis_columns_handles_each_independently():
    row = {
        "Deal Stage": "Seed",
        "Investing in these types of assets": "Real estate",
        "Investing in these business models:": "Marketplaces",
        "Would like to meet founders by": "Email intro",
    }
    result = classify_legacy_thesis_columns(row, NO_CONTEXT)
    assert result["thesis_private_deal_stages"] == ["Seed (product in market, early customers or pilots)"]
    assert result["thesis_private_asset_types"] == ["Real estate (direct ownership, syndications, REITs)"]
    assert result["thesis_private_business_models"] == ["Marketplaces (e.g., Airbnb, Uber-style platforms)"]
    assert result["thesis_private_meeting_preferences"] == ["In an email intro"]


# --- classify_how_early_do_you_invest (2026-08-06 broader-audit Phase 2) ---
#
# Wires crm_migration.py's own HOW_EARLY_KNOWN_PHRASES / _retokenize_known_phrases
# into the live classification-rule pipeline -- reused verbatim.


def test_how_early_do_you_invest_internal_comma_phrases_not_shredded():
    # The critical edge case: "Great team, no revenue" and "Great team, some
    # revenue" are each ONE canonical option containing their own comma. A naive
    # comma-split would produce "Great team" + "no revenue" as two wrong values.
    row = {"How early do you invest?": "Great team, no revenue, Great team, some revenue"}
    result = classify_how_early_do_you_invest(row, NO_CONTEXT)
    assert result["custom:how_early_do_you_invest"] == ["Great team, no revenue", "Great team, some revenue"]


def test_how_early_do_you_invest_mixed_comma_and_plain_phrases():
    row = {"How early do you invest?": "Great team, no revenue, $10k-$50k MRR / GMV, $1M+ MRR / GMV"}
    result = classify_how_early_do_you_invest(row, NO_CONTEXT)
    assert result["custom:how_early_do_you_invest"] == [
        "Great team, no revenue", "$10k-$50k MRR / GMV", "$1M+ MRR / GMV",
    ]


def test_how_early_do_you_invest_deduplicates_repeated_phrase():
    row = {"How early do you invest?": "$1M+ MRR / GMV, $1M+ MRR / GMV"}
    result = classify_how_early_do_you_invest(row, NO_CONTEXT)
    assert result["custom:how_early_do_you_invest"] == ["$1M+ MRR / GMV"]


def test_how_early_do_you_invest_stops_at_unrecognized_fragment():
    # _retokenize_known_phrases stops (never guesses) at the first unrecognized
    # fragment -- documenting this inherited behavior, not introducing it.
    row = {"How early do you invest?": "$1M+ MRR / GMV, Some Unknown Phrase, Great team, no revenue"}
    result = classify_how_early_do_you_invest(row, NO_CONTEXT)
    assert result["custom:how_early_do_you_invest"] == ["$1M+ MRR / GMV"]


def test_how_early_do_you_invest_missing_column_produces_no_keys():
    row = {"First Name": "Ada"}
    result = classify_how_early_do_you_invest(row, NO_CONTEXT)
    assert result == {}


# --- registry / integration ---


def test_apply_classification_rules_runs_the_full_registry():
    row = {
        "Industry": "Consumer Electronics",
        "Main Industry": "Healthcare",
        "Sub-industry": "Biotech",
        "Investor type": "Angel Investor",
        "Role": "Investor, VP",
        "Dinner Subscriptions": "Investor Dinners, Sigma Librae Dinners, Retreats",
        "Dinners Attended": "Investor Dinners, Savvy [2.25.2025] Austin",
        "Chris Degree Connection": "1st degree",
        "Age Range": "31-40",
        "Gender": "Male",
        "Stage": "Interested",
        "Check Size": "$25k - $50k",
        "Check Size (Institutional)": "$1M - $2M",
        "Accredited Status": "Yes",
        "Deal Stage": "Seed",
        "Investing in these types of assets": "Real estate",
        "Investing in these business models:": "Marketplaces",
        "Would like to meet founders by": "Email intro",
        "How early do you invest?": "Great team, no revenue",
    }
    full_context = {
        **ROLE_CONTEXT, **CHRIS_DEGREE_CONTEXT, **AGE_RANGE_CONTEXT, **GENDER_CONTEXT, **ENGAGEMENT_STAGE_CONTEXT,
        **CHECK_SIZE_CONTEXT, **ACCREDITED_STATUS_CONTEXT,
    }
    result = apply_classification_rules(row, full_context)
    assert result["industry"] == "Consumer Electronics"
    assert result["custom:investment_industry"] == ["Healthcare", "Biotech"]
    assert result["custom:investor_type"] == ["Angel Investor"]
    assert result["thesis_investor_mode"] == "Privately"
    assert result["custom:role"] == ["Investor"]  # VP dropped
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners"]  # normalized, Retreats dropped
    assert result["custom:dinners_attended"] == ["Investor Dinners", "Savvy [2.25.2025] Austin"]  # verbatim, not normalized
    assert result["custom:chris_degree_connection"] == "1st degree"
    assert result["custom:age_range"] == "31-40"
    assert result["custom:gender"] == "Male"
    assert result["custom:engagement_stage"] == "Interested"
    assert result["custom:check_size_personal"] == ["$25k - $50k"]
    assert result["custom:check_size_institutional"] == ["$1M - $2M"]
    assert result["custom:accredited_status"] == "Yes"
    assert result["thesis_private_deal_stages"] == ["Seed (product in market, early customers or pilots)"]
    assert result["thesis_private_asset_types"] == ["Real estate (direct ownership, syndications, REITs)"]
    assert result["thesis_private_business_models"] == ["Marketplaces (e.g., Airbnb, Uber-style platforms)"]
    assert result["thesis_private_meeting_preferences"] == ["In an email intro"]
    assert result["custom:how_early_do_you_invest"] == ["Great team, no revenue"]
    assert "funding_stage" not in result  # the exact collision this fix prevents


@pytest.mark.asyncio
async def test_build_classification_context_fetches_live_options_for_every_validated_field():
    class FakeField:
        def __init__(self, options):
            self.options = options

    FIELDS = {
        "role": FakeField(["Investor", "Founder", "CEO"]),
        "chris_degree_connection": FakeField(["1st degree", "2nd degree", "3rd degree", "N/A"]),
        "age_range": FakeField(["18-22", "23-30"]),
        "gender": FakeField(["Male", "Female"]),
        "engagement_stage": FakeField(["Cold", "Interested", "Unresponsive"]),
        "check_size_personal": FakeField(["$1k - $10k", "$10k - $25k"]),
        "check_size_institutional": FakeField(["$500k - $1M", "$1M - $2M"]),
        "accredited_status": FakeField(["Yes", "No"]),
    }

    class FakeCustomFieldStore:
        async def get_by_field_key(self, field_key):
            return FIELDS.get(field_key)

    context = await build_classification_context(FakeCustomFieldStore())
    assert context["role_options"] == {"Investor", "Founder", "CEO"}
    assert context["chris_degree_connection_options"] == {"1st degree", "2nd degree", "3rd degree", "N/A"}
    assert context["age_range_options"] == {"18-22", "23-30"}
    assert context["gender_options"] == {"Male", "Female"}
    assert context["engagement_stage_options"] == {"Cold", "Interested", "Unresponsive"}
    assert context["check_size_personal_options"] == {"$1k - $10k", "$10k - $25k"}
    assert context["check_size_institutional_options"] == {"$500k - $1M", "$1M - $2M"}
    assert context["accredited_status_options"] == {"Yes", "No"}


@pytest.mark.asyncio
async def test_build_classification_context_handles_missing_fields():
    class FakeCustomFieldStore:
        async def get_by_field_key(self, field_key):
            return None

    context = await build_classification_context(FakeCustomFieldStore())
    assert context["role_options"] == set()
    assert context["chris_degree_connection_options"] == set()
    assert context["age_range_options"] == set()
    assert context["gender_options"] == set()
    assert context["engagement_stage_options"] == set()
    assert context["check_size_personal_options"] == set()
    assert context["check_size_institutional_options"] == set()
    assert context["accredited_status_options"] == set()
