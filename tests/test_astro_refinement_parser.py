"""
Pure unit tests for app/services/astro_parser.py's Phase 1.1 refinement layer
(attempt_refinement()) -- no HTTP, no store, no network. Real production
option values (same constants used in test_astro_parser.py), so these
exercise the parser against the actual vocabulary it sees in production.
"""

from app.models.crm import FilterCondition
from app.services.astro_parser import (
    CHECK_SIZE_PERSONAL_FIELD_KEY,
    INVESTMENT_INDUSTRY_FIELD_KEY,
    INVESTOR_MODE_FIELD_KEY,
    INVESTOR_TYPE_FIELD_KEY,
    ParsedCommand,
    UnresolvedCommand,
    attempt_refinement,
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


def refine(text, filters, include_archived=False, intent="search_contacts"):
    return attempt_refinement(text, filters, include_archived, intent, REAL_INVESTOR_TYPE_OPTIONS, REAL_CHECK_SIZE_ORDERED_OPTIONS)


def field_map(filters):
    return {c.field: c for c in filters}


# --- RESET ---


def test_start_over_clears_all_filters():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Start over", starting)
    assert isinstance(result, ParsedCommand)
    assert result.filters == []
    assert result.operation == "reset"
    assert result.intent == "search_contacts"
    assert result.message_template(2360) == "Cleared all filters. Showing 2360 contacts."


def test_show_everyone_again_resets_distinct_from_show_them_again():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Show everyone again", starting, intent="count_contacts")
    assert isinstance(result, ParsedCommand)
    assert result.filters == []
    assert result.operation == "reset"


# --- intent-only change (filters preserved) ---


def test_show_them_again_switches_to_search_preserves_filters():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Show them again", starting, intent="count_contacts")
    assert isinstance(result, ParsedCommand)
    assert result.intent == "search_contacts"
    assert result.filters == starting
    assert result.operation == "change_intent"
    assert result.message_template(11) == "Showing 11 contacts matching your current filters."


def test_how_many_are_left_switches_to_count_preserves_filters():
    starting = [
        FilterCondition(field="city", operator="eq", value="Austin"),
        FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"]),
    ]
    result = refine("How many are left?", starting, intent="search_contacts")
    assert isinstance(result, ParsedCommand)
    assert result.intent == "count_contacts"
    assert result.filters == starting
    assert result.operation == "change_intent"
    assert result.message_template(4) == "4 contacts match your current filters."


# --- "Only X" -- REPLACE the ONE field, leave everything else untouched ---


def test_only_austin_adds_city_leaves_existing_state_untouched():
    starting = [
        FilterCondition(field="state", operator="eq", value="Texas"),
        FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"]),
        FilterCondition(field=INVESTMENT_INDUSTRY_FIELD_KEY, operator="contains_any", value=["Artificial Intelligence / Machine Learning"]),
    ]
    result = refine("Only Austin", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm["state"].value == "Texas"  # untouched
    assert fm["city"].value == "Austin"  # added
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office"]  # untouched
    assert fm[INVESTMENT_INDUSTRY_FIELD_KEY].value == ["Artificial Intelligence / Machine Learning"]  # untouched
    assert result.operation == "replace"
    assert result.changed_field == "city"
    assert result.message_template(11) == "Showing 11 contacts in Austin. Your other filters are unchanged."


def test_only_texas_replaces_state_leaves_city_untouched():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Only Texas", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm["city"].value == "Austin"
    assert fm["state"].value == "Texas"
    assert result.changed_field == "state"


def test_only_family_offices_replaces_investor_type_value_list():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Angel Investor", "Venture Capital"])]
    result = refine("Only family offices", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office"]  # replaced, not unioned
    assert result.operation == "replace"


def test_only_100k_plus_adds_check_size_when_none_existed():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Only $100k+", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[CHECK_SIZE_PERSONAL_FIELD_KEY].value == "$100k - $250k"
    assert fm["city"].value == "Austin"  # untouched
    assert result.operation == "replace"
    assert result.changed_field == CHECK_SIZE_PERSONAL_FIELD_KEY
    assert result.message_template(4) == "Added a $100k+ check-size filter. 4 contacts match."


def test_only_50k_plus_updates_existing_check_size():
    starting = [FilterCondition(field=CHECK_SIZE_PERSONAL_FIELD_KEY, operator="gte", value="$100k - $250k")]
    result = refine("Only $50k+", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[CHECK_SIZE_PERSONAL_FIELD_KEY].value == "$50k - $100k"
    assert result.message_template(20) == "Updated the check-size filter to $50k+. 20 contacts match."


# --- bare phrase with no keyword defaults to the same behavior as "only" ---


def test_bare_dollar_amount_defaults_to_check_size_filter():
    starting = []
    result = refine("$100k+", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[CHECK_SIZE_PERSONAL_FIELD_KEY].value == "$100k - $250k"


# --- ADD -- union into a multi-select field, never a second AND'd condition ---


def test_add_family_offices_unions_into_existing_investor_type():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Venture Capital"])]
    result = refine("Add family offices", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Venture Capital", "Family Office"]
    assert len([c for c in result.filters if c.field == INVESTOR_TYPE_FIELD_KEY]) == 1  # ONE condition, not two ANDed
    assert result.operation == "add"
    assert result.message_template(15) == "Added Family Office to your Investor Type filter. 15 contacts match."


def test_include_institutional_investors_too_resolves_investor_type_tag():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"])]
    result = refine("Include institutional investors too", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office", "Institutional Investor"]
    assert result.operation == "add"


def test_add_when_no_prior_condition_behaves_like_a_fresh_add():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Add family offices", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office"]
    assert fm["city"].value == "Austin"


# --- REMOVE ---


def test_remove_check_size_filter_whole_field():
    starting = [
        FilterCondition(field=CHECK_SIZE_PERSONAL_FIELD_KEY, operator="gte", value="$100k - $250k"),
        FilterCondition(field="city", operator="eq", value="Austin"),
    ]
    result = refine("Remove the check size filter", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert CHECK_SIZE_PERSONAL_FIELD_KEY not in fm
    assert fm["city"].value == "Austin"  # untouched
    assert result.operation == "remove"
    assert result.message_template(11) == "Removed the Check Size Personal filter. 11 contacts match."


def test_remove_check_size_filter_is_idempotent_when_already_absent():
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    result = refine("Remove the check size filter", starting)
    assert isinstance(result, ParsedCommand)
    assert result.filters == starting  # no-op, harmless


def test_remove_location_strips_both_city_and_state():
    starting = [
        FilterCondition(field="city", operator="eq", value="Austin"),
        FilterCondition(field="state", operator="eq", value="Texas"),
        FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"]),
    ]
    result = refine("Remove location", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert "city" not in fm
    assert "state" not in fm
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office"]


def test_remove_specific_value_leaves_other_values_in_the_list():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office", "Venture Capital"])]
    result = refine("Remove family offices", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Venture Capital"]
    assert result.operation == "remove"
    assert result.changed_field == INVESTOR_TYPE_FIELD_KEY


def test_remove_specific_value_drops_whole_condition_when_it_was_the_only_value():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"])]
    result = refine("Remove family offices", starting)
    assert isinstance(result, ParsedCommand)
    assert not any(c.field == INVESTOR_TYPE_FIELD_KEY for c in result.filters)


def test_remove_archived():
    starting = []
    result = refine("Remove archived", starting, include_archived=True)
    assert isinstance(result, ParsedCommand)
    assert result.include_archived is False
    assert result.operation == "remove"


# --- Investor Mode refinement (distinct field from Investor Type, per approved design) ---


def test_only_institutionally_sets_investor_mode_not_investor_type():
    starting = [FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"])]
    result = refine("Only institutionally", starting)
    assert isinstance(result, ParsedCommand)
    fm = field_map(result.filters)
    assert fm[INVESTOR_MODE_FIELD_KEY].value == ["Institutionally", "Both"]
    assert fm[INVESTOR_TYPE_FIELD_KEY].value == ["Family Office"]  # untouched


# --- Ambiguity: context must remain COMPLETELY unchanged ---


def test_ambiguous_refinement_leaves_context_byte_for_byte_unchanged():
    starting = [
        FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=["Family Office"]),
        FilterCondition(field="city", operator="eq", value="Austin"),
    ]
    result = refine("Only good ones", starting, include_archived=False)
    assert isinstance(result, UnresolvedCommand)
    assert result.unchanged_query is not None
    assert result.unchanged_query.filters == starting
    assert result.unchanged_query.include_archived is False
    assert "good ones" in result.unresolved_phrase


def test_unresolved_refinement_never_mutates_the_input_list_object():
    """Defensive: attempt_refinement must never mutate the caller's list in place --
    confirms via identity/equality after the call that the ORIGINAL list is intact."""
    starting = [FilterCondition(field="city", operator="eq", value="Austin")]
    starting_copy = list(starting)
    refine("Only something unrecognizable", starting)
    assert starting == starting_copy


def test_multiple_fields_in_one_message_is_unresolved_not_guessed():
    """Approved scope limit: compound refinements (more than one field changed
    in a single message) are explicitly out of scope for 1.1."""
    starting = []
    result = refine("Only Austin and family offices", starting)
    assert isinstance(result, UnresolvedCommand)
    assert result.unchanged_query.filters == starting


def test_truly_unrecognized_bare_phrase_is_unresolved():
    result = refine("Boston", [])
    assert isinstance(result, UnresolvedCommand)
