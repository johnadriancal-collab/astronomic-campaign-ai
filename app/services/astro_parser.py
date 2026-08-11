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

from app.models.crm import INDUSTRY_OPTIONS, FilterCondition

INVESTOR_TYPE_FIELD_KEY = "custom:investor_type"
INVESTMENT_INDUSTRY_FIELD_KEY = "custom:investment_industry"
CHECK_SIZE_PERSONAL_FIELD_KEY = "custom:check_size_personal"
INVESTOR_MODE_FIELD_KEY = "thesis_investor_mode"

SUPPORTED_FIELDS_MESSAGE = (
    "I can filter by Investment Industry, Investor Type, Check Size Personal, "
    "Investor Mode, City/State, and archived status."
)


@dataclass
class ParsedCommand:
    intent: str  # "search_contacts" | "count_contacts"
    filters: list[FilterCondition]
    include_archived: bool
    understood_as: str


@dataclass
class UnresolvedCommand:
    understood: dict[str, str] = field(default_factory=dict)
    unresolved_phrase: str = ""
    message: str = ""


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
