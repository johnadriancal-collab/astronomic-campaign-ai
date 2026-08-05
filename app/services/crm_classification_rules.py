"""
Centralized CSV-import classification/standardization layer.

Column mapping (see crm_import_service.py's `_apply_mapping`) is a strict
1:1 human-confirmed mapping: one CSV header -> one CRM target field. That
model can't express "derive this field from a specific source column
regardless of what mapping was chosen" or "combine several source columns
into one field" -- which is exactly how a mapping mistake once put Main
Industry's values into the core `industry` field. Classification rules
close that gap: each rule reads directly from the RAW CSV row (matched by
header alias, independent of column_mapping) and returns the field(s) it
derives. Rule output is applied AFTER column mapping in
crm_import_service.py and always wins for the fields it touches, so a bad
column_mapping can never override it again.

To add a future rule (Investor Type standardization, Role standardization,
Check Size standardization, etc.): write one function with the same
signature as classify_industry() below and add it to CLASSIFICATION_RULES.
No other change to the import pipeline is required.
"""

from typing import Any, Callable

Classifier = Callable[[dict[str, str]], dict[str, Any]]


def _normalize_header(header: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in header.strip().lower())
    return " ".join(cleaned.split())


def _find_column(raw_row: dict[str, str], *aliases: str) -> str | None:
    """
    Looks up a raw row's value by normalized-header match against any of
    `aliases`, independent of the human's column_mapping -- this is what
    lets a rule fire automatically on every future upload, with zero
    manual configuration.
    """
    normalized_aliases = {_normalize_header(a) for a in aliases}
    for header, value in raw_row.items():
        if _normalize_header(header) in normalized_aliases and value and value.strip():
            return value.strip()
    return None


def _split_tokens(value: str | None) -> list[str]:
    return [t.strip() for t in value.split(",") if t.strip()] if value else []


def _ordered_dedup(*token_lists: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for tokens in token_lists:
        for token in tokens:
            if token not in seen:
                seen.add(token)
                merged.append(token)
    return merged


def classify_industry(raw_row: dict[str, str]) -> dict[str, Any]:
    """
    industry <- CSV `Industry` only (the real Apollo/LinkedIn company
    industry). Never Main Industry/Sub-industry -- those are investment-
    interest data, a different concept from company industry.

    investment_industry (custom field) <- ordered, deduplicated union of
    Main Industry tokens (comma-split, original order preserved) then
    Sub-industry tokens (comma-split, original order preserved). New/unseen
    values are stored as-is -- there is no predefined option list to
    validate against, by design.
    """
    result: dict[str, Any] = {}

    industry = _find_column(raw_row, "Industry")
    if industry:
        result["industry"] = industry

    main_tokens = _split_tokens(_find_column(raw_row, "Main Industry"))
    sub_tokens = _split_tokens(_find_column(raw_row, "Sub-industry", "Sub Industry", "Subindustry"))
    merged = _ordered_dedup(main_tokens, sub_tokens)
    if merged:
        result["custom:investment_industry"] = merged

    return result


# Registry of independent classification rules. Each is applied to every
# imported row, in order; later rules win on key collision (none currently
# collide). Add new rules here -- see module docstring.
CLASSIFICATION_RULES: list[Classifier] = [classify_industry]


def apply_classification_rules(raw_row: dict[str, str]) -> dict[str, Any]:
    """Runs every registered rule against the raw row and merges their output."""
    result: dict[str, Any] = {}
    for rule in CLASSIFICATION_RULES:
        result.update(rule(raw_row))
    return result
