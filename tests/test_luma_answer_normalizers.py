"""
Pure LumaAnswerNormalizer transforms -- no FastAPI/service involved, every
case directly testable.
"""

import pytest

from app.models.luma import LumaAnswerNormalizer
from app.services.luma_answer_normalizers import apply_normalizer, normalize_linkedin_url


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
