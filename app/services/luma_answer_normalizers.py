"""
Pure value transforms for LumaQuestionMapping.normalizer
(app/models/luma.py's LumaAnswerNormalizer). Deliberately a small, closed
dispatch table of narrowly-typed functions -- NOT a generic transformation
or expression-execution engine. Adding a new normalizer means adding a new
enum member here AND a new function, a conscious code change, never a
data-driven/configurable transform string.

Every normalizer has the same contract: `(value: Any) -> Any | None`.
Returning None means "this value doesn't produce a valid result" -- the
caller (LumaSyncService._build_mapped_fields) must never write None into
mapped_fields, so an unrecognized/invalid/blank answer simply never
reaches the CRM field it was mapped to, rather than writing garbage.
"""

import re
from typing import Any

from app.models.luma import LumaAnswerNormalizer

_LINKEDIN_PATH_PREFIXES = ("linkedin.com", "www.linkedin.com")


def normalize_linkedin_url(value: Any) -> str | None:
    """
    Accepts what Luma's `linkedin`-type registration answers actually look
    like in practice (confirmed against real production data): a bare
    relative path like "/in/example" (the common case), a full URL
    ("https://www.linkedin.com/in/example" or without "www"/scheme), or
    occasionally a path with no leading slash ("in/example"). Returns a
    canonical "https://www.linkedin.com/..." URL in every valid case, or
    None for anything else (blank, unrelated text) -- callers must treat
    None as "do not populate this field", never as an empty-string write.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered.startswith("https://") or lowered.startswith("http://"):
        without_scheme = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    else:
        without_scheme = text

    lowered_without_scheme = without_scheme.lower()
    for prefix in _LINKEDIN_PATH_PREFIXES:
        if lowered_without_scheme.startswith(prefix):
            path = without_scheme[len(prefix) :]
            return f"https://www.linkedin.com{path}" if path.startswith("/") else f"https://www.linkedin.com/{path}"

    if without_scheme.startswith("/in/"):
        return f"https://www.linkedin.com{without_scheme}"
    if without_scheme.startswith("in/"):
        return f"https://www.linkedin.com/{without_scheme}"

    return None  # unrelated/invalid input -- never populate linkedin_url from garbage


_INVESTOR_TYPE_LABEL_TRANSLATIONS = {
    "syndicate lead": "I sponsor deals that I find",
}


def normalize_investor_type_labels(value: Any) -> Any:
    """
    Translates known Luma "Investor Type" labels that don't correspond
    verbatim to an existing CRM option into their CRM equivalent --
    currently just "Syndicate Lead" -> "I sponsor deals that I find".

    Any OTHER value (matched or not) is passed through UNCHANGED. This is
    deliberately NOT where an untranslatable value (e.g. "Fund Manager /
    General Partner", "Corporate Venture") gets dropped -- that's the
    generic CRM option-allowlist filter's job
    (luma_sync_service._filter_to_allowed_options), applied uniformly to
    every controlled CRM select field, not special-cased here.

    Handles both the native multi-select list shape Luma actually sends
    and a bare scalar, defensively.
    """
    if isinstance(value, list):
        return [_INVESTOR_TYPE_LABEL_TRANSLATIONS.get(str(v).strip().lower(), v) for v in value]
    if isinstance(value, str):
        return _INVESTOR_TYPE_LABEL_TRANSLATIONS.get(value.strip().lower(), value)
    return value


# Luma's 5 coarse "typical check size" dropdown buckets translated into the
# CRM's finer-grained custom:check_size_personal taxonomy. Where one Luma
# bucket spans multiple CRM buckets, every CRM bucket it could plausibly
# fall into is included (never approximating down to a single guess) --
# this exact table was specified and approved, not invented here. Keys use
# an EN DASH ("–"), matching Luma's actual answer strings exactly, not
# a hyphen.
_CHECK_SIZE_PERSONAL_BUCKET_TRANSLATIONS = {
    "under $25k": ["$1k - $10k", "$10k - $25k"],
    "$25k–$100k": ["$25k - $50k", "$50k - $100k"],
    "$100k–$250k": ["$100k - $250k"],
    "$250k–$500k": ["$250k - $500k"],
    "$500k+": ["$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+"],
}


def normalize_check_size_personal_bucket(value: Any) -> list[str] | None:
    """
    Translates a Luma "typical check size" dropdown answer into the list
    of custom:check_size_personal CRM options it could plausibly map to.
    An unrecognized Luma value returns None -- skipped, never written as
    an arbitrary/uncontrolled CRM value.
    """
    if not isinstance(value, str):
        return None
    return _CHECK_SIZE_PERSONAL_BUCKET_TRANSLATIONS.get(value.strip().lower())


_NORMALIZERS = {
    LumaAnswerNormalizer.LINKEDIN_URL: normalize_linkedin_url,
    LumaAnswerNormalizer.INVESTOR_TYPE_LABEL: normalize_investor_type_labels,
    LumaAnswerNormalizer.CHECK_SIZE_PERSONAL_BUCKET: normalize_check_size_personal_bucket,
}


def apply_normalizer(normalizer: LumaAnswerNormalizer | None, value: Any) -> Any | None:
    """Dispatches to the one function this normalizer name maps to. An
    unrecognized normalizer (shouldn't happen -- validated at mapping
    create/update time -- but never trusted blindly at ingestion time
    either) is treated as "no valid result", same as a failed transform,
    rather than passing the raw value through unchecked."""
    if normalizer is None:
        return value
    fn = _NORMALIZERS.get(normalizer)
    if fn is None:
        return None
    return fn(value)
