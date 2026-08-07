"""
Unit tests for crm_filter_service.py: the More Filters field registry,
validation, and predicate engine -- direct against the pure functions, no
service/store layer, so operator semantics and edge cases are pinned down
precisely.
"""

from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType, FilterCondition, FilterFieldType, FilterQuery, FilterSort
from app.services.crm_filter_service import (
    FilterValidationError,
    build_registry,
    evaluate_condition,
    matches_query,
    query_contacts,
    sort_key,
    validate_condition,
    validate_query,
)


def make_contact(**overrides):
    now = datetime.now(timezone.utc)
    defaults = {"crm_contact_id": overrides.pop("crm_contact_id", "c1"), "created_at": now, "updated_at": now}
    return CrmContact(**defaults, **overrides)


def make_custom_field(field_key, field_type, options=None, active=True):
    now = datetime.now(timezone.utc)
    return CrmCustomFieldDefinition(
        crm_custom_field_id=field_key, field_key=field_key, label=field_key, field_type=field_type,
        options=options or [], active=active, created_at=now, updated_at=now,
    )


CHECK_SIZE_OPTIONS = [
    "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
    "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+", "Other:",
]
AGE_RANGE_OPTIONS = ["18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased"]


@pytest.fixture
def registry():
    return build_registry([
        make_custom_field("check_size_personal", CustomFieldType.MULTI_SELECT, CHECK_SIZE_OPTIONS),
        make_custom_field("age_range", CustomFieldType.SINGLE_SELECT, AGE_RANGE_OPTIONS),
        make_custom_field("revenue_stage", CustomFieldType.SINGLE_SELECT, ["$250K - $500K", "$500k - $1M", "$1M - $10M", "$10M - $100M"]),
        make_custom_field("total_funding", CustomFieldType.NUMBER),
        make_custom_field("last_raised_at", CustomFieldType.DATE),
        make_custom_field("do_not_call", CustomFieldType.BOOLEAN),
        make_custom_field("notes", CustomFieldType.TEXT),
        make_custom_field("investor_type", CustomFieldType.MULTI_SELECT, ["Angel Investor", "Family Office", "Venture Capital"]),
        make_custom_field("sub_industry", CustomFieldType.TEXT, active=False),
    ])


@pytest.fixture
def field_by_key(registry):
    return {f.key: f for f in registry}


# --- Registry construction ---


def test_registry_includes_core_and_active_custom_fields(registry):
    keys = {f.key for f in registry}
    assert "city" in keys
    assert "thesis_investor_mode" in keys
    assert "custom:check_size_personal" in keys


def test_registry_excludes_inactive_custom_fields(registry):
    keys = {f.key for f in registry}
    assert "custom:sub_industry" not in keys


def test_registry_has_no_duplicate_keys(registry):
    keys = [f.key for f in registry]
    assert len(keys) == len(set(keys))


def test_deprecated_check_size_thesis_fields_excluded(registry):
    """thesis_private_check_sizes/thesis_institutional_check_sizes are deprecated --
    check_size_personal/institutional (custom fields) are the sole canonical destination."""
    keys = {f.key for f in registry}
    assert "thesis_private_check_sizes" not in keys
    assert "thesis_institutional_check_sizes" not in keys


# --- Ordered field registry shape (Check Size / Age Range) ---


def test_check_size_marked_ordered_with_other_excluded(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    assert cs.ordered is True
    assert cs.ordered_options == CHECK_SIZE_OPTIONS[:-1]  # every bucket except "Other:"
    assert cs.non_ordered_options == ["Other:"]
    assert set(cs.operators) >= {"contains_any", "contains_all", "not_contains", "is_empty", "is_not_empty", "gt", "gte", "lt", "lte"}


def test_age_range_marked_ordered_with_retired_deceased_excluded(field_by_key):
    ar = field_by_key["custom:age_range"]
    assert ar.ordered is True
    assert ar.ordered_options == AGE_RANGE_OPTIONS[:-2]  # every bucket except Retired/Deceased
    assert set(ar.non_ordered_options) == {"Retired", "Deceased"}
    assert {"gt", "gte", "lt", "lte"} <= set(ar.operators)


def test_deal_stage_not_marked_ordered(field_by_key):
    """Deal Stage mixes a real stage progression with Fund LP/Secondary, which don't fit
    anywhere in that line -- confirmed with the user 2026-08-07, left unordered rather
    than guessed."""
    ds = field_by_key["thesis_private_deal_stages"]
    assert ds.ordered is False
    assert ds.ordered_options == []
    assert "gte" not in ds.operators


def test_revenue_stage_fully_ordered_no_outliers(field_by_key):
    rs = field_by_key["custom:revenue_stage"]
    assert rs.ordered is True
    assert rs.non_ordered_options == []
    assert len(rs.ordered_options) == 4


# --- Validation: invalid field / operator / value rejected ---


def test_validate_query_rejects_unknown_field(registry):
    query = FilterQuery(filters=[FilterCondition(field="not_a_real_field", operator="eq", value="x")])
    with pytest.raises(FilterValidationError, match="Unknown filterable field"):
        validate_query(query, registry)


def test_validate_query_rejects_disallowed_operator_for_field_type(registry):
    # "gte" is not in TEXT's operator list, and city is not ordered.
    query = FilterQuery(filters=[FilterCondition(field="city", operator="gte", value="Austin")])
    with pytest.raises(FilterValidationError, match="not allowed"):
        validate_query(query, registry)


def test_validate_query_rejects_gte_on_unordered_field(field_by_key):
    ds = field_by_key["thesis_private_deal_stages"]
    condition = FilterCondition(field=ds.key, operator="gte", value="Seed (product in market, early customers or pilots)")
    with pytest.raises(FilterValidationError, match="not allowed"):
        validate_condition(condition, ds)


def test_validate_query_rejects_missing_value_when_required(registry):
    query = FilterQuery(filters=[FilterCondition(field="city", operator="eq", value=None)])
    with pytest.raises(FilterValidationError, match="requires a value"):
        validate_query(query, registry)


def test_validate_query_allows_missing_value_for_is_empty(registry):
    query = FilterQuery(filters=[FilterCondition(field="city", operator="is_empty", value=None)])
    validate_query(query, registry)  # should not raise


def test_validate_query_rejects_non_numeric_value_for_number_field(field_by_key):
    tf = field_by_key["custom:total_funding"]
    condition = FilterCondition(field=tf.key, operator="gt", value="not-a-number")
    with pytest.raises(FilterValidationError, match="valid number"):
        validate_condition(condition, tf)


def test_validate_query_rejects_invalid_date_value(field_by_key):
    lr = field_by_key["custom:last_raised_at"]
    condition = FilterCondition(field=lr.key, operator="before", value="not-a-date")
    with pytest.raises(FilterValidationError, match="valid ISO date"):
        validate_condition(condition, lr)


def test_validate_query_rejects_value_outside_closed_option_list(field_by_key):
    it = field_by_key["custom:investor_type"]
    condition = FilterCondition(field=it.key, operator="contains_any", value=["Not A Real Investor Type"])
    with pytest.raises(FilterValidationError, match="not a valid option"):
        validate_condition(condition, it)


def test_validate_query_accepts_open_vocabulary_field_any_value(field_by_key):
    tech = field_by_key["technologies"]
    condition = FilterCondition(field=tech.key, operator="contains_any", value=["SomeNewTechNoOneHeardOf"])
    validate_condition(condition, tech)  # no options declared -- any string is accepted


# --- Ordinal operator rejection for non-ordered options (the exact 7 requested cases) ---


def test_check_size_gte_against_ordered_bucket_accepted(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    condition = FilterCondition(field=cs.key, operator="gte", value="$1M - $2M")
    validate_condition(condition, cs)  # should not raise


def test_check_size_gte_against_other_rejected(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    condition = FilterCondition(field=cs.key, operator="gte", value="Other:")
    with pytest.raises(FilterValidationError, match="ordered_options"):
        validate_condition(condition, cs)


def test_age_range_gte_against_ordered_bucket_accepted(field_by_key):
    ar = field_by_key["custom:age_range"]
    condition = FilterCondition(field=ar.key, operator="gte", value="51-60")
    validate_condition(condition, ar)  # should not raise


def test_age_range_gte_against_retired_rejected(field_by_key):
    ar = field_by_key["custom:age_range"]
    condition = FilterCondition(field=ar.key, operator="gte", value="Retired")
    with pytest.raises(FilterValidationError, match="ordered_options"):
        validate_condition(condition, ar)


def test_age_range_is_any_of_including_retired_and_deceased_works(field_by_key):
    ar = field_by_key["custom:age_range"]
    condition = FilterCondition(field=ar.key, operator="eq", value=["Retired", "Deceased"])
    validate_condition(condition, ar)  # should not raise -- normal categorical value
    contact = make_contact(custom_fields={"age_range": "Retired"})
    assert evaluate_condition(contact, condition, ar) is True


def test_check_size_is_any_of_including_other_works(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    condition = FilterCondition(field=cs.key, operator="contains_any", value=["Other:"])
    validate_condition(condition, cs)  # should not raise
    contact = make_contact(custom_fields={"check_size_personal": ["Other:"]})
    assert evaluate_condition(contact, condition, cs) is True


def test_existing_non_ordinal_fields_continue_to_behave_normally(field_by_key):
    """gender-like single-select fields (here: revenue_stage's sibling engagement
    concept) still work with is/is-empty even though this fixture set doesn't mark
    them ordered -- confirms ordered-field handling didn't regress plain select fields."""
    notes = field_by_key["custom:notes"]
    contact_with = make_contact(custom_fields={"notes": "Met at a conference"})
    contact_without = make_contact()
    eq_condition = FilterCondition(field=notes.key, operator="eq", value="Met at a conference")
    empty_condition = FilterCondition(field=notes.key, operator="is_empty")
    assert evaluate_condition(contact_with, eq_condition, notes) is True
    assert evaluate_condition(contact_without, eq_condition, notes) is False
    assert evaluate_condition(contact_without, empty_condition, notes) is True


def test_ordinal_gte_on_list_field_matches_if_any_selected_value_qualifies(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1k - $10k", "$5M - $10M"]})
    condition = FilterCondition(field=cs.key, operator="gte", value="$1M - $2M")
    assert evaluate_condition(contact, condition, cs) is True  # $5M-$10M qualifies


def test_ordinal_gte_on_list_field_with_only_other_does_not_match(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["Other:"]})
    condition = FilterCondition(field=cs.key, operator="gte", value="$1M - $2M")
    assert evaluate_condition(contact, condition, cs) is False


# --- Same-field ordinal range grouping (gte + lte on ONE field = one selected
# option must satisfy BOTH bounds, not two different options each satisfying
# one) -- the fix for the false-positive found during the pre-deployment audit ---


def test_range_matches_single_bucket_within_both_bounds(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1M - $2M"]})
    query = FilterQuery(
        filters=[
            FilterCondition(field=cs.key, operator="gte", value="$500k - $1M"),
            FilterCondition(field=cs.key, operator="lte", value="$2M - $5M"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is True


def test_range_does_not_match_bimodal_selection_satisfying_bounds_separately(field_by_key):
    """The exact false-positive confirmed live during the audit: nothing in the
    selected list actually falls between the two bounds, even though one
    selected value clears the lower bound and a DIFFERENT one clears the upper."""
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1k - $10k", "$5M - $10M"]})
    query = FilterQuery(
        filters=[
            FilterCondition(field=cs.key, operator="gte", value="$500k - $1M"),
            FilterCondition(field=cs.key, operator="lte", value="$2M - $5M"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is False


def test_range_matches_when_one_of_several_selected_values_satisfies_both_bounds(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1k - $10k", "$1M - $2M", "$10M+"]})
    query = FilterQuery(
        filters=[
            FilterCondition(field=cs.key, operator="gte", value="$500k - $1M"),
            FilterCondition(field=cs.key, operator="lte", value="$2M - $5M"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is True


def test_range_only_gte_preserves_existing_any_match_behavior(field_by_key):
    """A single ordinal condition on a field is NOT grouped -- unchanged
    any-selected-value-qualifies semantics."""
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1k - $10k", "$5M - $10M"]})
    query = FilterQuery(filters=[FilterCondition(field=cs.key, operator="gte", value="$1M - $2M")])
    assert matches_query(contact, query, field_by_key) is True


def test_range_only_lte_preserves_existing_any_match_behavior(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["$1k - $10k", "$5M - $10M"]})
    query = FilterQuery(filters=[FilterCondition(field=cs.key, operator="lte", value="$100k - $250k")])
    assert matches_query(contact, query, field_by_key) is True


def test_range_grouping_still_excludes_other_as_non_ordinal(field_by_key):
    cs = field_by_key["custom:check_size_personal"]
    contact = make_contact(custom_fields={"check_size_personal": ["Other:"]})
    query = FilterQuery(
        filters=[
            FilterCondition(field=cs.key, operator="gte", value="$500k - $1M"),
            FilterCondition(field=cs.key, operator="lte", value="$2M - $5M"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is False


def test_range_grouping_on_single_select_ordered_field_is_a_no_op(field_by_key):
    """Age Range has only one current value -- grouping two ordinal conditions on
    it is mathematically identical to evaluating them independently, so this
    must behave exactly like it did before the fix."""
    ar = field_by_key["custom:age_range"]
    contact = make_contact(custom_fields={"age_range": "41-50"})
    query = FilterQuery(
        filters=[
            FilterCondition(field=ar.key, operator="gte", value="31-40"),
            FilterCondition(field=ar.key, operator="lte", value="51-60"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is True

    out_of_range = make_contact(custom_fields={"age_range": "71-80"})
    assert matches_query(out_of_range, query, field_by_key) is False


def test_range_grouping_retired_deceased_remain_non_ordinal(field_by_key):
    ar = field_by_key["custom:age_range"]
    contact = make_contact(custom_fields={"age_range": "Retired"})
    query = FilterQuery(
        filters=[
            FilterCondition(field=ar.key, operator="gte", value="31-40"),
            FilterCondition(field=ar.key, operator="lte", value="51-60"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is False


def test_revenue_stage_ordinal_behavior_unchanged_by_grouping_fix(field_by_key):
    """Revenue Stage (single-select, ordered, no non-ordered outliers) --
    confirms the grouping fix didn't regress a field with no outliers at all."""
    rs = field_by_key["custom:revenue_stage"]
    contact = make_contact(custom_fields={"revenue_stage": "$1M - $10M"})
    query = FilterQuery(
        filters=[
            FilterCondition(field=rs.key, operator="gte", value="$500k - $1M"),
            FilterCondition(field=rs.key, operator="lte", value="$10M - $100M"),
        ],
        logic="AND",
    )
    assert matches_query(contact, query, field_by_key) is True


# --- created_at / updated_at (new DATE core fields -- no schema/migration change,
# these are already-required, always-populated datetime fields on CrmContact) ---


def test_created_at_on_or_after_and_on_or_before_use_real_datetime_without_type_error(field_by_key):
    """The exact bug this guards against: CrmContact.created_at is a real
    `datetime`, and comparing it against a frontend date-input string (parsed as a
    plain `date`) used to raise `TypeError: can't compare datetime.datetime to
    datetime.date` because `_parse_date` checked `isinstance(raw, date)` before
    `isinstance(raw, datetime)` -- `datetime` is a `date` subclass, so the datetime
    branch was never reached and a full datetime was returned unchanged."""
    ca = field_by_key["created_at"]
    contact = CrmContact(crm_contact_id="c1", created_at=datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc), updated_at=datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc))
    on_or_after = FilterQuery(filters=[FilterCondition(field=ca.key, operator="on_or_after", value="2026-06-01")])
    on_or_before = FilterQuery(filters=[FilterCondition(field=ca.key, operator="on_or_before", value="2026-06-30")])
    too_late = FilterQuery(filters=[FilterCondition(field=ca.key, operator="on_or_after", value="2026-07-01")])
    assert matches_query(contact, on_or_after, field_by_key) is True
    assert matches_query(contact, on_or_before, field_by_key) is True
    assert matches_query(contact, too_late, field_by_key) is False


def test_updated_at_before_and_after_use_real_datetime_without_type_error(field_by_key):
    ua = field_by_key["updated_at"]
    contact = CrmContact(crm_contact_id="c1", created_at=datetime(2026, 6, 15, tzinfo=timezone.utc), updated_at=datetime(2026, 6, 15, tzinfo=timezone.utc))
    before = FilterQuery(filters=[FilterCondition(field=ua.key, operator="before", value="2026-06-16")])
    after = FilterQuery(filters=[FilterCondition(field=ua.key, operator="after", value="2026-06-14")])
    not_before = FilterQuery(filters=[FilterCondition(field=ua.key, operator="before", value="2026-06-15")])
    assert matches_query(contact, before, field_by_key) is True
    assert matches_query(contact, after, field_by_key) is True
    assert matches_query(contact, not_before, field_by_key) is False  # same day is not "before"


def test_created_at_is_always_populated_never_empty(field_by_key):
    """created_at/updated_at are required, non-nullable fields -- is_empty must
    always be False and is_not_empty always True for a real contact."""
    ca = field_by_key["created_at"]
    contact = make_contact()
    assert matches_query(contact, FilterQuery(filters=[FilterCondition(field=ca.key, operator="is_empty")]), field_by_key) is False
    assert matches_query(contact, FilterQuery(filters=[FilterCondition(field=ca.key, operator="is_not_empty")]), field_by_key) is True


# --- Text operators ---


@pytest.mark.parametrize(
    "operator,value,contact_city,expected",
    [
        ("eq", "Austin", "Austin", True),
        ("eq", "Austin", "Dallas", False),
        ("neq", "Austin", "Dallas", True),
        ("neq", "Austin", "Austin", False),
        ("contains", "Aus", "Austin", True),
        ("not_contains", "Dal", "Austin", True),
        ("not_contains", "Aus", "Austin", False),
    ],
)
def test_text_operators(field_by_key, operator, value, contact_city, expected):
    city = field_by_key["city"]
    contact = make_contact(city=contact_city)
    condition = FilterCondition(field="city", operator=operator, value=value)
    assert evaluate_condition(contact, condition, city) is expected


def test_text_eq_is_case_insensitive(field_by_key):
    city = field_by_key["city"]
    contact = make_contact(city="AUSTIN")
    condition = FilterCondition(field="city", operator="eq", value="austin")
    assert evaluate_condition(contact, condition, city) is True


def test_text_multi_value_eq_is_or(field_by_key):
    """'State is Texas or California' -- multi-value 'eq' expresses the OR-within-one-
    field mechanism the frontend uses instead of a separate is_any_of operator."""
    state = field_by_key["state"]
    condition = FilterCondition(field="state", operator="eq", value=["Texas", "California"])
    assert evaluate_condition(make_contact(state="Texas"), condition, state) is True
    assert evaluate_condition(make_contact(state="California"), condition, state) is True
    assert evaluate_condition(make_contact(state="Oregon"), condition, state) is False


# --- Missing/empty scalar values never satisfy a comparison operator ---


def test_missing_text_value_never_satisfies_neq(field_by_key):
    """A contact with NO city at all must not be treated as 'not equal to Texas' --
    unknown is not the same as confirmed different."""
    city = field_by_key["city"]
    contact = make_contact(city=None)
    condition = FilterCondition(field="city", operator="neq", value="Austin")
    assert evaluate_condition(contact, condition, city) is False


def test_missing_text_value_never_satisfies_not_contains(field_by_key):
    city = field_by_key["city"]
    contact = make_contact(city=None)
    condition = FilterCondition(field="city", operator="not_contains", value="Aus")
    assert evaluate_condition(contact, condition, city) is False


def test_missing_number_value_never_satisfies_any_comparison(field_by_key):
    tf = field_by_key["custom:total_funding"]
    contact = make_contact()  # no total_funding set at all
    for operator, value in [("eq", 5), ("neq", 5), ("gt", 0), ("gte", 0), ("lt", 100), ("lte", 100)]:
        condition = FilterCondition(field=tf.key, operator=operator, value=value)
        assert evaluate_condition(contact, condition, tf) is False, f"{operator} should not match missing data"


def test_missing_value_satisfies_is_empty(field_by_key):
    city = field_by_key["city"]
    condition = FilterCondition(field="city", operator="is_empty")
    assert evaluate_condition(make_contact(city=None), condition, city) is True
    assert evaluate_condition(make_contact(city=""), condition, city) is True
    assert evaluate_condition(make_contact(city="Austin"), condition, city) is False


# --- Number operators ---


@pytest.mark.parametrize(
    "operator,value,contact_value,expected",
    [
        ("eq", 100, 100, True),
        ("eq", 100, 200, False),
        ("neq", 100, 200, True),
        ("gt", 100, 200, True),
        ("gt", 100, 100, False),
        ("gte", 100, 100, True),
        ("lt", 100, 50, True),
        ("lte", 100, 100, True),
    ],
)
def test_number_operators(field_by_key, operator, value, contact_value, expected):
    tf = field_by_key["custom:total_funding"]
    contact = make_contact(custom_fields={"total_funding": contact_value})
    condition = FilterCondition(field=tf.key, operator=operator, value=value)
    assert evaluate_condition(contact, condition, tf) is expected


# --- Boolean operators ---


def test_boolean_is_true_and_is_false(field_by_key):
    dnc = field_by_key["custom:do_not_call"]
    contact_true = make_contact(custom_fields={"do_not_call": True})
    contact_false = make_contact(custom_fields={"do_not_call": False})
    contact_unset = make_contact()
    is_true = FilterCondition(field=dnc.key, operator="is_true")
    is_false = FilterCondition(field=dnc.key, operator="is_false")
    assert evaluate_condition(contact_true, is_true, dnc) is True
    assert evaluate_condition(contact_false, is_true, dnc) is False
    assert evaluate_condition(contact_false, is_false, dnc) is True
    assert evaluate_condition(contact_unset, is_true, dnc) is False
    assert evaluate_condition(contact_unset, is_false, dnc) is False  # unset is neither true nor false


def test_boolean_is_empty_distinguishes_unset_from_false(field_by_key):
    dnc = field_by_key["custom:do_not_call"]
    empty = FilterCondition(field=dnc.key, operator="is_empty")
    assert evaluate_condition(make_contact(), empty, dnc) is True  # never set
    assert evaluate_condition(make_contact(custom_fields={"do_not_call": False}), empty, dnc) is False  # False is a real value


# --- Multi-select operators (contains_any / contains_all / not_contains) ---


def test_contains_any(field_by_key):
    it = field_by_key["custom:investor_type"]
    contact = make_contact(custom_fields={"investor_type": ["Angel Investor", "Family Office"]})
    condition = FilterCondition(field=it.key, operator="contains_any", value=["Family Office", "Venture Capital"])
    assert evaluate_condition(contact, condition, it) is True


def test_contains_all(field_by_key):
    it = field_by_key["custom:investor_type"]
    contact = make_contact(custom_fields={"investor_type": ["Angel Investor", "Family Office"]})
    matches = FilterCondition(field=it.key, operator="contains_all", value=["Angel Investor", "Family Office"])
    misses = FilterCondition(field=it.key, operator="contains_all", value=["Angel Investor", "Venture Capital"])
    assert evaluate_condition(contact, matches, it) is True
    assert evaluate_condition(contact, misses, it) is False


def test_multi_select_not_contains(field_by_key):
    it = field_by_key["custom:investor_type"]
    contact = make_contact(custom_fields={"investor_type": ["Angel Investor"]})
    condition = FilterCondition(field=it.key, operator="not_contains", value=["Venture Capital"])
    assert evaluate_condition(contact, condition, it) is True


def test_multi_select_is_empty_vs_none_literal_value():
    """thesis_dietary_preferences = None (the LITERAL string option) is a real,
    non-empty selection, distinct from the list being genuinely empty ([])."""
    registry = build_registry([])
    field_by_key = {f.key: f for f in registry}
    dietary = field_by_key["thesis_dietary_preferences"]

    contact_literal_none = make_contact(thesis_dietary_preferences=["None"])
    contact_truly_empty = make_contact(thesis_dietary_preferences=[])

    is_empty = FilterCondition(field=dietary.key, operator="is_empty")
    assert evaluate_condition(contact_literal_none, is_empty, dietary) is False
    assert evaluate_condition(contact_truly_empty, is_empty, dietary) is True

    eq_none_literal = FilterCondition(field=dietary.key, operator="contains_any", value=["None"])
    assert evaluate_condition(contact_literal_none, eq_none_literal, dietary) is True
    assert evaluate_condition(contact_truly_empty, eq_none_literal, dietary) is False


def test_missing_custom_multi_select_field_is_empty(field_by_key):
    """A custom multi-select field never set at all (key absent from custom_fields)
    must behave identically to an explicitly-empty list."""
    it = field_by_key["custom:investor_type"]
    contact = make_contact()  # custom_fields = {} entirely
    is_empty = FilterCondition(field=it.key, operator="is_empty")
    contains_any = FilterCondition(field=it.key, operator="contains_any", value=["Angel Investor"])
    assert evaluate_condition(contact, is_empty, it) is True
    assert evaluate_condition(contact, contains_any, it) is False


# --- AND / OR logic across multiple conditions ---


def test_and_logic_requires_every_condition(field_by_key):
    query = FilterQuery(
        filters=[FilterCondition(field="state", operator="eq", value="Texas"), FilterCondition(field="city", operator="eq", value="Austin")],
        logic="AND",
    )
    match = make_contact(state="Texas", city="Austin")
    partial = make_contact(state="Texas", city="Dallas")
    assert matches_query(match, query, field_by_key) is True
    assert matches_query(partial, query, field_by_key) is False


def test_or_logic_requires_any_condition(field_by_key):
    query = FilterQuery(
        filters=[FilterCondition(field="state", operator="eq", value="Texas"), FilterCondition(field="state", operator="eq", value="California")],
        logic="OR",
    )
    assert matches_query(make_contact(state="Texas"), query, field_by_key) is True
    assert matches_query(make_contact(state="California"), query, field_by_key) is True
    assert matches_query(make_contact(state="Oregon"), query, field_by_key) is False


def test_empty_filter_list_matches_everything(field_by_key):
    query = FilterQuery(filters=[])
    assert matches_query(make_contact(), query, field_by_key) is True


# --- Core + custom field combined in one query ---


def test_core_and_custom_field_combined_with_and(field_by_key):
    query = FilterQuery(
        filters=[
            FilterCondition(field="state", operator="eq", value="Texas"),
            FilterCondition(field="custom:investor_type", operator="contains_any", value=["Family Office"]),
        ],
        logic="AND",
    )
    match = make_contact(state="Texas", custom_fields={"investor_type": ["Family Office"]})
    wrong_state = make_contact(state="California", custom_fields={"investor_type": ["Family Office"]})
    assert matches_query(match, query, field_by_key) is True
    assert matches_query(wrong_state, query, field_by_key) is False


# --- Sorting ---


def test_sort_key_puts_empty_values_last_regardless_of_direction(field_by_key):
    city = field_by_key["city"]
    named = make_contact(crm_contact_id="a", city="Austin")
    empty = make_contact(crm_contact_id="b", city=None)
    assert sort_key(named, city) == (False, "austin")
    assert sort_key(empty, city) == (True, "")


# --- query_contacts: pagination, sorting, zero results, archived ---


def test_query_contacts_paginates(field_by_key):
    contacts = [make_contact(crm_contact_id=str(i), city="Austin") for i in range(5)]
    query = FilterQuery(filters=[], page=2, page_size=2)
    page = query_contacts(contacts, query, field_by_key)
    assert page.total == 5
    assert page.page == 2
    assert page.page_size == 2
    assert len(page.items) == 2


def test_query_contacts_sorts_ascending_and_descending(field_by_key):
    contacts = [make_contact(crm_contact_id="a", last_name="Zephyr"), make_contact(crm_contact_id="b", last_name="Adams")]
    asc = query_contacts(contacts, FilterQuery(sort=FilterSort(field="last_name", direction="asc")), field_by_key)
    desc = query_contacts(contacts, FilterQuery(sort=FilterSort(field="last_name", direction="desc")), field_by_key)
    assert [c.crm_contact_id for c in asc.items] == ["b", "a"]
    assert [c.crm_contact_id for c in desc.items] == ["a", "b"]


def test_query_contacts_sorts_by_company_ascending_and_descending(field_by_key):
    contacts = [make_contact(crm_contact_id="a", company="Zenith Capital"), make_contact(crm_contact_id="b", company="Acme Ventures")]
    asc = query_contacts(contacts, FilterQuery(sort=FilterSort(field="company", direction="asc")), field_by_key)
    desc = query_contacts(contacts, FilterQuery(sort=FilterSort(field="company", direction="desc")), field_by_key)
    assert [c.crm_contact_id for c in asc.items] == ["b", "a"]
    assert [c.crm_contact_id for c in desc.items] == ["a", "b"]


def test_query_contacts_sort_combined_with_filters(field_by_key):
    """Sorting must operate on the FILTERED set, not the full unfiltered list --
    a contact excluded by the filter must never appear in sorted output."""
    contacts = [
        make_contact(crm_contact_id="a", state="Texas", last_name="Zephyr"),
        make_contact(crm_contact_id="b", state="Texas", last_name="Adams"),
        make_contact(crm_contact_id="c", state="California", last_name="Aaronson"),
    ]
    query = FilterQuery(
        filters=[FilterCondition(field="state", operator="eq", value="Texas")],
        sort=FilterSort(field="last_name", direction="asc"),
    )
    page = query_contacts(contacts, query, field_by_key)
    assert [c.crm_contact_id for c in page.items] == ["b", "a"]  # "c" (California) never appears


def test_query_contacts_sorts_before_paginating_not_after(field_by_key):
    """The page returned for page=2 must be the second slice of the SORTED list --
    if sorting happened after pagination (i.e. only within an already-cut page),
    this would come back in a different, wrong order."""
    names = ["Mona", "Elle", "Zed", "Ada", "Kay", "Bo"]
    contacts = [make_contact(crm_contact_id=str(i), last_name=name) for i, name in enumerate(names)]
    query = FilterQuery(sort=FilterSort(field="last_name", direction="asc"), page=2, page_size=2)
    page = query_contacts(contacts, query, field_by_key)
    full_sorted_order = sorted(names)
    expected_names_on_page_2 = full_sorted_order[2:4]
    assert [c.last_name for c in page.items] == expected_names_on_page_2


def test_query_contacts_sort_keeps_empty_last_even_descending(field_by_key):
    contacts = [make_contact(crm_contact_id="a", last_name="Zephyr"), make_contact(crm_contact_id="b", last_name=None)]
    desc = query_contacts(contacts, FilterQuery(sort=FilterSort(field="last_name", direction="desc")), field_by_key)
    assert [c.crm_contact_id for c in desc.items] == ["a", "b"]  # empty-last, not first


def test_query_contacts_zero_results(field_by_key):
    contacts = [make_contact(city="Austin")]
    query = FilterQuery(filters=[FilterCondition(field="city", operator="eq", value="Nowhere")])
    page = query_contacts(contacts, query, field_by_key)
    assert page.total == 0
    assert page.items == []


def test_query_contacts_excludes_archived_by_default(field_by_key):
    contacts = [make_contact(crm_contact_id="a", archived=False), make_contact(crm_contact_id="b", archived=True)]
    page = query_contacts(contacts, FilterQuery(), field_by_key)
    assert page.total == 1
    assert page.items[0].crm_contact_id == "a"


def test_query_contacts_includes_archived_when_requested(field_by_key):
    contacts = [make_contact(crm_contact_id="a", archived=False), make_contact(crm_contact_id="b", archived=True)]
    page = query_contacts(contacts, FilterQuery(include_archived=True), field_by_key)
    assert page.total == 2


def test_query_contacts_large_result_set_still_paginates_correctly(field_by_key):
    contacts = [make_contact(crm_contact_id=str(i), state="Texas") for i in range(250)]
    query = FilterQuery(filters=[FilterCondition(field="state", operator="eq", value="Texas")], page=5, page_size=50)
    page = query_contacts(contacts, query, field_by_key)
    assert page.total == 250
    assert len(page.items) == 50
    assert page.items[0].crm_contact_id == "200"
