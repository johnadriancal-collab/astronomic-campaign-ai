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

Every rule has the signature `(raw_row, context) -> dict`. `context` is a
per-BATCH (not per-row) dict of reference data a rule needs but that would
be wasteful/wrong to look up on every row -- e.g. classify_role's live
approved-options set, fetched from the CrmCustomFieldDefinition once per
import and passed to every row. Rules that need no reference data (e.g.
classify_industry) just ignore the second argument. See
CrmImportService.preview() for where `context` is built.

To add a future rule (Check Size standardization, etc.): write one
function with this same signature and add it to CLASSIFICATION_RULES. If
it needs reference data, extend `build_classification_context()` too. No
other change to the import pipeline is required.
"""

from typing import Any, Callable

Classifier = Callable[[dict[str, str], dict[str, Any]], dict[str, Any]]


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


def classify_industry(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
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


def classify_investor_mode(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    investor_type (custom field) <- CSV `Investor type`, comma-split and
    trimmed, stored exactly as given -- never reinterpreted, renamed, or
    filtered against a fixed list.

    thesis_investor_mode ("Invests privately or institutionally?") <-
    derive_investor_mode(investor_type), the SAME function that powers the
    manual create/update automation (imported directly, not reimplemented)
    so a CSV-imported contact and a manually-entered contact are always
    classified identically. Left unset (never guessed) when investor_type
    carries no private/institutional signal.
    """
    from app.models.crm import derive_investor_mode  # local import avoids a hard import-time dependency for callers that only need classify_industry/classify_role

    tokens = _split_tokens(_find_column(raw_row, "Investor type", "Investor Type"))
    if not tokens:
        return {}

    result: dict[str, Any] = {"custom:investor_type": tokens}
    mode = derive_investor_mode(tokens)
    if mode:
        result["thesis_investor_mode"] = mode
    return result


def classify_role(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    role (custom field) <- CSV `Role`, comma-split and trimmed, kept ONLY
    if it exactly matches the live approved Role taxonomy (context
    ["role_options"], fetched from the CrmCustomFieldDefinition -- see
    build_classification_context() -- never hardcoded here). Unsupported
    tags (job titles like "VP", "Director", "President" that merely sound
    role-adjacent) are dropped, never mapped to the nearest approved value.
    """
    approved = context.get("role_options") or set()
    tokens = _split_tokens(_find_column(raw_row, "Role"))
    kept = [t for t in tokens if t in approved]
    return {"custom:role": kept} if kept else {}


def classify_dinner_subscriptions(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    dinner_subscriptions (custom field) <- CSV `Dinner Subscriptions`,
    comma-split and trimmed, then run through
    normalize_dinner_subscriptions() -- the SAME function that powers the
    one-time contact-value migration (imported directly, not reimplemented)
    so a freshly-imported contact and a migrated contact always end up with
    identical normalized values. This is what makes legacy wording (e.g.
    "Sigma Librae Dinners", "Retreats") get collapsed/dropped automatically
    on every future upload, regardless of whether the human maps this
    column at all -- see module docstring.
    """
    from app.models.crm import normalize_dinner_subscriptions  # local import, same rationale as classify_investor_mode

    tokens = _split_tokens(_find_column(raw_row, "Dinner Subscriptions", "Dinner Subscription"))
    if not tokens:
        return {}
    normalized = normalize_dinner_subscriptions(tokens)
    return {"custom:dinner_subscriptions": normalized} if normalized else {}


# Registry of independent classification rules. Each is applied to every
# imported row, in order; later rules win on key collision (none currently
# collide). Add new rules here -- see module docstring.
CLASSIFICATION_RULES: list[Classifier] = [
    classify_industry,
    classify_investor_mode,
    classify_role,
    classify_dinner_subscriptions,
]


async def build_classification_context(custom_field_store: Any) -> dict[str, Any]:
    """
    Reference data every rule in CLASSIFICATION_RULES needs, computed ONCE
    per import batch (not per row) and passed to every rule call. Add a key
    here when a new rule needs live reference data (e.g. a fixed options
    list) rather than deriving it fresh per row.
    """
    role_field = await custom_field_store.get_by_field_key("role")
    return {"role_options": set(role_field.options) if role_field else set()}


def apply_classification_rules(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """Runs every registered rule against the raw row and merges their output."""
    result: dict[str, Any] = {}
    for rule in CLASSIFICATION_RULES:
        result.update(rule(raw_row, context))
    return result
