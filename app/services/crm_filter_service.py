"""
More Filters: the dynamic field registry + validation + predicate engine behind
GET /crm/filterable-fields and POST /crm/contacts/query.

Architecture (see the audit/plan conversation this implements):
  - The registry is the single source of truth the frontend builds its entire
    filter UI from -- field list, categories, operator choices, and select
    options all come from here, never hardcoded a second time in the frontend.
  - Field TYPE here is richer than get_contact_export_fields()'s scalar/list/
    boolean: it's what determines which operators a field exposes, and none of
    the core/thesis fields are actually enum-enforced by Pydantic (thesis_investor_mode
    is a plain `str`), so this classification is necessarily a small, explicit,
    hand-maintained list -- same "one small named registry is the source of truth"
    pattern as EXTERNAL_FIELD_NAMES/THESIS_FIELD_NAMES in app/models/crm.py.
  - Ordinal (gt/gte/lt/lte) operators are ONLY exposed for fields explicitly marked
    `ordered=True`, and only ever compare against that field's own `ordered_options`
    list -- never inferred from the option strings themselves. A value outside
    `ordered_options` (e.g. Check Size's "Other:", Age Range's "Retired"/"Deceased")
    is a completely valid value for every other operator, but is rejected outright
    if used with an ordinal operator, and never silently assigned a guessed position.
  - Filtering itself is a Python predicate pass over CrmContactStore.list() -- the
    same approach list_contacts() (untouched, still backs GET /crm/contacts) already
    uses; this is additive, not a rewrite of the existing filter path.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.models.crm import (
    ASSET_TYPE_OPTIONS,
    BUSINESS_MODEL_OPTIONS,
    DEAL_STAGE_OPTIONS,
    DEMOGRAPHIC_PREFERENCE_OPTIONS,
    DIETARY_PREFERENCE_OPTIONS,
    INDUSTRY_OPTIONS,
    INVESTOR_MODE_OPTIONS,
    MEETING_PREFERENCE_OPTIONS,
    CrmContact,
    CrmContactPage,
    CrmCustomFieldDefinition,
    CustomFieldType,
    FilterCondition,
    FilterFieldMeta,
    FilterFieldType,
    FilterQuery,
)
from app.services.crm_service import _is_empty

CUSTOM_FIELD_PREFIX = "custom:"


class FilterValidationError(ValueError):
    """Raised for any unrecognized field, disallowed operator, or malformed value --
    always caught by the API route and turned into a 400, never allowed to reach
    the store layer or be interpreted as a query fragment."""


# --- Operator vocabulary, base set per field type (ordinal ops are added
# separately, only for fields explicitly marked ordered=True) ---

_BASE_OPERATORS: dict[FilterFieldType, list[str]] = {
    FilterFieldType.TEXT: ["eq", "neq", "contains", "not_contains", "is_empty", "is_not_empty"],
    FilterFieldType.NUMBER: ["eq", "neq", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty"],
    FilterFieldType.BOOLEAN: ["is_true", "is_false", "is_empty", "is_not_empty"],
    FilterFieldType.DATE: ["eq", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty"],
    FilterFieldType.SINGLE_SELECT: ["eq", "neq", "is_empty", "is_not_empty"],
    FilterFieldType.MULTI_SELECT: ["contains_any", "contains_all", "not_contains", "is_empty", "is_not_empty"],
}

_ORDINAL_OPERATORS = ["gt", "gte", "lt", "lte"]

_NO_VALUE_OPERATORS = {"is_empty", "is_not_empty", "is_true", "is_false"}


def _operators_for(field_type: FilterFieldType, ordered: bool) -> list[str]:
    ops = list(_BASE_OPERATORS[field_type])
    if ordered and field_type in (FilterFieldType.SINGLE_SELECT, FilterFieldType.MULTI_SELECT):
        ops.extend(_ORDINAL_OPERATORS)
    return ops


# --- Core/thesis field registry -- (key, label, category, type, options) ---
# storage_shape is derived (MULTI_SELECT -> "list", everything else -> "scalar"),
# never hand-specified, since it's fully determined by `type` here.
#
# Deliberately EXCLUDED: thesis_private_check_sizes/thesis_institutional_check_sizes
# (+ their _other companions) -- deprecated since the 2026-08-06 Check Size
# consolidation, zero real data, canonical replacement is the check_size_personal/
# check_size_institutional CUSTOM fields below. Also excluded:
# thesis_investor_mode_manual_override (an internal automation flag, not a
# prospecting/segmentation attribute).

_CONTACT = "Contact"
_COMPANY = "Company"
_THESIS = "Investor / Thesis"
_CUSTOM = "Custom Fields"

_CORE_FIELDS: list[tuple[str, str, str, FilterFieldType, list[str]]] = [
    # Already existing, always-populated datetime fields on CrmContact -- no schema
    # or migration change needed to expose these, just registry entries using the
    # same DATE type/operator machinery every other date field already uses.
    ("created_at", "Created At", _CONTACT, FilterFieldType.DATE, []),
    ("updated_at", "Updated At", _CONTACT, FilterFieldType.DATE, []),
    ("first_name", "First Name", _CONTACT, FilterFieldType.TEXT, []),
    ("last_name", "Last Name", _CONTACT, FilterFieldType.TEXT, []),
    ("email", "Email", _CONTACT, FilterFieldType.TEXT, []),
    ("email_status", "Email Status", _CONTACT, FilterFieldType.TEXT, []),
    ("phone", "Phone", _CONTACT, FilterFieldType.TEXT, []),
    ("linkedin_url", "LinkedIn URL", _CONTACT, FilterFieldType.TEXT, []),
    ("title", "Title", _CONTACT, FilterFieldType.TEXT, []),
    ("apollo_contact_id", "Apollo Contact ID", _CONTACT, FilterFieldType.TEXT, []),
    ("city", "City", _CONTACT, FilterFieldType.TEXT, []),
    ("state", "State", _CONTACT, FilterFieldType.TEXT, []),
    ("country", "Country", _CONTACT, FilterFieldType.TEXT, []),
    ("company", "Company", _COMPANY, FilterFieldType.TEXT, []),
    ("company_website", "Company Website", _COMPANY, FilterFieldType.TEXT, []),
    ("industry", "Industry", _COMPANY, FilterFieldType.TEXT, []),
    ("company_size", "Company Size", _COMPANY, FilterFieldType.TEXT, []),
    ("revenue", "Revenue", _COMPANY, FilterFieldType.TEXT, []),
    ("funding_stage", "Funding Stage", _COMPANY, FilterFieldType.TEXT, []),
    ("funding_amount", "Funding Amount", _COMPANY, FilterFieldType.TEXT, []),
    ("seniority", "Seniority", _COMPANY, FilterFieldType.TEXT, []),
    ("department", "Department", _COMPANY, FilterFieldType.TEXT, []),
    ("job_function", "Job Function", _COMPANY, FilterFieldType.TEXT, []),
    ("technologies", "Technologies", _COMPANY, FilterFieldType.MULTI_SELECT, []),
    ("thesis_cities", "Cities (Investor Thesis)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_investor_mode", "Investor Mode", _THESIS, FilterFieldType.SINGLE_SELECT, INVESTOR_MODE_OPTIONS),
    ("thesis_also_invests_institutionally", "Also Invests Institutionally", _THESIS, FilterFieldType.BOOLEAN, []),
    ("thesis_private_asset_types", "Private Asset Types", _THESIS, FilterFieldType.MULTI_SELECT, ASSET_TYPE_OPTIONS),
    ("thesis_private_asset_types_other", "Private Asset Types (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_business_models", "Private Business Models", _THESIS, FilterFieldType.MULTI_SELECT, BUSINESS_MODEL_OPTIONS),
    ("thesis_private_business_models_other", "Private Business Models (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_industries", "Private Industries", _THESIS, FilterFieldType.MULTI_SELECT, INDUSTRY_OPTIONS),
    ("thesis_private_industries_other", "Private Industries (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_deal_stages", "Private Deal Stages", _THESIS, FilterFieldType.MULTI_SELECT, DEAL_STAGE_OPTIONS),
    ("thesis_private_deal_stages_other", "Private Deal Stages (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_meeting_preferences", "Private Meeting Preferences", _THESIS, FilterFieldType.MULTI_SELECT, MEETING_PREFERENCE_OPTIONS),
    ("thesis_private_meeting_preferences_other", "Private Meeting Preferences (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_demographic_preferences", "Private Demographic Preferences", _THESIS, FilterFieldType.MULTI_SELECT, DEMOGRAPHIC_PREFERENCE_OPTIONS),
    ("thesis_private_demographic_preferences_other", "Private Demographic Preferences (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_private_other_criteria", "Private Other Criteria", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_asset_types", "Institutional Asset Types", _THESIS, FilterFieldType.MULTI_SELECT, ASSET_TYPE_OPTIONS),
    ("thesis_institutional_asset_types_other", "Institutional Asset Types (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_business_models", "Institutional Business Models", _THESIS, FilterFieldType.MULTI_SELECT, BUSINESS_MODEL_OPTIONS),
    ("thesis_institutional_business_models_other", "Institutional Business Models (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_industries", "Institutional Industries", _THESIS, FilterFieldType.MULTI_SELECT, INDUSTRY_OPTIONS),
    ("thesis_institutional_industries_other", "Institutional Industries (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_deal_stages", "Institutional Deal Stages", _THESIS, FilterFieldType.MULTI_SELECT, DEAL_STAGE_OPTIONS),
    ("thesis_institutional_deal_stages_other", "Institutional Deal Stages (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_meeting_preferences", "Institutional Meeting Preferences", _THESIS, FilterFieldType.MULTI_SELECT, MEETING_PREFERENCE_OPTIONS),
    ("thesis_institutional_meeting_preferences_other", "Institutional Meeting Preferences (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_demographic_preferences", "Institutional Demographic Preferences", _THESIS, FilterFieldType.MULTI_SELECT, DEMOGRAPHIC_PREFERENCE_OPTIONS),
    ("thesis_institutional_demographic_preferences_other", "Institutional Demographic Preferences (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_institutional_other_criteria", "Institutional Other Criteria", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_dietary_preferences", "Dietary Preferences", _THESIS, FilterFieldType.MULTI_SELECT, DIETARY_PREFERENCE_OPTIONS),
    ("thesis_dietary_preferences_other", "Dietary Preferences (Other)", _THESIS, FilterFieldType.TEXT, []),
    ("thesis_referral_emails", "Referral Emails", _THESIS, FilterFieldType.TEXT, []),
]

# Deal Stage (private + institutional) is deliberately NOT marked ordered: its option
# list mixes a real stage progression (Friends & Family -> ... -> Pre-IPO) with "Fund LP"
# (an investment vehicle, not a stage) and "Secondary" (a transaction type, not a stage) --
# there is no principled position for either without guessing. Confirmed with the user
# 2026-08-07; default (unordered) stands until an explicit order is provided.

# field_key -> conceptual category, for the custom fields that clearly belong somewhere
# more specific than a generic "Custom Fields" bucket. Anything NOT listed here defaults
# to _CUSTOM. This mapping is inherently a judgment call (there's no schema-derivable
# "category" for a custom field) -- called out explicitly in the implementation report.
_CUSTOM_FIELD_CATEGORY_OVERRIDES: dict[str, str] = {
    "gender": _CONTACT,
    "age_range": _CONTACT,
    "role": _CONTACT,
    "do_not_call": _CONTACT,
    "secondary_email": _CONTACT,
    "work_direct_phone": _CONTACT,
    "corporate_phone": _CONTACT,
    "accredited_status": _THESIS,
    "investor_type": _THESIS,
    "how_early_do_you_invest": _THESIS,
    "how_often_do_you_invest": _THESIS,
    "do_not_invest_in": _THESIS,
    "investment_geography_preference": _THESIS,
    "check_size_personal": _THESIS,
    "check_size_institutional": _THESIS,
    "investment_industry": _THESIS,
    "revenue_stage": _COMPANY,
    "last_raised_at": _COMPANY,
    "total_funding": _COMPANY,
}

# field_key -> explicit ascending order. Any option NOT in this list (e.g. Check Size's
# "Other:", Age Range's "Retired"/"Deceased") is a non_ordered_option: valid for every
# other operator, but never assigned a guessed position, and rejected outright if used
# with an ordinal operator. Confirmed against live production custom-field options
# 2026-08-07 -- not inferred from the strings themselves.
_ORDERED_OPTIONS: dict[str, list[str]] = {
    "check_size_personal": [
        "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
        "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+",
    ],
    "check_size_institutional": [
        "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
        "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+",
    ],
    "revenue_stage": ["$250K - $500K", "$500k - $1M", "$1M - $10M", "$10M - $100M"],
    "age_range": ["18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+"],
}

_CUSTOM_TYPE_TO_FILTER_TYPE: dict[CustomFieldType, FilterFieldType] = {
    CustomFieldType.TEXT: FilterFieldType.TEXT,
    CustomFieldType.LONG_TEXT: FilterFieldType.TEXT,
    CustomFieldType.NUMBER: FilterFieldType.NUMBER,
    CustomFieldType.DATE: FilterFieldType.DATE,
    CustomFieldType.BOOLEAN: FilterFieldType.BOOLEAN,
    CustomFieldType.SINGLE_SELECT: FilterFieldType.SINGLE_SELECT,
    CustomFieldType.MULTI_SELECT: FilterFieldType.MULTI_SELECT,
}


def _make_field_meta(
    key: str,
    label: str,
    category: str,
    field_type: FilterFieldType,
    options: list[str],
    source: str,
    lookup_key: str | None = None,
) -> FilterFieldMeta:
    """`lookup_key` is the BARE key _ORDERED_OPTIONS is keyed by -- for custom fields
    this is `definition.field_key` without the `custom:` prefix that ends up on the
    final registry `key`, so the two must be passed separately rather than derived
    from each other."""
    ordered_options = _ORDERED_OPTIONS.get(lookup_key if lookup_key is not None else key, [])
    ordered = bool(ordered_options)
    non_ordered_options = [o for o in options if o not in ordered_options] if ordered else []
    return FilterFieldMeta(
        key=key,
        label=label,
        category=category,
        type=field_type,
        storage_shape="list" if field_type == FilterFieldType.MULTI_SELECT else "scalar",
        source=source,
        options=options,
        ordered=ordered,
        ordered_options=ordered_options,
        non_ordered_options=non_ordered_options,
        operators=_operators_for(field_type, ordered),
    )


def build_registry(custom_fields: list[CrmCustomFieldDefinition]) -> list[FilterFieldMeta]:
    """The complete, merged filterable-field list -- core/thesis fields (hand-registered
    above) plus every ACTIVE custom field (from the live definitions the caller already
    fetched), normalized to one shape. Inactive custom fields (e.g. the deprecated
    sub_industry) are excluded, matching the existing custom-field-admin convention."""
    registry = [_make_field_meta(key, label, category, field_type, options, "thesis" if key.startswith("thesis_") else "core")
                for key, label, category, field_type, options in _CORE_FIELDS]
    for definition in custom_fields:
        if not definition.active:
            continue
        field_type = _CUSTOM_TYPE_TO_FILTER_TYPE[definition.field_type]
        category = _CUSTOM_FIELD_CATEGORY_OVERRIDES.get(definition.field_key, _CUSTOM)
        registry.append(
            _make_field_meta(
                f"{CUSTOM_FIELD_PREFIX}{definition.field_key}",
                definition.label,
                category,
                field_type,
                definition.options,
                "custom",
                lookup_key=definition.field_key,
            )
        )
    return registry


# --- Validation ---


def _parse_number(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise FilterValidationError(f"Not a valid number: {raw!r}")


def _parse_date(raw: Any) -> date:
    # `datetime` must be checked before `date` -- datetime IS a date subclass,
    # so the reversed order would return a full datetime unchanged here
    # (e.g. CrmContact.created_at, a real datetime) and later comparisons
    # against a plain `date` (from a frontend date-input string) would raise
    # `TypeError: can't compare datetime.datetime to datetime.date`.
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        raise FilterValidationError(f"Not a valid ISO date: {raw!r}")


def _as_value_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def validate_condition(condition: FilterCondition, field: FilterFieldMeta) -> None:
    if condition.operator not in field.operators:
        raise FilterValidationError(f"Operator '{condition.operator}' is not allowed for field '{field.key}'")

    if condition.operator in _NO_VALUE_OPERATORS:
        return  # value is ignored regardless of what was sent

    values = _as_value_list(condition.value)
    if not values:
        raise FilterValidationError(f"Field '{field.key}' with operator '{condition.operator}' requires a value")

    # gt/gte/lt/lte mean two different things depending on field type: a plain
    # numeric/date comparison (NUMBER/DATE fields, validated below like any other
    # value), or -- only for a select-type field explicitly marked `ordered` -- a
    # position comparison against that field's own `ordered_options`. The `field.
    # ordered` check is what disambiguates them; `_operators_for` only ever adds
    # these operator names to a select field's list when `ordered` is True, so a
    # NUMBER field reaching this branch is never possible.
    if field.ordered and condition.operator in _ORDINAL_OPERATORS:
        if len(values) != 1:
            raise FilterValidationError("Ordinal operators (gt/gte/lt/lte) take exactly one value")
        if values[0] not in field.ordered_options:
            raise FilterValidationError(
                f"'{values[0]!r}' is not one of {field.key}'s ordered_options -- ordinal comparisons only "
                f"operate against the explicitly ordered options, never a non_ordered_option"
            )
        return

    if field.type == FilterFieldType.NUMBER:
        for v in values:
            _parse_number(v)
    elif field.type == FilterFieldType.DATE:
        for v in values:
            _parse_date(v)
    elif field.type in (FilterFieldType.SINGLE_SELECT, FilterFieldType.MULTI_SELECT) and field.options:
        # Closed vocabulary -- reject a value outside the declared options. Open fields
        # (options == []), e.g. technologies/investment_industry, accept any string.
        valid = {o.lower() for o in field.options}
        for v in values:
            if str(v).lower() not in valid:
                raise FilterValidationError(f"'{v!r}' is not a valid option for field '{field.key}'")


def validate_query(query: FilterQuery, registry: list[FilterFieldMeta]) -> dict[str, FilterFieldMeta]:
    """Returns the field-by-key lookup on success (handed to matches_query so the
    filter/sort pass doesn't have to re-scan the registry list per condition).
    Raises FilterValidationError on the first problem found -- field/operator/value
    are all checked against the registry, never trusted as raw column/SQL input."""
    field_by_key = {f.key: f for f in registry}
    for condition in query.filters:
        field = field_by_key.get(condition.field)
        if field is None:
            raise FilterValidationError(f"Unknown filterable field: '{condition.field}'")
        validate_condition(condition, field)

    if query.logic not in ("AND", "OR"):
        raise FilterValidationError(f"logic must be 'AND' or 'OR', got {query.logic!r}")

    if query.sort is not None:
        sort_field = field_by_key.get(query.sort.field)
        if sort_field is None:
            raise FilterValidationError(f"Unknown sort field: '{query.sort.field}'")
        if query.sort.direction not in ("asc", "desc"):
            raise FilterValidationError(f"sort direction must be 'asc' or 'desc', got {query.sort.direction!r}")

    return field_by_key


# --- Value extraction ---


def get_field_value(contact: CrmContact, field: FilterFieldMeta) -> Any:
    if field.source == "custom":
        field_key = field.key[len(CUSTOM_FIELD_PREFIX) :]
        return contact.custom_fields.get(field_key)
    return getattr(contact, field.key, None)


# --- Predicate evaluation, one condition at a time ---


def _scalar_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _eval_scalar_condition(value: Any, operator: str, values: list[Any], field: FilterFieldMeta) -> bool:
    if field.type == FilterFieldType.NUMBER:
        if value is None:
            return False
        num = float(value)
        if operator == "eq":
            return any(num == _parse_number(v) for v in values)
        if operator == "neq":
            return all(num != _parse_number(v) for v in values)
        if operator == "gt":
            return num > _parse_number(values[0])
        if operator == "gte":
            return num >= _parse_number(values[0])
        if operator == "lt":
            return num < _parse_number(values[0])
        if operator == "lte":
            return num <= _parse_number(values[0])

    if field.type == FilterFieldType.DATE:
        if value is None:
            return False
        contact_date = _parse_date(value)
        if operator == "eq":
            return any(contact_date == _parse_date(v) for v in values)
        if operator == "before":
            return contact_date < _parse_date(values[0])
        if operator == "after":
            return contact_date > _parse_date(values[0])
        if operator == "on_or_before":
            return contact_date <= _parse_date(values[0])
        if operator == "on_or_after":
            return contact_date >= _parse_date(values[0])

    if field.type == FilterFieldType.BOOLEAN:
        if operator == "is_true":
            return value is True
        if operator == "is_false":
            return value is False

    if field.ordered and operator in _ORDINAL_OPERATORS:
        current = _scalar_str(value)
        if current is None or current not in field.ordered_options:
            return False
        current_pos = field.ordered_options.index(current)
        threshold_pos = field.ordered_options.index(values[0])
        if operator == "gt":
            return current_pos > threshold_pos
        if operator == "gte":
            return current_pos >= threshold_pos
        if operator == "lt":
            return current_pos < threshold_pos
        if operator == "lte":
            return current_pos <= threshold_pos

    # TEXT and SINGLE_SELECT (and any other scalar field's eq/neq/contains/not_contains).
    # Missing/empty data never satisfies ANY comparison operator, including neq/
    # not_contains -- "we don't know" is not the same as "confirmed different,"
    # and treating it as a match would silently pull in contacts with no data at
    # all under a filter meant to exclude a specific known value. is_empty/
    # is_not_empty (handled earlier, before this function is even called) remain
    # the only way to query for missing data.
    current = _scalar_str(value)
    if current is None:
        return False
    current_lower = current.lower()
    values_lower = [str(v).lower() for v in values]
    if operator == "eq":
        return current_lower in values_lower
    if operator == "neq":
        return current_lower not in values_lower
    if operator == "contains":
        return any(v in current_lower for v in values_lower)
    if operator == "not_contains":
        return all(v not in current_lower for v in values_lower)

    raise FilterValidationError(f"Unhandled operator '{operator}' for field type {field.type}")  # pragma: no cover -- guarded by validate_condition


def _eval_list_condition(value: Any, operator: str, values: list[Any], field: FilterFieldMeta) -> bool:
    current_list = [str(v).lower() for v in (value or [])]
    values_lower = [str(v).lower() for v in values]

    if field.ordered and operator in _ORDINAL_OPERATORS:
        ordered_lower = [o.lower() for o in field.ordered_options]
        threshold_pos = ordered_lower.index(values_lower[0])
        positions = [ordered_lower.index(v) for v in current_list if v in ordered_lower]
        if not positions:
            return False
        if operator == "gt":
            return max(positions) > threshold_pos
        if operator == "gte":
            return max(positions) >= threshold_pos
        if operator == "lt":
            return min(positions) < threshold_pos
        if operator == "lte":
            return min(positions) <= threshold_pos

    if operator == "contains_any":
        return any(v in current_list for v in values_lower)
    if operator == "contains_all":
        return all(v in current_list for v in values_lower)
    if operator == "not_contains":
        return all(v not in current_list for v in values_lower)

    raise FilterValidationError(f"Unhandled operator '{operator}' for field type {field.type}")  # pragma: no cover -- guarded by validate_condition


def evaluate_condition(contact: CrmContact, condition: FilterCondition, field: FilterFieldMeta) -> bool:
    value = get_field_value(contact, field)

    if condition.operator == "is_empty":
        return _is_empty(value)
    if condition.operator == "is_not_empty":
        return not _is_empty(value)

    values = _as_value_list(condition.value)
    if field.storage_shape == "list":
        return _eval_list_condition(value, condition.operator, values, field)
    return _eval_scalar_condition(value, condition.operator, values, field)


def _threshold_position(condition: FilterCondition, field: FilterFieldMeta) -> int:
    threshold = _as_value_list(condition.value)[0]
    return field.ordered_options.index(threshold)


def _position_satisfies(position: int, condition: FilterCondition, field: FilterFieldMeta) -> bool:
    threshold_pos = _threshold_position(condition, field)
    if condition.operator == "gt":
        return position > threshold_pos
    if condition.operator == "gte":
        return position >= threshold_pos
    if condition.operator == "lt":
        return position < threshold_pos
    if condition.operator == "lte":
        return position <= threshold_pos
    raise FilterValidationError(f"Unhandled ordinal operator '{condition.operator}'")  # pragma: no cover -- guarded by validate_condition


def _evaluate_ordinal_group(contact: CrmContact, group: list[FilterCondition], field: FilterFieldMeta) -> bool:
    """
    Two or more ordinal (gt/gte/lt/lte) conditions on the SAME field must be
    satisfied by ONE selected/current value together, not independently --
    otherwise `gte A AND lte B` on a multi-select field would let two
    DIFFERENT selected options each satisfy one bound, incorrectly matching a
    contact who has nothing actually between A and B (e.g. Check Size
    ["$1k-$10k", "$5M-$10M"] would wrongly satisfy ">= $500k-$1M AND <=
    $2M-$5M" if each condition were checked against the list independently).

    For a SCALAR field this is a no-op versus evaluating each condition
    independently -- there's only one candidate value either way, so a single-
    select ordered field (Age Range, Revenue Stage) is unaffected by this
    grouping; it only changes behavior for ordered MULTI_SELECT fields (Check
    Size Personal/Institutional today), and does so via the same generic
    `ordered_options`/`non_ordered_options` mechanism, not a Check-Size-specific
    branch.
    """
    value = get_field_value(contact, field)
    candidates = value if field.storage_shape == "list" else [value]
    for candidate in candidates or []:
        candidate_str = _scalar_str(candidate)
        if candidate_str is None or candidate_str not in field.ordered_options:
            continue  # a non_ordered_option (e.g. "Other:") never satisfies an ordinal bound
        position = field.ordered_options.index(candidate_str)
        if all(_position_satisfies(position, condition, field) for condition in group):
            return True
    return False


def _evaluate_all_conditions(
    contact: CrmContact, conditions: list[FilterCondition], field_by_key: dict[str, FilterFieldMeta]
) -> list[bool]:
    """
    One boolean per condition, EXCEPT: a field with 2+ ordinal conditions in
    this same query collapses to exactly one boolean (see
    _evaluate_ordinal_group) so it participates in the surrounding AND/OR
    exactly like any other single condition. A field with 0 or 1 ordinal
    condition -- the overwhelming majority of queries -- takes the original,
    unchanged per-condition path.
    """
    ordinal_by_field: dict[str, list[FilterCondition]] = {}
    for condition in conditions:
        if condition.operator in _ORDINAL_OPERATORS:
            ordinal_by_field.setdefault(condition.field, []).append(condition)
    grouped_fields = {key for key, group in ordinal_by_field.items() if len(group) > 1}

    results: list[bool] = []
    already_evaluated_group: set[str] = set()
    for condition in conditions:
        if condition.field in grouped_fields:
            if condition.field in already_evaluated_group:
                continue
            already_evaluated_group.add(condition.field)
            results.append(_evaluate_ordinal_group(contact, ordinal_by_field[condition.field], field_by_key[condition.field]))
        else:
            results.append(evaluate_condition(contact, condition, field_by_key[condition.field]))
    return results


def matches_query(contact: CrmContact, query: FilterQuery, field_by_key: dict[str, FilterFieldMeta]) -> bool:
    if not query.filters:
        return True
    results = _evaluate_all_conditions(contact, query.filters, field_by_key)
    return all(results) if query.logic == "AND" else any(results)


# --- Sorting ---


def sort_key(contact: CrmContact, sort_field: FilterFieldMeta) -> tuple[bool, str]:
    """(is_empty, comparable_value) -- empty/missing values always sort last,
    regardless of direction, same convention as compareContactsByName on the
    frontend (nameless contacts sort to the end, never scattered unpredictably)."""
    value = get_field_value(contact, sort_field)
    empty = _is_empty(value)
    if empty:
        return (True, "")
    if isinstance(value, list):
        comparable = ", ".join(str(v) for v in value).lower()
    else:
        comparable = str(value).lower()
    return (False, comparable)


def query_contacts(
    contacts: list[CrmContact], query: FilterQuery, field_by_key: dict[str, FilterFieldMeta]
) -> CrmContactPage:
    if not query.include_archived:
        contacts = [c for c in contacts if not c.archived]
    filtered = [c for c in contacts if matches_query(c, query, field_by_key)]

    if query.sort is not None:
        sort_field = field_by_key[query.sort.field]
        filtered.sort(key=lambda c: sort_key(c, sort_field), reverse=(query.sort.direction == "desc"))
        # Empty values stay last even when reverse=True flips everything else --
        # re-stabilize by keeping the natural (empty-last) order for ties on the
        # emptiness flag specifically. Python's sort is stable, and reverse=True
        # would otherwise put empties FIRST on a descending sort, which is wrong.
        if query.sort.direction == "desc":
            non_empty = [c for c in filtered if not sort_key(c, sort_field)[0]]
            empty = [c for c in filtered if sort_key(c, sort_field)[0]]
            filtered = non_empty + empty

    total = len(filtered)
    page = max(query.page, 1)
    page_size = max(query.page_size, 1)
    start = (page - 1) * page_size
    items = filtered[start : start + page_size]
    return CrmContactPage(items=items, total=total, page=page, page_size=page_size)
