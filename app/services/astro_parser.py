"""
Astro Core Phase 1 -- deterministic, Claude-free command parser for read-only
CRM search/count. Pure text -> FilterQuery translation, NO filtering logic of
its own: the output is handed to CrmService.query_contacts(), the exact same
validated engine POST /crm/contacts/query already uses (see
app/services/crm_filter_service.py). This module never imports httpx, never
imports ClaudeClient, and makes zero network calls -- see app/api/astro.py
for the route that wires this to the CRM.

Design (approved 2026-08-12):
  - Investor Type ("family offices", "institutional investors", "VC") is
    resolved against the LIVE custom-field options (passed in by the caller,
    fetched fresh from CrmService.get_filterable_fields() per request) --
    never a hardcoded list, since that vocabulary is admin-editable.
  - Investment Industry ("AI", "fintech") is resolved against the closed
    INDUSTRY_OPTIONS vocabulary (the same constant the registry itself uses
    for the sibling thesis_private_industries/thesis_institutional_industries
    fields), but the FILTER TARGET is custom:investment_industry -- the
    consolidated field. Known consequence: a contact whose investment_industry
    only has legacy-CSV-vocabulary strings (e.g. "Information Technology (IT)")
    won't match an ITF-vocabulary alias target even if semantically related --
    that's a real data-coverage gap in investment_industry itself, not
    something this parser tries to paper over by inventing a translation.
  - "institutional investor(s)" -> custom:investor_type contains_any
    ["Institutional Investor"] (an explicit tag with that exact meaning).
    "institutionally"/"invests institutionally"/"privately" (the HOW someone
    invests, Q6) -> thesis_investor_mode. These are deliberately kept
    separate per the approved design -- see _INVESTOR_MODE_TRIGGERS below.
  - Check Size thresholds ("$100k+") are resolved against the field's own
    LIVE ordered_options (passed in by the caller) via a small generic
    bucket-lower-bound parser -- never a hardcoded amount-to-bucket table.
  - Location (city/state) uses a deliberately small, explicit, hand-
    maintained gazetteer -- NOT a general-purpose one. Expand it over time
    as real prospecting geographies come up; an unrecognized location is
    unresolved, never guessed.
  - Anything left over after every known extractor has run, once the finite
    STOPWORDS set is subtracted, makes the whole command Unresolved -- never
    silently dropped or guessed into a filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from app.models.crm import INDUSTRY_OPTIONS, FilterCondition, FilterQuery

INVESTOR_TYPE_FIELD_KEY = "custom:investor_type"
INVESTMENT_INDUSTRY_FIELD_KEY = "custom:investment_industry"
CHECK_SIZE_PERSONAL_FIELD_KEY = "custom:check_size_personal"
INVESTOR_MODE_FIELD_KEY = "thesis_investor_mode"

SUPPORTED_FIELDS_MESSAGE = (
    "I can filter by Investment Industry, Investor Type, Check Size Personal, "
    "Investor Mode, City/State, and archived status."
)

FIELD_LABELS: dict[str, str] = {
    "city": "City",
    "state": "State",
    INVESTOR_TYPE_FIELD_KEY: "Investor Type",
    INVESTMENT_INDUSTRY_FIELD_KEY: "Investment Industry",
    CHECK_SIZE_PERSONAL_FIELD_KEY: "Check Size Personal",
    INVESTOR_MODE_FIELD_KEY: "Investor Mode",
}


@dataclass
class ParsedCommand:
    intent: str  # "search_contacts" | "count_contacts"
    filters: list[FilterCondition]
    include_archived: bool
    understood_as: str
    # Populated ONLY for a resolved Phase 1.1 refinement turn (never for a
    # standalone Phase 1 command) -- see attempt_refinement() below.
    operation: str | None = None  # "add" | "replace" | "remove" | "reset" | "change_intent"
    changed_field: str | None = None
    # Deterministic message-builder: the parser knows WHAT happened and HOW to
    # phrase it, but not the result count (it has no DB access) -- the route
    # calls this with the real `total` from CrmService.query_contacts() to
    # produce the final user-facing string. Never touches Claude/Anthropic.
    message_template: Callable[[int], str] | None = None


@dataclass
class UnresolvedCommand:
    understood: dict[str, str] = field(default_factory=dict)
    unresolved_phrase: str = ""
    message: str = ""
    # Populated ONLY when this Unresolved came from a refinement attempt (not
    # a standalone Phase 1 parse) -- the exact, byte-for-byte unchanged query
    # the caller had before this turn, so the route can prove/report that an
    # ambiguous refinement never mutated anything.
    unchanged_query: FilterQuery | None = None


# --- normalization ---


def _normalize(text: str) -> str:
    lowered = text.lower()
    # Keep letters/digits/$/%/&/+/./whitespace; drop other punctuation (?, commas,
    # etc.) -- '&' and '$' are needed for option strings ("Aerospace & Defense"),
    # '+' and '.' are needed for dollar-amount triggers ("$100k+", "$1.5M+").
    cleaned = re.sub(r"[^a-z0-9$%&+.\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


# --- intent ---

_COUNT_TRIGGERS = ("how many", "count of", "number of", "count")
_SEARCH_TRIGGERS = ("find", "show", "search", "list")

# Every intent-trigger word doubles as a stopword -- it's consumed by
# _detect_intent's substring check but never removed from the text buffer
# itself, so it must be dropped during the final leftover check too.
_STOPWORDS = {
    "find", "show", "search", "list", "count", "how", "many", "of", "number",
    "are", "is", "in", "with", "who", "interested", "invest", "invests",
    "investing", "investor", "investors", "contact", "contacts", "people",
    "the", "a", "an", "for", "and", "or", "companies", "you", "have", "has",
    "me", "us", "please", "to", "at", "check", "size", "sizes", "both",
}


def _detect_intent(normalized: str) -> str | None:
    if any(trigger in normalized for trigger in _COUNT_TRIGGERS):
        return "count_contacts"
    if any(normalized.startswith(trigger) for trigger in _SEARCH_TRIGGERS):
        return "search_contacts"
    return None


# --- archived ---

_ARCHIVED_TRIGGERS = ("including archived", "include archived", "archived too", "with archived")


def _extract_archived(remaining: str) -> tuple[str, bool]:
    for trigger in _ARCHIVED_TRIGGERS:
        idx = remaining.find(trigger)
        if idx != -1:
            remaining = remaining[:idx] + " " + remaining[idx + len(trigger) :]
            return remaining, True
    return remaining, False


# --- generic alias consumption (investor type, industry) ---


def _find_all_and_consume(remaining: str, alias_map: dict[str, str]) -> tuple[str, list[str]]:
    """
    Longest-alias-first, word-boundary matching, repeated until no more
    aliases match (so multiple distinct values for the same field -- e.g.
    two industries in one command -- are all captured, and removing one
    match can never leave a stale position for the next search).
    """
    matched: list[str] = []
    sorted_aliases = sorted(alias_map.keys(), key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for alias in sorted_aliases:
            m = re.search(r"\b" + re.escape(alias) + r"\b", remaining)
            if m:
                target = alias_map[alias]
                if target not in matched:
                    matched.append(target)
                remaining = remaining[: m.start()] + " " + remaining[m.end() :]
                changed = True
                break
    return remaining, matched


# --- investor type (live-registry-derived) ---

_INVESTOR_TYPE_MANUAL_SYNONYMS = {
    "vc": "Venture Capital",
    "vcs": "Venture Capital",
    "pe": "Private Equity",
    "lp": "Fund LP",
    "lps": "Fund LP",
    "angel": "Angel Investor",
    "angels": "Angel Investor",
}


def _investor_type_aliases(live_options: list[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for option in live_options:
        lower = option.lower()
        aliases[lower] = option
        aliases[lower + "s"] = option  # simple pluralization, e.g. "family offices"
    for synonym, target in _INVESTOR_TYPE_MANUAL_SYNONYMS.items():
        if target in live_options:  # never trust a synonym whose target isn't actually live
            aliases[synonym] = target
    return aliases


# --- investment industry (INDUSTRY_OPTIONS-derived, see module docstring) ---

_INDUSTRY_MANUAL_ALIASES = {
    "ai": "Artificial Intelligence / Machine Learning",
    "ml": "Artificial Intelligence / Machine Learning",
    "artificial intelligence": "Artificial Intelligence / Machine Learning",
    "machine learning": "Artificial Intelligence / Machine Learning",
    "fintech": "Fintech (Finance & Insurance)",
    "finance": "Fintech (Finance & Insurance)",
    "healthtech": "Healthcare & HealthTech",
    "healthcare": "Healthcare & HealthTech",
    "health": "Healthcare & HealthTech",
    "proptech": "Real Estate & PropTech",
    "real estate": "Real Estate & PropTech",
    "saas": "SaaS / Software Infrastructure",
    "software": "SaaS / Software Infrastructure",
    "cyber": "Cybersecurity",
    "edtech": "EdTech (Education Technology)",
    "education": "EdTech (Education Technology)",
    "climate": "Climate Tech & Sustainability",
    "sustainability": "Climate Tech & Sustainability",
    "govtech": "GovTech / Civic Tech",
    "civic tech": "GovTech / Civic Tech",
    "hrtech": "HR Tech & Future of Work",
    "adtech": "Marketing & AdTech",
    "marketing": "Marketing & AdTech",
    "mental health": "Mental Health & Wellness",
    "wellness": "Mental Health & Wellness",
    "agtech": "AgTech & Food Production",
    "biotech": "Biotech & Life Sciences",
    "life sciences": "Biotech & Life Sciences",
    "gaming": "Entertainment & Gaming",
    "creator economy": "Social Media & Creator Economy",
    "social media": "Social Media & Creator Economy",
    "legaltech": "LegalTech",
    "telecom": "Telecom & Connectivity",
    "travel": "Travel, Tourism & Hospitality",
    "hospitality": "Travel, Tourism & Hospitality",
    "automotive": "Automotive & Mobility",
    "mobility": "Automotive & Mobility",
    "construction": "Construction & Built Environment",
    "manufacturing": "Industrial / Manufacturing / Robotics",
    "robotics": "Industrial / Manufacturing / Robotics",
    "fashion": "Fashion & Apparel",
    "retail": "Consumer Goods & Retail",
    "media": "Creative Industries (Media, Music, Photo, etc.)",
    "defense": "Aerospace & Defense",
    "aerospace": "Aerospace & Defense",
    "veterinary": "Veterinary / Animal Health",
    "animal health": "Veterinary / Animal Health",
}


def _build_industry_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {option.lower(): option for option in INDUSTRY_OPTIONS}
    for alias, target in _INDUSTRY_MANUAL_ALIASES.items():
        # Fail fast at import time rather than silently accepting a typo'd
        # target that would otherwise never resolve to a real option.
        assert target in INDUSTRY_OPTIONS, f"Astro industry alias {alias!r} points to unknown option {target!r}"
        aliases[alias] = target
    return aliases


_INDUSTRY_ALIASES = _build_industry_aliases()


# --- investor mode (HOW someone invests -- distinct from investor type) ---

_INVESTOR_MODE_TRIGGERS = {
    "institutionally": ["Institutionally", "Both"],
    "privately": ["Privately", "Both"],
}


def _extract_investor_mode(remaining: str) -> tuple[str, list[FilterCondition], list[str]]:
    conditions: list[FilterCondition] = []
    labels: list[str] = []
    for trigger, values in _INVESTOR_MODE_TRIGGERS.items():
        m = re.search(r"\b" + re.escape(trigger) + r"\b", remaining)
        if m:
            conditions.append(FilterCondition(field=INVESTOR_MODE_FIELD_KEY, operator="eq", value=values))
            labels.append(trigger)
            remaining = remaining[: m.start()] + " " + remaining[m.end() :]
    return remaining, conditions, labels


# --- location: deliberately small, explicit gazetteer (2026-08-12) ---
# Expand this table over time as real prospecting geographies come up --
# NOT a general-purpose gazetteer. Unrecognized locations are unresolved,
# never guessed.

_LOCATION_ALIASES: dict[str, tuple[str, str]] = {
    "austin": ("city", "Austin"),
    "texas": ("state", "Texas"),
    "tx": ("state", "Texas"),
}


def _extract_locations(remaining: str) -> tuple[str, list[FilterCondition]]:
    conditions: list[FilterCondition] = []
    sorted_aliases = sorted(_LOCATION_ALIASES.keys(), key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for alias in sorted_aliases:
            m = re.search(r"\b" + re.escape(alias) + r"\b", remaining)
            if m:
                field_key, value = _LOCATION_ALIASES[alias]
                conditions.append(FilterCondition(field=field_key, operator="eq", value=value))
                remaining = remaining[: m.start()] + " " + remaining[m.end() :]
                changed = True
                break
    return remaining, conditions


# --- check size: amount parsing + live ordered-bucket resolution ---

_DOLLAR_PATTERN = re.compile(r"\$?(\d+(?:\.\d+)?)\s*([km])?\s*\+", re.IGNORECASE)
_BUCKET_LEADING_AMOUNT = re.compile(r"\$?(\d+(?:\.\d+)?)\s*([km])?", re.IGNORECASE)


def _apply_suffix(amount: float, suffix: str) -> float:
    suffix = suffix.lower()
    if suffix == "k":
        return amount * 1_000
    if suffix == "m":
        return amount * 1_000_000
    return amount


def _bucket_lower_bound(option: str) -> float | None:
    m = _BUCKET_LEADING_AMOUNT.match(option.strip())
    if not m:
        return None
    return _apply_suffix(float(m.group(1)), m.group(2) or "")


def _resolve_check_size_bucket(amount: float, ordered_options: list[str]) -> str | None:
    candidates = [(opt, _bucket_lower_bound(opt)) for opt in ordered_options]
    candidates = [(opt, lb) for opt, lb in candidates if lb is not None]
    candidates.sort(key=lambda pair: pair[1])
    for opt, lower_bound in candidates:
        if lower_bound >= amount:
            return opt
    return candidates[-1][0] if candidates else None


def _extract_check_size(remaining: str, ordered_options: list[str]) -> tuple[str, str | None]:
    match = _DOLLAR_PATTERN.search(remaining)
    if not match:
        return remaining, None
    amount = _apply_suffix(float(match.group(1)), match.group(2) or "")
    bucket = _resolve_check_size_bucket(amount, ordered_options)
    if bucket is None:
        return remaining, None
    remaining = remaining[: match.start()] + " " + remaining[match.end() :]
    return remaining, bucket


# --- main entry point ---


def parse(
    text: str,
    investor_type_options: list[str],
    check_size_ordered_options: list[str],
) -> ParsedCommand | UnresolvedCommand:
    normalized = _normalize(text)
    intent = _detect_intent(normalized)

    remaining = normalized
    understood: dict[str, str] = {}
    filters: list[FilterCondition] = []

    remaining, include_archived = _extract_archived(remaining)
    if include_archived:
        understood["Archived"] = "included"

    remaining, check_size_bucket = _extract_check_size(remaining, check_size_ordered_options)
    if check_size_bucket:
        filters.append(FilterCondition(field=CHECK_SIZE_PERSONAL_FIELD_KEY, operator="gte", value=check_size_bucket))
        understood["Check Size Personal"] = f">= {check_size_bucket}"

    remaining, matched_investor_types = _find_all_and_consume(remaining, _investor_type_aliases(investor_type_options))
    if matched_investor_types:
        filters.append(FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=matched_investor_types))
        understood["Investor Type"] = ", ".join(matched_investor_types)

    remaining, mode_conditions, mode_labels = _extract_investor_mode(remaining)
    filters.extend(mode_conditions)
    if "institutionally" in mode_labels and "privately" in mode_labels:
        understood["Investor Mode"] = "Both"
    elif "institutionally" in mode_labels:
        understood["Investor Mode"] = "Institutionally (or Both)"
    elif "privately" in mode_labels:
        understood["Investor Mode"] = "Privately (or Both)"

    remaining, matched_industries = _find_all_and_consume(remaining, _INDUSTRY_ALIASES)
    if matched_industries:
        filters.append(
            FilterCondition(field=INVESTMENT_INDUSTRY_FIELD_KEY, operator="contains_any", value=matched_industries)
        )
        understood["Investment Industry"] = ", ".join(matched_industries)

    remaining, location_conditions = _extract_locations(remaining)
    filters.extend(location_conditions)
    for condition in location_conditions:
        understood["City" if condition.field == "city" else "State"] = condition.value

    leftover_tokens = [t for t in remaining.split() if t not in _STOPWORDS and t.strip()]
    leftover_phrase = " ".join(leftover_tokens).strip()

    if intent is None:
        return UnresolvedCommand(
            understood=understood,
            unresolved_phrase=leftover_phrase,
            message=(
                "I didn't recognize a command here -- I understand commands starting with "
                "Find/Show/Search, or How many/Count. " + SUPPORTED_FIELDS_MESSAGE
            ),
        )

    if leftover_phrase:
        return UnresolvedCommand(
            understood=understood,
            unresolved_phrase=leftover_phrase,
            message=(
                (f"I understood {', '.join(f'{k} = {v}' for k, v in understood.items())}, " if understood else "I ")
                + f"but I don't know what you mean by '{leftover_phrase}'. " + SUPPORTED_FIELDS_MESSAGE
            ),
        )

    understood_as = ", ".join(f"{k} = {v}" for k, v in understood.items()) or "(no filters -- showing all contacts)"
    return ParsedCommand(intent=intent, filters=filters, include_archived=include_archived, understood_as=understood_as)


# =====================================================================
# Phase 1.1 (2026-08-13): conversational query refinement.
#
# Architecture: the frontend holds ALL conversation state (the last
# resolved FilterQuery + intent) and resends it as `context` on every
# follow-up request -- see app/models/astro.py's AstroCommandContext and
# app/api/astro.py's dispatch. This module has NO session store, no
# session id, no persistence of any kind; attempt_refinement() below is a
# pure function of (text, current_filters, current_include_archived,
# current_intent, live options) -> a new ParsedCommand/UnresolvedCommand,
# exactly like parse() above.
#
# app/api/astro.py always tries the STANDALONE parse() first; only when
# that returns Unresolved (most commonly: no recognized verb, e.g. "Only
# Austin" has no "find"/"show"/"how many") AND a `context` was supplied
# does it fall back to attempt_refinement(). This is what makes Phase 1
# standalone commands behave identically whether or not `context` is
# present (requirement: never change existing behavior) -- a fully
# well-formed sentence like "Find investors in Aerospace" always resolves
# via the plain parse() above and is treated as a brand-new query,
# discarding any prior context.
#
# Single refinement operation per message only (2026-08-13, approved
# scope limit) -- a message that resolves to changes on more than one
# field in one go (e.g. hypothetically "Only Austin and family offices")
# is deliberately treated as Unresolved with a "one filter at a time"
# message, rather than guessing which combination was intended. Compound
# refinements are an explicit non-goal for this phase.
# =====================================================================


def _contacts_word(total: int) -> str:
    return "contact" if total == 1 else "contacts"


def _format_understood(understood: dict[str, str]) -> str:
    return ", ".join(f"{k} = {v}" for k, v in understood.items()) or "no filters"


def _filters_to_understood(filters: list[FilterCondition]) -> dict[str, str]:
    """Same rendering convention parse() uses, but built FROM an existing filter
    list (for describing what's still active before/after a refinement) rather
    than accumulated during extraction."""
    understood: dict[str, str] = {}
    for condition in filters:
        label = FIELD_LABELS.get(condition.field, condition.field)
        if condition.field == CHECK_SIZE_PERSONAL_FIELD_KEY:
            understood[label] = f">= {condition.value}"
        elif isinstance(condition.value, list):
            understood[label] = ", ".join(str(v) for v in condition.value)
        else:
            understood[label] = str(condition.value)
    return understood


# --- FilterQuery mutation helpers (pure, deterministic) ---


def _remove_fields(filters: list[FilterCondition], field_keys: list[str]) -> list[FilterCondition]:
    return [c for c in filters if c.field not in field_keys]


def _replace_field(filters: list[FilterCondition], condition: FilterCondition) -> list[FilterCondition]:
    kept = [c for c in filters if c.field != condition.field]
    kept.append(condition)
    return kept


def _union_field(filters: list[FilterCondition], field_key: str, operator: str, new_values: list[str]) -> tuple[list[FilterCondition], list[str]]:
    """Extends the existing condition's value list (deduplicated) rather than
    adding a second AND'd condition -- a second condition on the same
    contains_any field would mean INTERSECTION, not the union "show X too"
    semantics ADD is supposed to have. Returns (new_filters, values_actually_added)."""
    existing = next((c for c in filters if c.field == field_key), None)
    if existing is None:
        return filters + [FilterCondition(field=field_key, operator=operator, value=list(new_values))], list(new_values)
    existing_values = existing.value if isinstance(existing.value, list) else [existing.value]
    added = [v for v in new_values if v not in existing_values]
    merged_values = existing_values + added
    kept = [c for c in filters if c.field != field_key]
    kept.append(FilterCondition(field=field_key, operator=operator, value=merged_values))
    return kept, added


# --- message templates (deterministic; `total` supplied later by the route) ---


def _format_threshold_display(bucket: str) -> str:
    lower = _bucket_lower_bound(bucket)
    if lower is None:
        return bucket
    if lower % 1_000_000 == 0:
        return f"${int(lower // 1_000_000)}M+"
    return f"${int(lower // 1_000)}k+"


def _msg_location(value: str) -> Callable[[int], str]:
    return lambda total: f"Showing {total} {_contacts_word(total)} in {value}. Your other filters are unchanged."


def _msg_check_size(display: str, had_prior: bool) -> Callable[[int], str]:
    if had_prior:
        return lambda total: f"Updated the check-size filter to {display}. {total} {_contacts_word(total)} match."
    return lambda total: f"Added a {display} check-size filter. {total} {_contacts_word(total)} match."


def _msg_remove_field(label: str) -> Callable[[int], str]:
    return lambda total: f"Removed the {label} filter. {total} {_contacts_word(total)} match."


def _msg_remove_value(label: str, values: list[str]) -> Callable[[int], str]:
    joined = " and ".join(values)
    return lambda total: f"Removed {joined} from your {label} filter. {total} {_contacts_word(total)} match."


def _msg_add_values(label: str, values: list[str]) -> Callable[[int], str]:
    joined = " and ".join(values)
    return lambda total: f"Added {joined} to your {label} filter. {total} {_contacts_word(total)} match."


def _msg_replace_multiselect(label: str, values: list[str]) -> Callable[[int], str]:
    joined = " or ".join(values)
    return lambda total: f"Showing {total} {_contacts_word(total)} matching {label} = {joined}. Your other filters are unchanged."


def _msg_investor_mode(desc: str) -> Callable[[int], str]:
    return lambda total: f"Showing {total} {_contacts_word(total)} who invest {desc}. Your other filters are unchanged."


def _msg_reset() -> Callable[[int], str]:
    return lambda total: f"Cleared all filters. Showing {total} {_contacts_word(total)}."


def _msg_change_intent(new_intent: str) -> Callable[[int], str]:
    if new_intent == "count_contacts":
        return lambda total: f"{total} {_contacts_word(total)} match your current filters."
    return lambda total: f"Showing {total} {_contacts_word(total)} matching your current filters."


def _msg_archived(included: bool) -> Callable[[int], str]:
    if included:
        return lambda total: f"Now including archived contacts. {total} {_contacts_word(total)} match."
    return lambda total: f"Excluding archived contacts again. {total} {_contacts_word(total)} match."


# --- concept vocabulary for whole-field REMOVE (distinct from the VALUE alias
# tables above -- "remove check size" names the FIELD, not a value) ---

_CONCEPT_TO_FIELDS: dict[str, list[str]] = {
    "check size": [CHECK_SIZE_PERSONAL_FIELD_KEY],
    "check-size": [CHECK_SIZE_PERSONAL_FIELD_KEY],
    "location": ["city", "state"],
    "city": ["city"],
    "state": ["state"],
    "investor type": [INVESTOR_TYPE_FIELD_KEY],
    "investment industry": [INVESTMENT_INDUSTRY_FIELD_KEY],
    "industry": [INVESTMENT_INDUSTRY_FIELD_KEY],
    "investor mode": [INVESTOR_MODE_FIELD_KEY],
}

_RESET_TRIGGERS = ("start over", "reset the search", "reset", "clear all filters", "clear filters", "show everyone again", "show all again")
_REFINEMENT_ONLY_STOPWORDS = {"them", "it", "again", "left", "still", "everyone", "all", "over"}


def attempt_refinement(
    text: str,
    current_filters: list[FilterCondition],
    current_include_archived: bool,
    current_intent: str,
    investor_type_options: list[str],
    check_size_ordered_options: list[str],
) -> ParsedCommand | UnresolvedCommand:
    """
    Called ONLY as a fallback when the standalone parse() above returned
    Unresolved AND the caller supplied an existing (filters, include_archived,
    intent) context -- see the module docstring above this section. Never
    called for a fully-formed standalone command; see app/api/astro.py.
    """
    normalized = _normalize(text)
    unchanged_query = FilterQuery(filters=list(current_filters), include_archived=current_include_archived)

    def unresolved(unresolved_phrase: str) -> UnresolvedCommand:
        current_desc = _format_understood(_filters_to_understood(current_filters))
        return UnresolvedCommand(
            understood={},
            unresolved_phrase=unresolved_phrase,
            message=(
                f"Your current filters are unchanged ({current_desc}). I don't know what you mean by "
                f"'{unresolved_phrase}'. " + SUPPORTED_FIELDS_MESSAGE
            ),
            unchanged_query=unchanged_query,
        )

    # 1. RESET -- checked before anything else; "show everyone/all again" must
    # not be mistaken for the filters-preserving "show them/it again" below.
    for trigger in _RESET_TRIGGERS:
        if trigger in normalized:
            return ParsedCommand(
                intent="search_contacts",
                filters=[],
                include_archived=False,
                understood_as="(reset -- showing all contacts)",
                operation="reset",
                changed_field=None,
                message_template=_msg_reset(),
            )

    # 2. Intent-only change ("How many are left?", "Show them again") -- if,
    # after stripping intent triggers + the generic Phase 1 STOPWORDS + a
    # small set of refinement-only pronouns, nothing meaningful remains.
    detected_intent = _detect_intent(normalized)
    intent_stripped = normalized
    for trigger in _COUNT_TRIGGERS + _SEARCH_TRIGGERS:
        intent_stripped = intent_stripped.replace(trigger, " ")
    leftover_after_intent = [
        t for t in intent_stripped.split() if t not in _STOPWORDS and t not in _REFINEMENT_ONLY_STOPWORDS
    ]
    if detected_intent is not None and not leftover_after_intent:
        return ParsedCommand(
            intent=detected_intent,
            filters=list(current_filters),
            include_archived=current_include_archived,
            understood_as="(unchanged filters)",
            operation="change_intent",
            changed_field=None,
            message_template=_msg_change_intent(detected_intent),
        )

    # 3. REMOVE
    remove_match = re.search(r"\b(remove|drop|clear|without)\b", normalized)
    if remove_match:
        remainder = normalized[remove_match.end() :].strip()

        for concept in sorted(_CONCEPT_TO_FIELDS, key=len, reverse=True):
            if re.search(r"\b" + re.escape(concept) + r"\b", remainder):
                field_keys = _CONCEPT_TO_FIELDS[concept]
                new_filters = _remove_fields(current_filters, field_keys)
                label = " / ".join(FIELD_LABELS.get(k, k) for k in field_keys)
                return ParsedCommand(
                    intent=current_intent,
                    filters=new_filters,
                    include_archived=current_include_archived,
                    understood_as=f"(removed {label})",
                    operation="remove",
                    changed_field=",".join(field_keys),
                    message_template=_msg_remove_field(label),
                )

        if re.search(r"\barchived\b", remainder):
            return ParsedCommand(
                intent=current_intent,
                filters=list(current_filters),
                include_archived=False,
                understood_as="(archived excluded)",
                operation="remove",
                changed_field="include_archived",
                message_template=_msg_archived(included=False),
            )

        for field_key, alias_map in (
            (INVESTOR_TYPE_FIELD_KEY, _investor_type_aliases(investor_type_options)),
            (INVESTMENT_INDUSTRY_FIELD_KEY, _INDUSTRY_ALIASES),
        ):
            _, matched_values = _find_all_and_consume(remainder, alias_map)
            if matched_values:
                existing = next((c for c in current_filters if c.field == field_key), None)
                if existing is None:
                    continue
                existing_values = existing.value if isinstance(existing.value, list) else [existing.value]
                remaining_values = [v for v in existing_values if v not in matched_values]
                if remaining_values:
                    new_filters = _replace_field(
                        current_filters, FilterCondition(field=field_key, operator=existing.operator, value=remaining_values)
                    )
                else:
                    new_filters = _remove_fields(current_filters, [field_key])
                label = FIELD_LABELS[field_key]
                return ParsedCommand(
                    intent=current_intent,
                    filters=new_filters,
                    include_archived=current_include_archived,
                    understood_as=f"(removed {', '.join(matched_values)} from {label})",
                    operation="remove",
                    changed_field=field_key,
                    message_template=_msg_remove_value(label, matched_values),
                )

        return unresolved(remainder or "that")

    # 4/5/6. ONLY (replace) / ADD (union) / bare phrase (defaults to replace)
    only_match = re.match(r"^only\b\s*(.*)", normalized)
    add_match = re.match(r"^(?:add|also|include)\b\s*(.*)", normalized)
    is_add = False
    if only_match:
        phrase = only_match.group(1)
    elif add_match:
        phrase = add_match.group(1)
        is_add = True
    else:
        phrase = normalized

    phrase = re.sub(r"\btoo\b", " ", phrase).strip()

    remainder = phrase
    remainder, check_size_bucket = _extract_check_size(remainder, check_size_ordered_options)
    remainder, matched_investor_types = _find_all_and_consume(remainder, _investor_type_aliases(investor_type_options))
    remainder, mode_conditions, mode_labels = _extract_investor_mode(remainder)
    remainder, matched_industries = _find_all_and_consume(remainder, _INDUSTRY_ALIASES)
    remainder, location_conditions = _extract_locations(remainder)
    remainder, archived_flag = _extract_archived(remainder)

    leftover_tokens = [t for t in remainder.split() if t not in _STOPWORDS]
    leftover_phrase = " ".join(leftover_tokens).strip()
    if leftover_phrase:
        return unresolved(leftover_phrase)

    changes: list[tuple[list[FilterCondition], bool, str, str, Callable[[int], str]]] = []
    # each entry: (new_filters, new_include_archived, operation, changed_field, message_template)

    if check_size_bucket:
        had_prior = any(c.field == CHECK_SIZE_PERSONAL_FIELD_KEY for c in current_filters)
        new_filters = _replace_field(
            current_filters, FilterCondition(field=CHECK_SIZE_PERSONAL_FIELD_KEY, operator="gte", value=check_size_bucket)
        )
        display = _format_threshold_display(check_size_bucket)
        changes.append((new_filters, current_include_archived, "replace", CHECK_SIZE_PERSONAL_FIELD_KEY, _msg_check_size(display, had_prior)))

    if matched_investor_types:
        if is_add:
            new_filters, added = _union_field(current_filters, INVESTOR_TYPE_FIELD_KEY, "contains_any", matched_investor_types)
            changes.append(
                (new_filters, current_include_archived, "add", INVESTOR_TYPE_FIELD_KEY,
                 _msg_add_values(FIELD_LABELS[INVESTOR_TYPE_FIELD_KEY], added or matched_investor_types))
            )
        else:
            new_filters = _replace_field(
                current_filters, FilterCondition(field=INVESTOR_TYPE_FIELD_KEY, operator="contains_any", value=matched_investor_types)
            )
            changes.append(
                (new_filters, current_include_archived, "replace", INVESTOR_TYPE_FIELD_KEY,
                 _msg_replace_multiselect(FIELD_LABELS[INVESTOR_TYPE_FIELD_KEY], matched_investor_types))
            )

    if matched_industries:
        if is_add:
            new_filters, added = _union_field(current_filters, INVESTMENT_INDUSTRY_FIELD_KEY, "contains_any", matched_industries)
            changes.append(
                (new_filters, current_include_archived, "add", INVESTMENT_INDUSTRY_FIELD_KEY,
                 _msg_add_values(FIELD_LABELS[INVESTMENT_INDUSTRY_FIELD_KEY], added or matched_industries))
            )
        else:
            new_filters = _replace_field(
                current_filters, FilterCondition(field=INVESTMENT_INDUSTRY_FIELD_KEY, operator="contains_any", value=matched_industries)
            )
            changes.append(
                (new_filters, current_include_archived, "replace", INVESTMENT_INDUSTRY_FIELD_KEY,
                 _msg_replace_multiselect(FIELD_LABELS[INVESTMENT_INDUSTRY_FIELD_KEY], matched_industries))
            )

    if mode_conditions:
        new_filters = _remove_fields(current_filters, [INVESTOR_MODE_FIELD_KEY]) + mode_conditions
        if "institutionally" in mode_labels and "privately" in mode_labels:
            desc = "both privately and institutionally"
        elif "institutionally" in mode_labels:
            desc = "institutionally"
        else:
            desc = "privately"
        changes.append((new_filters, current_include_archived, "replace", INVESTOR_MODE_FIELD_KEY, _msg_investor_mode(desc)))

    if location_conditions:
        new_filters = current_filters
        for condition in location_conditions:
            new_filters = _replace_field(new_filters, condition)
        primary = location_conditions[0]
        changes.append((new_filters, current_include_archived, "replace", primary.field, _msg_location(str(primary.value))))

    if archived_flag:
        changes.append((list(current_filters), True, "add" if is_add else "replace", "include_archived", _msg_archived(included=True)))

    if not changes:
        return unresolved(phrase.strip() or text.strip())

    if len(changes) > 1:
        fields_touched = ", ".join(FIELD_LABELS.get(c[3], c[3]) for c in changes)
        return unresolved(f"multiple filters at once ({fields_touched}) -- please change one at a time")

    new_filters, new_include_archived, operation, changed_field, message_template = changes[0]
    return ParsedCommand(
        intent=current_intent,
        filters=new_filters,
        include_archived=new_include_archived,
        understood_as=f"(refined {FIELD_LABELS.get(changed_field, changed_field)})",
        operation=operation,
        changed_field=changed_field,
        message_template=message_template,
    )
