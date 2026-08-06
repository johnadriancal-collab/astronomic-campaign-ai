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
    classify_dinner_subscriptions,
    classify_industry,
    classify_investor_mode,
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


# --- registry / integration ---


def test_apply_classification_rules_runs_the_full_registry():
    row = {
        "Industry": "Consumer Electronics",
        "Main Industry": "Healthcare",
        "Sub-industry": "Biotech",
        "Investor type": "Angel Investor",
        "Role": "Investor, VP",
        "Dinner Subscriptions": "Investor Dinners, Sigma Librae Dinners, Retreats",
    }
    result = apply_classification_rules(row, ROLE_CONTEXT)
    assert result["industry"] == "Consumer Electronics"
    assert result["custom:investment_industry"] == ["Healthcare", "Biotech"]
    assert result["custom:investor_type"] == ["Angel Investor"]
    assert result["thesis_investor_mode"] == "Privately"
    assert result["custom:role"] == ["Investor"]  # VP dropped
    assert result["custom:dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners"]  # normalized, Retreats dropped


@pytest.mark.asyncio
async def test_build_classification_context_fetches_live_role_options():
    class FakeRoleField:
        options = ["Investor", "Founder", "CEO"]

    class FakeCustomFieldStore:
        async def get_by_field_key(self, field_key):
            assert field_key == "role"
            return FakeRoleField()

    context = await build_classification_context(FakeCustomFieldStore())
    assert context["role_options"] == {"Investor", "Founder", "CEO"}


@pytest.mark.asyncio
async def test_build_classification_context_handles_missing_role_field():
    class FakeCustomFieldStore:
        async def get_by_field_key(self, field_key):
            return None

    context = await build_classification_context(FakeCustomFieldStore())
    assert context["role_options"] == set()
