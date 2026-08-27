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


_NORMALIZERS = {
    LumaAnswerNormalizer.LINKEDIN_URL: normalize_linkedin_url,
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
