"""
Pure unit tests for app/services/astro_parser.py -- no HTTP, no store, no
network. Live option lists below are copied from real production values
(confirmed 2026-08-12 via GET /crm/custom-fields) rather than invented, so
these tests exercise the parser against the actual vocabulary it will see.
"""

from app.models.crm import INDUSTRY_OPTIONS
from app.services.astro_parser import (
    CHECK_SIZE_PERSONAL_FIELD_KEY,
    INVESTMENT_INDUSTRY_FIELD_KEY,
    INVESTOR_MODE_FIELD_KEY,
    INVESTOR_TYPE_FIELD_KEY,
    ParsedCommand,
    UnresolvedCommand,
    parse,
)

REAL_INVESTOR_TYPE_OPTIONS = [
    "Angel Investor", "Family Office", "Fund LP", "I sponsor deals that I find",
    "Institutional Investor", "Invest with group of Angels",
    "Participate in syndicated investments", "Private Equity", "Private Investor",
    "Venture Capital",
]
REAL_CHECK_SIZE_ORDERED_OPTIONS = [
    "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
    "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+",
]


def run(text: str) -> ParsedCommand | UnresolvedCommand:
    return parse(text, REAL_INVESTOR_TYPE_OPTIONS, REAL_CHECK_SIZE_ORDERED_OPTIONS)


def condition_dict(conditions):
    return {(c.field, c.operator): c.value for c in conditions}


# --- your 8 example commands, one test each ---


def test_find_investors_in_austin():
    result = run("Find investors in Austin")
    assert isinstance(result, ParsedCommand)
    assert result.intent == "search_contacts"
    conditions = condition_dict(result.filters)
    assert conditions[("city", "eq")] == "Austin"
    assert len(result.filters) == 1


def test_find_family_offices_in_texas():
    result = run("Find family offices in Texas")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTOR_TYPE_FIELD_KEY, "contains_any")] == ["Family Office"]
    assert conditions[("state", "eq")] == "Texas"


def test_find_investors_interested_in_full_industry_name():
    result = run("Find investors interested in Artificial Intelligence / Machine Learning")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTMENT_INDUSTRY_FIELD_KEY, "contains_any")] == ["Artificial Intelligence / Machine Learning"]


def test_find_investors_interested_in_ai_alias():
    result = run("Find investors interested in AI")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTMENT_INDUSTRY_FIELD_KEY, "contains_any")] == ["Artificial Intelligence / Machine Learning"]


def test_find_investors_in_austin_interested_in_aerospace_and_defense():
    result = run("Find investors in Austin interested in Aerospace & Defense")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[("city", "eq")] == "Austin"
    assert conditions[(INVESTMENT_INDUSTRY_FIELD_KEY, "contains_any")] == ["Aerospace & Defense"]


def test_find_investors_with_100k_plus_check_sizes():
    result = run("Find investors with $100k+ check sizes")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(CHECK_SIZE_PERSONAL_FIELD_KEY, "gte")] == "$100k - $250k"


def test_show_institutional_investors_resolves_to_investor_type_tag():
    """Approved decision: 'institutional investor(s)' -> the explicit investor_type
    tag, NOT thesis_investor_mode -- distinct from the 'institutionally' tests below."""
    result = run("Show institutional investors")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTOR_TYPE_FIELD_KEY, "contains_any")] == ["Institutional Investor"]
    assert INVESTOR_MODE_FIELD_KEY not in {f for f, _ in conditions}


def test_how_many_family_offices_are_in_austin():
    result = run("How many family offices are in Austin?")
    assert isinstance(result, ParsedCommand)
    assert result.intent == "count_contacts"
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTOR_TYPE_FIELD_KEY, "contains_any")] == ["Family Office"]
    assert conditions[("city", "eq")] == "Austin"


# --- investor mode: HOW someone invests, distinct from the investor_type tag above ---


def test_invests_institutionally_uses_investor_mode_not_investor_type():
    result = run("Find investors who invest institutionally")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTOR_MODE_FIELD_KEY, "eq")] == ["Institutionally", "Both"]
    assert INVESTOR_TYPE_FIELD_KEY not in {f for f, _ in conditions}


def test_bare_institutionally_resolves_to_investor_mode():
    result = run("Find investors institutionally")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(INVESTOR_MODE_FIELD_KEY, "eq")] == ["Institutionally", "Both"]


def test_invests_both_privately_and_institutionally_collapses_to_both_via_and():
    """Two separate eq conditions on the SAME field, ANDed by the existing query
    engine, naturally collapse to exactly 'Both' -- no special-cased phrase needed."""
    result = run("Find investors who invest both privately and institutionally")
    assert isinstance(result, ParsedCommand)
    matching = [c for c in result.filters if c.field == INVESTOR_MODE_FIELD_KEY]
    assert len(matching) == 2
    values = {tuple(c.value) for c in matching}
    assert values == {("Institutionally", "Both"), ("Privately", "Both")}


# --- check size edge cases ---


def test_check_size_rounds_up_to_next_bucket_when_between_boundaries():
    result = run("Find investors with $75k+ check sizes")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(CHECK_SIZE_PERSONAL_FIELD_KEY, "gte")] == "$100k - $250k"


def test_check_size_above_top_bucket_uses_top_bucket():
    result = run("Find investors with $50M+ check sizes")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(CHECK_SIZE_PERSONAL_FIELD_KEY, "gte")] == "$10M+"


def test_check_size_exact_bucket_boundary():
    result = run("Find investors with $1M+ check sizes")
    assert isinstance(result, ParsedCommand)
    conditions = condition_dict(result.filters)
    assert conditions[(CHECK_SIZE_PERSONAL_FIELD_KEY, "gte")] == "$1M - $2M"


# --- combining multiple filters, and the no-filter degenerate case ---


def test_plain_find_investors_has_no_filters():
    result = run("Find investors")
    assert isinstance(result, ParsedCommand)
    assert result.filters == []
    assert result.understood_as == "(no filters -- showing all contacts)"


def test_archived_inclusion_phrase():
    result = run("Find investors in Austin including archived")
    assert isinstance(result, ParsedCommand)
    assert result.include_archived is True


def test_default_excludes_archived():
    result = run("Find investors in Austin")
    assert isinstance(result, ParsedCommand)
    assert result.include_archived is False


# --- unresolved: never invent a filter for something unrecognized ---


def test_unrecognized_descriptive_phrase_is_unresolved_not_guessed():
    result = run("Find high quality investors in Austin")
    assert isinstance(result, UnresolvedCommand)
    assert result.understood == {"City": "Austin"}
    assert "high quality" in result.unresolved_phrase
    assert "Austin" not in result.unresolved_phrase


def test_unrecognized_industry_phrase_is_unresolved():
    result = run("Find investors interested in underwater basket weaving")
    assert isinstance(result, UnresolvedCommand)
    assert "underwater basket weaving" in result.unresolved_phrase


def test_unrecognized_location_is_unresolved_not_guessed():
    """Small, explicit gazetteer per the approved design -- a real city NOT in the
    table must never be silently accepted as a city filter."""
    result = run("Find investors in Boston")
    assert isinstance(result, UnresolvedCommand)
    assert "boston" in result.unresolved_phrase


def test_investor_type_alias_not_in_live_options_is_unresolved():
    """If the live custom field's options ever drop an alias's target, that alias
    must stop resolving -- never fall back to a value that no longer really exists."""
    options_without_family_office = [o for o in REAL_INVESTOR_TYPE_OPTIONS if o != "Family Office"]
    result = parse("Find family offices in Austin", options_without_family_office, REAL_CHECK_SIZE_ORDERED_OPTIONS)
    assert isinstance(result, UnresolvedCommand)
    assert "family offices" in result.unresolved_phrase


def test_no_recognized_verb_is_unresolved():
    result = parse("family offices in austin", REAL_INVESTOR_TYPE_OPTIONS, REAL_CHECK_SIZE_ORDERED_OPTIONS)
    assert isinstance(result, UnresolvedCommand)
    assert "Find/Show/Search" in result.message


def test_every_industry_alias_target_is_a_real_industry_option():
    """Structural guarantee, not just a spot check -- see the assert already
    enforced at import time in astro_parser.py; this re-confirms it from the test
    side using the same canonical INDUSTRY_OPTIONS list."""
    from app.services.astro_parser import _INDUSTRY_ALIASES

    for alias, target in _INDUSTRY_ALIASES.items():
        assert target in INDUSTRY_OPTIONS, f"{alias!r} -> {target!r} is not a real industry option"
