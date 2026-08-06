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

import re
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


def _validate_single_select(raw_value: str | None, options: set[str]) -> str | None:
    """
    Kept ONLY if it exactly matches one of `options` -- same "never guess"
    principle as classify_role's approved-tags filter, applied to a
    single-select field instead of a multi-select one. A single-select
    field renders as one fixed dropdown; storing a value that isn't one of
    its options would show as blank/broken there, so an unrecognized value
    is dropped (never stored) rather than preserved verbatim -- unlike
    Dinner Subscriptions' multi-select convention, where an extra list
    item can't corrupt the field the same way.
    """
    if not raw_value:
        return None
    return raw_value if raw_value in options else None


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


def classify_dinners_attended(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    dinners_attended (custom field) <- CSV `Dinners Attended`, comma-split,
    trimmed, and order-preserving-deduplicated -- deliberately NO wording
    normalization, unlike Dinner Subscriptions. Every dated historical
    entry (e.g. "Savvy [2.25.2025] Austin") must survive verbatim; this is
    not a stable closed set the way Dinner Subscriptions is, so there is no
    legacy/delete mapping here, ever.
    """
    tokens = _split_tokens(_find_column(raw_row, "Dinners Attended", "Dinner Attended"))
    deduped = _ordered_dedup(tokens)
    return {"custom:dinners_attended": deduped} if deduped else {}


def classify_chris_degree_connection(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    chris_degree_connection (custom field, single_select) <- CSV `Chris
    Degree Connection`, kept only if it exactly matches one of the field's
    live options (context["chris_degree_connection_options"] -- see
    build_classification_context()). Every real value found in the 2026-08-06
    two-CSV audit ("1st degree"/"2nd degree"/"3rd degree") matched cleanly.
    """
    raw = _find_column(raw_row, "Chris Degree Connection")
    value = _validate_single_select(raw, context.get("chris_degree_connection_options") or set())
    return {"custom:chris_degree_connection": value} if value else {}


def classify_age_range(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    age_range (custom field, single_select) <- CSV `Age Range`, kept only
    if it exactly matches one of the field's live options (context
    ["age_range_options"]) -- e.g. "31-40", "61-70". Never invents/derives
    a range from some other column.
    """
    raw = _find_column(raw_row, "Age Range")
    value = _validate_single_select(raw, context.get("age_range_options") or set())
    return {"custom:age_range": value} if value else {}


def classify_gender(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    gender (custom field, single_select) <- CSV `Gender`, kept only if it
    exactly matches one of the field's live options (context
    ["gender_options"]).
    """
    raw = _find_column(raw_row, "Gender")
    value = _validate_single_select(raw, context.get("gender_options") or set())
    return {"custom:gender": value} if value else {}


def classify_engagement_stage(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    engagement_stage (custom field, single_select) <- CSV `Stage`, kept only
    if it exactly matches one of the field's live options (context
    ["engagement_stage_options"]).

    This exists specifically to keep `Stage` OUT of the core `funding_stage`
    field. HEADER_ALIASES used to map both "stage" and "funding stage" to
    funding_stage -- confirmed via the 2026-08-06 two-CSV audit that neither
    real export even has a "Funding Stage" column; their "Stage" column
    holds outreach/engagement values (Interested/Cold/Unresponsive/Replied),
    which is exactly what engagement_stage's own description says it's
    for: "Our own outreach/engagement pipeline stage -- NOT a funding
    stage." Classification rules always win over column_mapping, so this
    permanently prevents that collision regardless of what a human maps
    "Stage" to during a future upload.
    """
    raw = _find_column(raw_row, "Stage")
    value = _validate_single_select(raw, context.get("engagement_stage_options") or set())
    return {"custom:engagement_stage": value} if value else {}


def classify_accredited_status(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    accredited_status (custom field, single_select) <- CSV `Accredited
    Status`, kept only if it exactly matches one of the field's live
    options (context["accredited_status_options"], e.g. "Yes"/"No"). Same
    never-guess validated-single-select pattern as Chris Degree
    Connection/Age Range/Gender/Engagement Stage.
    """
    raw = _find_column(raw_row, "Accredited Status")
    value = _validate_single_select(raw, context.get("accredited_status_options") or set())
    return {"custom:accredited_status": value} if value else {}


_CHECK_SIZE_DASH_RE = re.compile(r"[‒–—−]")  # figure/en/em dash, minus sign -> hyphen


def _split_check_size_tokens(value: str | None) -> list[str]:
    """
    Splits on a comma immediately followed by whitespace ONLY -- the
    established multi-value delimiter for this column (every real
    multi-selection found in the 2026-08-06 two-CSV audit uses ", " between
    buckets, e.g. "$25k - $50k, $50k - $100k"). A comma with NO following
    whitespace is a thousands separator glued inside one dollar amount
    (e.g. "$5,000-$10,000", "$50,000-$200,000 avg.") and must never be
    split on -- confirmed by scanning every comma in both real CSVs' Check
    Size / Check Size (Institutional) columns: each one is either
    immediately followed by a digit (thousands separator) or by whitespace
    (value separator), with zero ambiguous cases. A naive `split(",")`
    would shred "$5,000-$10,000" into "$5" and "000-$10,000".
    """
    if not value:
        return []
    return [t.strip() for t in re.split(r",\s+", value) if t.strip()]


def _normalize_check_size_token(token: str, options: set[str]) -> str | None:
    """
    Maps a raw token to one of the field's live canonical bucket options
    (e.g. "$25k - $50k") if -- and only if -- it's the same bucket modulo
    harmless formatting noise: whitespace, hyphen/en-dash/em-dash variants,
    and casing. Never guesses a bucket for a range that spans several
    buckets ("$25k-$100k"), free text ("Depends on Asset Allocation", "up
    to 50k"), or any other non-exact match -- same "never guess" principle
    as _validate_single_select, generalized to a multi-select field with
    much more variant-heavy real-world input.
    """

    def _key(s: str) -> str:
        cleaned = _CHECK_SIZE_DASH_RE.sub("-", s.strip())
        cleaned = re.sub(r"\s*-\s*", "-", cleaned)
        return re.sub(r"\s+", "", cleaned).lower()

    token_key = _key(token)
    for option in options:
        if token_key == _key(option):
            return option
    return None


def classify_check_size(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    check_size_personal (custom field, multi_select) <- CSV `Check Size`.
    check_size_institutional (custom field, multi_select) <- CSV `Check
    Size (Institutional)`. These are two genuinely distinct columns in both
    real CSVs (confirmed via the 2026-08-06 audit: of the rows in CSV B
    where both columns are populated, the majority -- 64/119 -- have
    DIFFERENT values, and 20 rows have an institutional value with no
    personal value at all) -- never derive one from the other or copy one
    into the other.

    Each cell is comma-split on ", " only (see _split_check_size_tokens --
    a bare comma with no following space is a thousands separator inside
    one dollar amount, never a value delimiter) and every resulting token
    is kept ONLY if it matches one of the field's live options modulo
    whitespace/dash-variant/casing noise (see _normalize_check_size_token).
    A token that doesn't match any live option -- "Depends on Asset
    Allocation", "up to 50k", "$5,000-$10,000", a range spanning several
    buckets like "$25k-$100k" -- is dropped from this structured field,
    same "never guess a picklist value" rule already used for Chris Degree
    Connection/Age Range/Gender/Engagement Stage. The original CSV text is
    never lost regardless -- it always remains in source_snapshot, and
    _apply_custom_field's list union-merge (crm_service.py) means a
    recognized value here is added to, never replaces, whatever the
    contact already has.
    """
    result: dict[str, Any] = {}

    personal_options = context.get("check_size_personal_options") or set()
    personal_tokens = _split_check_size_tokens(_find_column(raw_row, "Check Size"))
    personal = _ordered_dedup(
        [v for t in personal_tokens if (v := _normalize_check_size_token(t, personal_options))]
    )
    if personal:
        result["custom:check_size_personal"] = personal

    institutional_options = context.get("check_size_institutional_options") or set()
    institutional_tokens = _split_check_size_tokens(_find_column(raw_row, "Check Size (Institutional)"))
    institutional = _ordered_dedup(
        [v for t in institutional_tokens if (v := _normalize_check_size_token(t, institutional_options))]
    )
    if institutional:
        result["custom:check_size_institutional"] = institutional

    return result


# 2026-08-06 broader-audit Phase 2 -- wires the legacy-wording translation
# infrastructure that already existed in crm_migration.py (built for the
# one-time /crm/import/{id}/translate-legacy-values endpoint, but never
# reachable from the normal upload -> preview -> commit flow -- the
# frontend import wizard never calls that endpoint) into the SAME
# always-wins classification-rule mechanism every other permanent fix in
# this project uses. Reuses LEGACY_THESIS_COLUMN_VALUE_MAPS,
# _translate_comma_joined_column, HOW_EARLY_KNOWN_PHRASES, and
# _retokenize_known_phrases verbatim -- no value-map or tokenizer logic is
# reimplemented here.

# CSV column -> target CRM field, for the four legacy thesis columns being wired
# this round. Deliberately excludes "Founder Diversity Preference" (the fifth key
# in LEGACY_THESIS_COLUMN_VALUE_MAPS) -- the 2026-08-06 broader audit found a second
# plausible source for thesis_private_demographic_preferences (the "Would you prefer
# to dine with a gender-specific..." column) that's still an open duplicate-
# destination decision; wiring one candidate now would preempt that decision.
_THESIS_COLUMN_TARGETS: dict[str, str] = {
    "Deal Stage": "thesis_private_deal_stages",
    "Investing in these types of assets": "thesis_private_asset_types",
    "Investing in these business models:": "thesis_private_business_models",
    "Would like to meet founders by": "thesis_private_meeting_preferences",
}


def classify_legacy_thesis_columns(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    Deal Stage / Investing in these types of assets / Investing in these
    business models: / Would like to meet founders by (core Investor
    Thesis list fields, Q11/Q7/Q8/Q12) <- their respective CSV columns,
    translated through crm_migration.py's own legacy-wording maps
    (_DEAL_STAGE_LEGACY_MAP etc., accessed via LEGACY_THESIS_COLUMN_VALUE_MAPS)
    via _translate_comma_joined_column -- the SAME function and maps the
    (previously unreachable) translate-legacy-values endpoint used.

    _translate_comma_joined_column comma-splits the RAW abbreviated text
    first (safe -- no abbreviated token contains its own comma, verified
    against every real value in both CSVs), translates each token through
    the legacy map, THEN joins with semicolons -- so a canonical value that
    itself contains a comma (e.g. "Collectibles (e.g., art, wine,
    watches)") is produced only after the comma-splitting is already done,
    and splitting the semicolon-joined result here can never shred it. An
    unrecognized token is preserved verbatim by that function (never
    dropped, never guessed into the wrong bucket) -- consistent with these
    fields having no fixed option list to validate against.
    """
    from app.services.crm_migration import (  # local import, same rationale as classify_investor_mode
        LEGACY_THESIS_COLUMN_VALUE_MAPS,
        _translate_comma_joined_column,
    )

    result: dict[str, Any] = {}
    for column, target_field in _THESIS_COLUMN_TARGETS.items():
        raw = _find_column(raw_row, column)
        if not raw:
            continue
        legacy_map = LEGACY_THESIS_COLUMN_VALUE_MAPS[column]
        translated = _translate_comma_joined_column(raw, legacy_map)
        tokens = _ordered_dedup([t.strip() for t in translated.split(";") if t.strip()])
        if tokens:
            result[target_field] = tokens
    return result


def classify_how_early_do_you_invest(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """
    how_early_do_you_invest (custom field, multi_select) <- CSV `How early
    do you invest?`, re-tokenized by _retokenize_known_phrases (reused
    verbatim from crm_migration.py) against HOW_EARLY_KNOWN_PHRASES -- two
    of its six live options contain their own internal comma ("Great team,
    no revenue", "Great team, some revenue"), so a naive comma-split would
    shred them into "Great team" + "no revenue" as separate, wrong values.
    _retokenize_known_phrases greedily matches the longest known phrase at
    each position and stops (rather than guessing) on an unrecognized
    fragment -- so a row mixing recognized and unrecognized text keeps only
    the recognized prefix; the raw text is never lost regardless, since it
    always remains in source_snapshot.
    """
    from app.services.crm_migration import (  # local import, same rationale as classify_investor_mode
        HOW_EARLY_COLUMN,
        HOW_EARLY_KNOWN_PHRASES,
        _retokenize_known_phrases,
    )

    raw = _find_column(raw_row, HOW_EARLY_COLUMN)
    if not raw:
        return {}
    tokens = _ordered_dedup(_retokenize_known_phrases(raw, HOW_EARLY_KNOWN_PHRASES))
    return {"custom:how_early_do_you_invest": tokens} if tokens else {}


# Registry of independent classification rules. Each is applied to every
# imported row, in order; later rules win on key collision (none currently
# collide). Add new rules here -- see module docstring.
CLASSIFICATION_RULES: list[Classifier] = [
    classify_industry,
    classify_investor_mode,
    classify_role,
    classify_dinner_subscriptions,
    classify_dinners_attended,
    classify_chris_degree_connection,
    classify_age_range,
    classify_gender,
    classify_engagement_stage,
    classify_check_size,
    classify_accredited_status,
    classify_legacy_thesis_columns,
    classify_how_early_do_you_invest,
]


async def build_classification_context(custom_field_store: Any) -> dict[str, Any]:
    """
    Reference data every rule in CLASSIFICATION_RULES needs, computed ONCE
    per import batch (not per row) and passed to every rule call. Add a key
    here when a new rule needs live reference data (e.g. a fixed options
    list) rather than deriving it fresh per row.
    """

    async def _options(field_key: str) -> set[str]:
        field = await custom_field_store.get_by_field_key(field_key)
        return set(field.options) if field else set()

    role_field = await custom_field_store.get_by_field_key("role")
    return {
        "role_options": set(role_field.options) if role_field else set(),
        "chris_degree_connection_options": await _options("chris_degree_connection"),
        "age_range_options": await _options("age_range"),
        "gender_options": await _options("gender"),
        "engagement_stage_options": await _options("engagement_stage"),
        "check_size_personal_options": await _options("check_size_personal"),
        "check_size_institutional_options": await _options("check_size_institutional"),
        "accredited_status_options": await _options("accredited_status"),
    }


def apply_classification_rules(raw_row: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
    """Runs every registered rule against the raw row and merges their output."""
    result: dict[str, Any] = {}
    for rule in CLASSIFICATION_RULES:
        result.update(rule(raw_row, context))
    return result
