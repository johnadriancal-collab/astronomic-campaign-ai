"""
Deterministic extraction layer for Email Intake (Phase 1) -- see the
architecture audit's categories A ("deterministic/reliable") and B
("deterministic with a controlled alias"). Deliberately does NOT reuse
crm_classification_rules.py: that module's entire contract is "a raw CSV/
Form row keyed by column header," which has no meaning for free-text
email prose. What IS reused, directly, from the existing CRM code rather
than reimplemented:

  - `_normalize_check_size_token` (crm_classification_rules.py) for exact-
    modulo-formatting bucket matching against the field's LIVE options --
    the same "never guess a picklist value" rule CSV import already uses.
  - `_union_merge_list` (crm_service.py) to pre-compute the FINAL merged
    list value a multi-select proposal shows as `proposed_value` -- so
    Approve is always a plain SET, never a second merge decision (see
    email_intake_service.py's module docstring).

Category C (narrative/subjective interpretation -- "he's leaning toward
enterprise software", "she'll probably write around half a million") is
DELIBERATELY not attempted here: no rule below ever produces a field
change from prose that isn't an explicit regex/alias/exact-option match.
This is the ONLY extractor in Phase 1 -- see EmailExtractor below for the
swappable interface a future ClaudeEmailExtractor would implement
identically, without the approval pipeline changing at all.

Design choice on Notes: deliberately NOT implemented in Phase 1. The full
source email is already shown verbatim in the review UI, so a redundant
"append this excerpt to Notes" proposal would only add proposal noise
without adding information a reviewer doesn't already have in front of
them. See the final report for the explicit rationale requested.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.crm import INDUSTRY_OPTIONS, CrmContact
from app.models.email_intake import EmailCrmFieldChange, EmailFieldChangeOperation
from app.repositories.crm_custom_field_store import CrmCustomFieldStore
from app.services.crm_classification_rules import _normalize_check_size_token
from app.services.crm_service import _union_merge_list

CUSTOM_FIELD_PREFIX = "custom:"

# Category B: a small, explicit, auditable set of synonyms -- each maps ONLY
# to a canonical string that already exists in INDUSTRY_OPTIONS (models/crm.py).
# Never invented, never fuzzy -- adding an entry means adding one exact line
# here, and it is trivially auditable end-to-end by reading this dict.
INDUSTRY_ALIASES: dict[str, str] = {
    "ai": "Artificial Intelligence / Machine Learning",
    "artificial intelligence": "Artificial Intelligence / Machine Learning",
    "machine learning": "Artificial Intelligence / Machine Learning",
    "ml": "Artificial Intelligence / Machine Learning",
    "healthcare": "Healthcare & HealthTech",
    "health tech": "Healthcare & HealthTech",
    "healthtech": "Healthcare & HealthTech",
    "fintech": "Fintech (Finance & Insurance)",
    "saas": "SaaS / Software Infrastructure",
    "cybersecurity": "Cybersecurity",
}
for _option in INDUSTRY_OPTIONS:
    INDUSTRY_ALIASES.setdefault(_option.lower(), _option)
assert all(v in INDUSTRY_OPTIONS for v in INDUSTRY_ALIASES.values()), (
    "INDUSTRY_ALIASES must map only to canonical INDUSTRY_OPTIONS values"
)

FIELD_LABELS: dict[str, str] = {
    "email": "Email",
    f"{CUSTOM_FIELD_PREFIX}secondary_email": "Secondary Email",
    "linkedin_url": "LinkedIn URL",
    "phone": "Phone Number",
    "company": "Company",
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_COMPANY_PHRASE_RE = re.compile(r"\b(?:is\s+)?(?:now at|joined|moved to)\s+([^.,;\n]{2,60})", re.IGNORECASE)
_CHECK_SIZE_CANDIDATE_RE = re.compile(r"\$?\d[\d,]*\s*[kKmM]?\s*[-–—]\s*\$?\d[\d,]*\s*[kKmM]?\+?")
_INDUSTRY_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(INDUSTRY_ALIASES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _clean_company_name(raw: str) -> str:
    cleaned = re.split(r"\bas\b", raw, maxsplit=1, flags=re.IGNORECASE)[0]
    return cleaned.strip().rstrip(".").strip()


def _extract_email_change(body_text: str, contact: CrmContact) -> EmailCrmFieldChange | None:
    """
    Only proposes a change when it adds information: if the contact's
    primary email is already set, a body-mentioned email is treated as a
    matching signal only (already consumed by classify_match upstream),
    never re-proposed as a redundant "correct" field change -- a
    secondary/different address is a genuinely distinct proposal, see
    `_extract_secondary_email_change`.
    """
    if contact.email:
        return None
    match = _EMAIL_RE.search(body_text)
    if not match:
        return None
    found = match.group(0)
    return EmailCrmFieldChange(
        field_key="email",
        field_label=FIELD_LABELS["email"],
        operation=EmailFieldChangeOperation.SET,
        current_value=contact.email,
        proposed_value=found,
        source_text=found,
    )


def _extract_secondary_email_change(body_text: str, contact: CrmContact) -> EmailCrmFieldChange | None:
    if not contact.email:
        return None
    key = f"{CUSTOM_FIELD_PREFIX}secondary_email"
    current = contact.custom_fields.get("secondary_email")
    for match in _EMAIL_RE.finditer(body_text):
        found = match.group(0)
        if found.lower() == contact.email.lower():
            continue  # the contact's own already-known address, not new information
        if found == current:
            return None
        return EmailCrmFieldChange(
            field_key=key,
            field_label=FIELD_LABELS[key],
            operation=EmailFieldChangeOperation.SET,
            current_value=current,
            proposed_value=found,
            source_text=found,
        )
    return None


def _extract_linkedin_change(body_text: str, contact: CrmContact) -> EmailCrmFieldChange | None:
    match = _LINKEDIN_RE.search(body_text)
    if not match:
        return None
    found = match.group(0)
    if found == contact.linkedin_url:
        return None
    return EmailCrmFieldChange(
        field_key="linkedin_url",
        field_label=FIELD_LABELS["linkedin_url"],
        operation=EmailFieldChangeOperation.SET,
        current_value=contact.linkedin_url,
        proposed_value=found,
        source_text=found,
    )


def _extract_phone_change(body_text: str, contact: CrmContact) -> EmailCrmFieldChange | None:
    match = _PHONE_RE.search(body_text)
    if not match:
        return None
    found = match.group(0).strip()
    if found == contact.phone:
        return None
    return EmailCrmFieldChange(
        field_key="phone",
        field_label=FIELD_LABELS["phone"],
        operation=EmailFieldChangeOperation.SET,
        current_value=contact.phone,
        proposed_value=found,
        source_text=found,
    )


def _extract_company_change(body_text: str, contact: CrmContact) -> EmailCrmFieldChange | None:
    match = _COMPANY_PHRASE_RE.search(body_text)
    if not match:
        return None
    company = _clean_company_name(match.group(1))
    if not company or company == contact.company:
        return None
    return EmailCrmFieldChange(
        field_key="company",
        field_label=FIELD_LABELS["company"],
        operation=EmailFieldChangeOperation.SET,
        current_value=contact.company,
        proposed_value=company,
        source_text=match.group(0).strip(),
    )


def _extract_industry_change(body_text: str, contact: CrmContact, context: dict[str, Any]) -> EmailCrmFieldChange | None:
    matches = list(dict.fromkeys(m.group(1) for m in _INDUSTRY_ALIAS_RE.finditer(body_text)))
    canonical_hits: list[str] = []
    for raw in matches:
        canonical = INDUSTRY_ALIASES.get(raw.lower())
        if canonical and canonical not in canonical_hits:
            canonical_hits.append(canonical)
    if not canonical_hits:
        return None

    field_key = f"{CUSTOM_FIELD_PREFIX}investment_industry"
    current = contact.custom_fields.get("investment_industry") or []
    current_list = current if isinstance(current, list) else []
    merged = _union_merge_list(current_list, canonical_hits)
    if merged == current_list:
        return None
    label = context.get("investment_industry_label", "Investment Industry")
    return EmailCrmFieldChange(
        field_key=field_key,
        field_label=label,
        operation=EmailFieldChangeOperation.UNION_ADD,
        current_value=current_list,
        proposed_value=merged,
        source_text="; ".join(matches),
    )


def _extract_check_size_change(
    body_text: str, contact: CrmContact, context: dict[str, Any], field_key: str, label_key: str
) -> EmailCrmFieldChange | None:
    options = context.get(f"{field_key}_options") or set()
    if not options:
        return None
    found: list[str] = []
    source_tokens: list[str] = []
    for match in _CHECK_SIZE_CANDIDATE_RE.finditer(body_text):
        token = match.group(0)
        canonical = _normalize_check_size_token(token, options)
        if canonical and canonical not in found:
            found.append(canonical)
            source_tokens.append(token.strip())
    if not found:
        return None

    custom_key = f"{CUSTOM_FIELD_PREFIX}{field_key}"
    current = contact.custom_fields.get(field_key) or []
    current_list = current if isinstance(current, list) else []
    merged = _union_merge_list(current_list, found)
    if merged == current_list:
        return None
    label = context.get(label_key, field_key)
    return EmailCrmFieldChange(
        field_key=custom_key,
        field_label=label,
        operation=EmailFieldChangeOperation.UNION_ADD,
        current_value=current_list,
        proposed_value=merged,
        source_text="; ".join(source_tokens),
    )


async def build_email_extraction_context(custom_field_store: CrmCustomFieldStore) -> dict[str, Any]:
    """
    Live reference data every rule needs, fetched ONCE per extraction call
    (not per rule) -- same "context built once, passed to every rule"
    convention as crm_classification_rules.build_classification_context().
    """

    async def _options_and_label(field_key: str) -> tuple[set[str], str | None]:
        definition = await custom_field_store.get_by_field_key(field_key)
        if not definition:
            return set(), None
        return set(definition.options), definition.label

    investment_options, investment_label = await _options_and_label("investment_industry")
    personal_options, personal_label = await _options_and_label("check_size_personal")
    institutional_options, institutional_label = await _options_and_label("check_size_institutional")
    return {
        "investment_industry_label": investment_label or "Investment Industry",
        "check_size_personal_options": personal_options,
        "check_size_personal_label": personal_label or "Check Size (Personal)",
        "check_size_institutional_options": institutional_options,
        "check_size_institutional_label": institutional_label or "Check Size (Institutional)",
        "_investment_industry_options": investment_options,  # unused today; INDUSTRY_OPTIONS is a fixed global list
    }


class EmailExtractor(Protocol):
    """
    Swappable extraction interface. `DeterministicEmailExtractor` (below)
    is the only implementation in Phase 1. A future `ClaudeEmailExtractor`
    would implement this exact same signature and return the exact same
    `EmailCrmFieldChange` shape -- EmailIntakeService.ingest() never
    branches on which extractor produced a proposal, and Claude (if ever
    wired in here) would still never gain CRM write authority: every
    proposal, from any extractor, is subject to the identical human-
    approval/stale-check pipeline in email_intake_service.py.
    """

    async def extract(self, body_text: str, contact: CrmContact, context: dict[str, Any]) -> list[EmailCrmFieldChange]:
        ...


@dataclass
class DeterministicEmailExtractor:
    """Category A + B only -- see this module's docstring. Never touches
    Notes; never attempts category-C narrative interpretation."""

    async def extract(self, body_text: str, contact: CrmContact, context: dict[str, Any]) -> list[EmailCrmFieldChange]:
        changes: list[EmailCrmFieldChange] = []
        for change in (
            _extract_email_change(body_text, contact),
            _extract_secondary_email_change(body_text, contact),
            _extract_linkedin_change(body_text, contact),
            _extract_phone_change(body_text, contact),
            _extract_company_change(body_text, contact),
            _extract_industry_change(body_text, contact, context),
            _extract_check_size_change(body_text, contact, context, "check_size_personal", "check_size_personal_label"),
            _extract_check_size_change(
                body_text, contact, context, "check_size_institutional", "check_size_institutional_label"
            ),
        ):
            if change is not None:
                changes.append(change)
        return changes
