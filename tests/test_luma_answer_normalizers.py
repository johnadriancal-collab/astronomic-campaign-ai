"""
Pure LumaAnswerNormalizer transforms -- no FastAPI/service involved, every
case directly testable.
"""

import pytest

from app.models.luma import LumaAnswerNormalizer
from app.services.luma_answer_normalizers import (
    apply_normalizer,
    normalize_check_size_personal_bucket,
    normalize_investor_type_labels,
    normalize_linkedin_url,
)

# The CRM's exact, live custom:check_size_personal options (12 total,
# confirmed against production) -- every translated output below must be
# an exact member of this set, never an approximation.
_CRM_CHECK_SIZE_PERSONAL_OPTIONS = {
    "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
    "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M",
    "$10M+", "Other:",
}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/in/john-adrian-c-9ba98176", "https://www.linkedin.com/in/john-adrian-c-9ba98176"),  # relative path (the real, common Luma shape)
        ("in/charlie", "https://www.linkedin.com/in/charlie"),  # relative path, no leading slash
    ],
)
def test_relative_path_is_normalized(raw, expected):
    assert normalize_linkedin_url(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.linkedin.com/in/alice", "https://www.linkedin.com/in/alice"),
        ("http://www.linkedin.com/in/alice", "https://www.linkedin.com/in/alice"),  # http -> https
        ("https://linkedin.com/in/bob", "https://www.linkedin.com/in/bob"),  # missing "www"
    ],
)
def test_already_complete_urls_remain_valid(raw, expected):
    assert normalize_linkedin_url(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("linkedin.com/in/dave", "https://www.linkedin.com/in/dave"),
        ("www.linkedin.com/in/erin", "https://www.linkedin.com/in/erin"),
    ],
)
def test_missing_scheme_is_normalized(raw, expected):
    assert normalize_linkedin_url(raw) == expected


@pytest.mark.parametrize("raw", ["not a linkedin url at all", "https://twitter.com/someone", "random text", "12345"])
def test_unrelated_or_invalid_strings_are_rejected(raw):
    assert normalize_linkedin_url(raw) is None


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_or_non_string_input_is_rejected(raw):
    assert normalize_linkedin_url(raw) is None


def test_non_string_types_are_rejected_not_crashed_on():
    assert normalize_linkedin_url(12345) is None
    assert normalize_linkedin_url(["/in/alice"]) is None
    assert normalize_linkedin_url({"value": "/in/alice"}) is None


def test_case_insensitive_domain_matching_preserves_path_case():
    assert normalize_linkedin_url("HTTPS://WWW.LINKEDIN.COM/in/CaseSensitivePath") == "https://www.linkedin.com/in/CaseSensitivePath"


# --- apply_normalizer dispatch ------------------------------------------


def test_apply_normalizer_none_passes_value_through_unchanged():
    assert apply_normalizer(None, "/in/alice") == "/in/alice"


def test_apply_normalizer_linkedin_dispatches_correctly():
    assert apply_normalizer(LumaAnswerNormalizer.LINKEDIN_URL, "/in/alice") == "https://www.linkedin.com/in/alice"


def test_apply_normalizer_linkedin_rejects_invalid_input():
    assert apply_normalizer(LumaAnswerNormalizer.LINKEDIN_URL, "garbage") is None


# --- normalize_check_size_personal_bucket -------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Under $25K", ["$1k - $10k", "$10k - $25k"]),
        ("$25K–$100K", ["$25k - $50k", "$50k - $100k"]),
        ("$100K–$250K", ["$100k - $250k"]),
        ("$250K–$500K", ["$250k - $500k"]),
        ("$500K+", ["$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+"]),
    ],
)
def test_all_five_check_size_translations_match_crm_options_exactly(raw, expected):
    result = normalize_check_size_personal_bucket(raw)
    assert result == expected
    assert set(result).issubset(_CRM_CHECK_SIZE_PERSONAL_OPTIONS)


def test_check_size_translation_is_case_and_whitespace_insensitive():
    assert normalize_check_size_personal_bucket("  under $25k  ") == ["$1k - $10k", "$10k - $25k"]


@pytest.mark.parametrize("raw", ["$1,000,000+", "Unknown", "", None, 12345, ["$500K+"]])
def test_unrecognized_check_size_values_are_skipped(raw):
    assert normalize_check_size_personal_bucket(raw) is None


def test_apply_normalizer_check_size_dispatches_correctly():
    assert apply_normalizer(LumaAnswerNormalizer.CHECK_SIZE_PERSONAL_BUCKET, "$500K+") == [
        "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+",
    ]


# --- normalize_investor_type_labels --------------------------------------


def test_syndicate_lead_is_translated_to_its_crm_equivalent():
    assert normalize_investor_type_labels("Syndicate Lead") == "I sponsor deals that I find"


def test_syndicate_lead_translation_is_case_and_whitespace_insensitive():
    assert normalize_investor_type_labels("  syndicate LEAD  ") == "I sponsor deals that I find"


def test_syndicate_lead_translated_within_a_multi_select_list():
    result = normalize_investor_type_labels(["Angel Investor", "Syndicate Lead"])
    assert result == ["Angel Investor", "I sponsor deals that I find"]


@pytest.mark.parametrize("raw", ["Fund Manager / General Partner", "Corporate Venture", "Angel Investor"])
def test_untranslatable_labels_pass_through_unchanged(raw):
    """These are NOT dropped here -- that's the downstream CRM
    option-allowlist filter's job, applied uniformly to every controlled
    select field."""
    assert normalize_investor_type_labels(raw) == raw


def test_untranslatable_labels_pass_through_unchanged_within_a_list():
    result = normalize_investor_type_labels(["Fund Manager / General Partner", "Corporate Venture"])
    assert result == ["Fund Manager / General Partner", "Corporate Venture"]


def test_apply_normalizer_investor_type_dispatches_correctly():
    assert apply_normalizer(LumaAnswerNormalizer.INVESTOR_TYPE_LABEL, ["Syndicate Lead"]) == ["I sponsor deals that I find"]
