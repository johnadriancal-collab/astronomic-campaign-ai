"""
Tests for the centralized CSV-import classification layer
(app/services/crm_classification_rules.py). classify_industry is the first
rule; these tests also double as a template for future rules (Investor
Type, Role, Check Size, etc.) added to the same registry.
"""

from app.services.crm_classification_rules import apply_classification_rules, classify_industry


def test_industry_comes_from_industry_column_only():
    row = {"Industry": "Information Technology & Services", "Main Industry": "Healthcare", "Sub-industry": "Biotech"}
    result = classify_industry(row)
    assert result["industry"] == "Information Technology & Services"


def test_investment_industry_merges_main_then_sub_preserving_order():
    row = {"Main Industry": "Healthcare, Fintech", "Sub-industry": "Biotech, Digital Health"}
    result = classify_industry(row)
    assert result["custom:investment_industry"] == ["Healthcare", "Fintech", "Biotech", "Digital Health"]


def test_investment_industry_dedups_exact_duplicate_values():
    row = {"Main Industry": "Healthcare, Gaming", "Sub-industry": "Gaming, Biotech"}
    result = classify_industry(row)
    assert result["custom:investment_industry"] == ["Healthcare", "Gaming", "Biotech"]


def test_new_unseen_industry_values_pass_through_unchanged():
    row = {"Industry": "Artisanal Cheese Production", "Main Industry": "Space Tourism", "Sub-industry": "Zero-G Hospitality"}
    result = classify_industry(row)
    assert result["industry"] == "Artisanal Cheese Production"
    assert result["custom:investment_industry"] == ["Space Tourism", "Zero-G Hospitality"]


def test_missing_columns_produce_no_keys():
    row = {"First Name": "Ada", "Last Name": "Lovelace"}
    result = classify_industry(row)
    assert result == {}


def test_only_main_industry_present():
    row = {"Main Industry": "Healthcare"}
    result = classify_industry(row)
    assert result["custom:investment_industry"] == ["Healthcare"]
    assert "industry" not in result


def test_only_sub_industry_present():
    row = {"Sub-industry": "Biotech, Digital Health"}
    result = classify_industry(row)
    assert result["custom:investment_industry"] == ["Biotech", "Digital Health"]


def test_header_variants_are_matched_case_and_spacing_insensitive():
    row = {"industry": "Media Production", "main industry": "Sports", "sub industry": "Athlete Wellness"}
    result = classify_industry(row)
    assert result["industry"] == "Media Production"
    assert result["custom:investment_industry"] == ["Sports", "Athlete Wellness"]


def test_blank_values_are_treated_as_missing():
    row = {"Industry": "  ", "Main Industry": "", "Sub-industry": "Biotech"}
    result = classify_industry(row)
    assert "industry" not in result
    assert result["custom:investment_industry"] == ["Biotech"]


def test_apply_classification_rules_runs_the_full_registry():
    row = {"Industry": "Consumer Electronics", "Main Industry": "Healthcare", "Sub-industry": "Biotech"}
    result = apply_classification_rules(row)
    assert result["industry"] == "Consumer Electronics"
    assert result["custom:investment_industry"] == ["Healthcare", "Biotech"]
