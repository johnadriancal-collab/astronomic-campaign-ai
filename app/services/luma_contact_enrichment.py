"""
Luma -> CrmContact self-reported Company/Job Title enrichment, plus a
Company Website resolver. Deliberately separate from apply_import_mapping()
and the generic LumaQuestionMapping system (see this module's own
architecture report) -- a different, higher-confidence merge policy
(nonblank self-report REPLACES, not fill-only) deserves its own code path,
not a flag bolted onto the CSV-import contract every other pipeline relies
on staying exactly as-is.

Extraction is STRUCTURAL, never label-based: Luma's registration form has
a built-in `question_type == "company"` question whose `value` is
`{"company": ..., "job_title": ...}` (confirmed live in Luma's payload
shape and already exercised by app/services/luma_sync_service.py's own
tests). This does NOT depend on the organizer-visible question label,
which can vary per event and isn't a stable identifier -- unlike the
generic LumaQuestionMapping system (label-matched, admin-configured),
nothing here is configuration-dependent.

Recency algorithm ("most recent valid self-reported value wins",
independently per field): registered_at -> joined_at -> associated
LumaEvent.start_at -> UNKNOWN. `LumaRegistration.updated_at`/`synced_at`
are deliberately NEVER used as a recency signal -- they're AstroHub's own
persistence/sync timestamps, stamped to "now" on every save including a
backfill/reconciliation run, so using them would make an old registration
look artificially "newer" than it really is purely because of when this
app happened to (re)process it. An UNKNOWN-recency answer can be adopted
only when nothing else is available for that field (single valid answer,
no known-recency competitor); if 2+ UNKNOWN-recency answers disagree with
no known-recency tiebreaker, resolution deliberately FAILS CONSERVATIVE --
no guess, the field is flagged (`ambiguous_unknown`) and left untouched.

Regression safety ("a late-arriving webhook for an older registration must
never regress a newer self-report") comes from ALWAYS recomputing a
contact's resolution from its COMPLETE current set of stored
LumaRegistration rows, not from comparing one new registration against a
remembered "current winner". This is deliberately more robust than an
incremental/pairwise comparison: an old registration processed late can
never "look newer" than a registration that's also already stored,
because both are simply inputs to the exact same batch-max computation,
in whatever order it's called. The live webhook path and the historical
backfill therefore call the EXACT SAME functions here -- there is no
separate, subtly different "backfill algorithm".

Provenance is intentionally minimal -- no new CrmContact model field, no
general-purpose framework. A small `custom_fields["field_provenance"]`
dict (see FIELD_PROVENANCE_KEY) records, per field this module actually
CHANGES (company/title/company_website), the source, the winning
registration's own luma_guest_id (already a persisted, non-secret
identifier -- never raw payload, never email), and the recency tier/
timestamp used. This is audit/display information; correctness of the
"no regression" guarantee does NOT depend on it (see above). Provenance
for a field is written ONLY when that field's VALUE actually changes as
a result of THIS call -- never merely because Luma's current answer
happens to already match what's stored. A Contact whose Luma-resolved
Company/Title/Website all already match its current values produces ZERO
updates and ZERO provenance mutation: we have no way to know the
historical source of an already-identical value, so this module makes no
claim about it.

Company Website (V1, corrected): an EXISTING nonblank company_website is
NEVER automatically cleared or overwritten by this module, even when
Company changes -- without reliable provenance on that existing website
(manually verified? imported? inferred? tied to the old company?
still valid after a rename/rebrand?), silently clearing it on a Company
change is a destructive guess this module refuses to make. When Company
changes materially against a PREVIOUSLY non-blank company AND an existing
website is present, the website is left completely untouched and the
outcome is marked `website_review_needed=True` instead -- a review queue,
not a cleanup. Tier 1 (internal same-company knowledge) may ONLY populate
a company_website that is CURRENTLY BLANK -- an unambiguous single
normalized-domain match among other non-archived contacts sharing the
same normalized company. Tier 2 (corporate email-domain candidate) is
fully implemented and evaluable but is NEVER applied to
CrmContact.company_website anywhere in this module -- callers (the
backfill report, in particular) may compute and SURFACE a Tier 2
candidate for human review, but must not write it. This mirrors the
explicit, not-yet-approved status of Tier 2 automatic writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from app.models.crm import CrmContact
from app.models.luma import LumaEvent, LumaRegistration

FIELD_PROVENANCE_KEY = "field_provenance"
LUMA_SELF_REPORT_SOURCE = "luma_self_report"
WEBSITE_SOURCE_INTERNAL_MATCH = "internal_company_match"

# Luma's own free/personal email providers -- conservative and short on
# purpose (see architecture report): anything NOT on this list is treated
# as a Tier 2 CANDIDATE (lower confidence than Tier 1), never a certainty.
FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.ca",
        "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
        "icloud.com", "me.com", "mac.com", "aol.com",
        "protonmail.com", "proton.me", "pm.me",
        "gmx.com", "gmx.net", "mail.com", "yandex.com", "zoho.com", "fastmail.com",
    }
)

_LEGAL_SUFFIX_WORDS = frozenset({"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company"})


class RecencyTier(str, Enum):
    REGISTERED_AT = "registered_at"
    JOINED_AT = "joined_at"
    EVENT_START_AT = "event_start_at"
    UNKNOWN = "unknown"


_TIER_PRIORITY: dict[RecencyTier, int] = {
    RecencyTier.REGISTERED_AT: 3,
    RecencyTier.JOINED_AT: 2,
    RecencyTier.EVENT_START_AT: 1,
    RecencyTier.UNKNOWN: 0,
}


def _ensure_aware(dt: datetime) -> datetime:
    """Defensive only -- every timestamp this module compares should
    already be timezone-aware (Pydantic/Luma's own ISO8601 payloads), but
    a naive value would raise on comparison rather than silently
    misbehave; treat a naive value as UTC rather than crash a webhook."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class FieldCandidate:
    value: str
    tier: RecencyTier
    recency_at: datetime | None
    luma_guest_id: str


@dataclass(frozen=True)
class ResolvedField:
    value: str
    tier: RecencyTier
    recency_at: datetime | None
    luma_guest_id: str


@dataclass(frozen=True)
class FieldResolution:
    resolved: ResolvedField | None
    ambiguous_unknown: bool  # 2+ UNKNOWN-recency candidates, no known-recency one -- deliberately unresolved
    candidate_count: int  # total valid nonblank candidates seen (known + unknown)


@dataclass(frozen=True)
class ContactFieldResolutions:
    company: FieldResolution
    title: FieldResolution


@dataclass(frozen=True)
class RegistrationRecency:
    tier: RecencyTier
    recency_at: datetime | None


@dataclass(frozen=True)
class Tier1Result:
    candidate: str | None  # canonical "https://domain", or None
    ambiguous: bool  # 2+ distinct normalized domains among matching contacts
    matching_contact_count: int  # non-archived contacts sharing the normalized company AND having a nonblank website


@dataclass(frozen=True)
class Tier2Result:
    candidate: str | None  # canonical "https://domain", or None
    excluded_free_domain: str | None  # set when a domain existed but was excluded as free/personal


@dataclass(frozen=True)
class LumaEnrichmentOutcome:
    contact: CrmContact
    changed_field_keys: list[str] = field(default_factory=list)
    ambiguous_fields: list[str] = field(default_factory=list)  # "company" and/or "title"
    # An existing, nonblank company_website was left untouched (never
    # cleared/overwritten -- see module docstring) despite a material
    # Company change -- a signal for human review, not an action taken.
    website_review_needed: bool = False
    website_tier1_ambiguous: bool = False


# --- structural extraction (question_type == "company", never label-based) -


def extract_company_question_answer(registration: LumaRegistration) -> tuple[str | None, str | None]:
    """Returns (company, job_title), each trimmed and None if blank/
    missing/malformed. Matches Luma's OWN structural question_type, never
    the organizer-configurable label. If more than one such answer exists
    on a single registration (not expected per Luma's form design, but
    handled defensively), the FIRST one encountered wins, deterministically."""
    for answer in registration.registration_answers:
        if answer.question_type != "company":
            continue
        value = answer.value
        if not isinstance(value, dict):
            continue
        return _clean_str(value.get("company")), _clean_str(value.get("job_title"))
    return None, None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


# --- recency -----------------------------------------------------------------


def compute_registration_recency(registration: LumaRegistration, event: LumaEvent | None) -> RegistrationRecency:
    """registered_at -> joined_at -> event.start_at -> UNKNOWN. Deliberately
    never falls back to registration.updated_at/synced_at -- see module
    docstring."""
    if registration.registered_at is not None:
        return RegistrationRecency(RecencyTier.REGISTERED_AT, _ensure_aware(registration.registered_at))
    if registration.joined_at is not None:
        return RegistrationRecency(RecencyTier.JOINED_AT, _ensure_aware(registration.joined_at))
    if event is not None and event.start_at is not None:
        return RegistrationRecency(RecencyTier.EVENT_START_AT, _ensure_aware(event.start_at))
    return RegistrationRecency(RecencyTier.UNKNOWN, None)


def _candidates_for_field(
    registrations: list[LumaRegistration], events_by_id: dict[str, LumaEvent], *, want_title: bool
) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for reg in registrations:
        company, job_title = extract_company_question_answer(reg)
        value = job_title if want_title else company
        if value is None:
            continue
        recency = compute_registration_recency(reg, events_by_id.get(reg.luma_event_id))
        candidates.append(FieldCandidate(value=value, tier=recency.tier, recency_at=recency.recency_at, luma_guest_id=reg.luma_guest_id))
    return candidates


def resolve_field(candidates: list[FieldCandidate]) -> FieldResolution:
    """Batch, order-independent resolution over ALL candidates for one
    field. Known-recency candidates always beat unknown-recency ones,
    regardless of how many of either exist. Among known-recency
    candidates: highest tier priority wins; ties broken by later
    recency_at; an EXACT tie (same tier AND same recency_at) is broken by
    the lexicographically SMALLEST luma_guest_id -- deterministic and
    stable across reruns (see test_luma_contact_enrichment.py's tie test).
    A single unknown-recency candidate with nothing else to compare
    against is adopted (best available information). 2+ unknown-recency
    candidates with no known-recency one to arbitrate: deliberately
    unresolved (ambiguous_unknown=True) -- this module never guesses."""
    if not candidates:
        return FieldResolution(resolved=None, ambiguous_unknown=False, candidate_count=0)

    known = [c for c in candidates if c.tier != RecencyTier.UNKNOWN]
    unknown = [c for c in candidates if c.tier == RecencyTier.UNKNOWN]

    if known:
        best = _pick_best_known(known)
        return FieldResolution(
            resolved=ResolvedField(value=best.value, tier=best.tier, recency_at=best.recency_at, luma_guest_id=best.luma_guest_id),
            ambiguous_unknown=False,
            candidate_count=len(candidates),
        )

    if len(unknown) == 1:
        only = unknown[0]
        return FieldResolution(
            resolved=ResolvedField(value=only.value, tier=only.tier, recency_at=None, luma_guest_id=only.luma_guest_id),
            ambiguous_unknown=False,
            candidate_count=1,
        )

    return FieldResolution(resolved=None, ambiguous_unknown=True, candidate_count=len(unknown))


def _pick_best_known(known: list[FieldCandidate]) -> FieldCandidate:
    best_sort_key = max((_TIER_PRIORITY[c.tier], c.recency_at) for c in known)
    tied = [c for c in known if (_TIER_PRIORITY[c.tier], c.recency_at) == best_sort_key]
    return min(tied, key=lambda c: c.luma_guest_id)


def resolve_contact_luma_fields(
    registrations: list[LumaRegistration], events_by_id: dict[str, LumaEvent]
) -> ContactFieldResolutions:
    """The one shared resolution function -- called identically by the
    live webhook path (with the contact's COMPLETE current registration
    set, always re-fetched fresh, never an incremental subset) and the
    historical backfill (with the same complete set, examined all at
    once). Company and Title are resolved completely independently."""
    return ContactFieldResolutions(
        company=resolve_field(_candidates_for_field(registrations, events_by_id, want_title=False)),
        title=resolve_field(_candidates_for_field(registrations, events_by_id, want_title=True)),
    )


# --- company-name / website normalization (scoped to this feature only) -----


def normalize_company_name(value: str | None) -> str:
    """Local to THIS feature only -- not a repo-wide company normalizer
    (none exists elsewhere; see architecture report). Used only to decide
    "is this materially the same company" (stale-website invalidation) and
    to group contacts for Tier 1 website matching -- never to alter what's
    actually stored in .company, which always keeps the self-reported text
    verbatim. Lowercases, strips punctuation, and drops a short list of
    trailing legal-entity suffix words (Inc/LLC/Ltd/Corp/Co/Company)."""
    if not value:
        return ""
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    while tokens and tokens[-1] in _LEGAL_SUFFIX_WORDS:
        tokens.pop()
    return " ".join(tokens)


def normalize_website_domain(value: str | None) -> str | None:
    """Extracts a lowercase, "www."-stripped host from a URL or bare
    domain string -- enough to treat "https://acme.com",
    "http://www.acme.com/", and "acme.com" as the SAME Tier 1 grouping
    key, without a public-suffix-list dependency this repo doesn't have.
    Returns None for a blank/unparseable value."""
    if not value:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if "://" not in v:
        v = f"//{v}"  # let urlparse treat a bare "acme.com[/path]" as a netloc
    parsed = urlparse(v)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.split(":")[0]  # drop a port if present
    if host.startswith("www."):
        host = host[4:]
    return host or None


def canonical_website_url(value: str | None) -> str | None:
    """The stored representation for an auto-resolved company_website --
    always "https://{domain}", domain-only. Deliberately does not
    reproduce the original scheme/path/trailing slash of whichever
    contact record it was copied from."""
    domain = normalize_website_domain(value)
    return f"https://{domain}" if domain else None


def extract_email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.strip().lower().rsplit("@", 1)[-1].strip()
    return domain or None


def is_free_email_domain(domain: str | None) -> bool:
    return bool(domain) and domain in FREE_EMAIL_DOMAINS


# --- Company Website resolver -------------------------------------------------


def resolve_tier1_website(
    normalized_company: str, contacts: list[CrmContact], *, exclude_crm_contact_id: str | None = None
) -> Tier1Result:
    """Pure -- caller supplies the candidate contact pool (does its own
    store I/O). Exactly one unambiguous distinct normalized domain among
    non-archived contacts sharing `normalized_company` -> that candidate.
    Zero matches -> None, not ambiguous. 2+ distinct domains -> ambiguous,
    no candidate selected (never guesses which one is right)."""
    if not normalized_company:
        return Tier1Result(candidate=None, ambiguous=False, matching_contact_count=0)

    domains: set[str] = set()
    matches = 0
    for c in contacts:
        if c.archived or (exclude_crm_contact_id and c.crm_contact_id == exclude_crm_contact_id):
            continue
        if normalize_company_name(c.company) != normalized_company:
            continue
        domain = normalize_website_domain(c.company_website)
        if domain:
            matches += 1
            domains.add(domain)

    if not domains:
        return Tier1Result(candidate=None, ambiguous=False, matching_contact_count=matches)
    if len(domains) > 1:
        return Tier1Result(candidate=None, ambiguous=True, matching_contact_count=matches)
    return Tier1Result(candidate=f"https://{next(iter(domains))}", ambiguous=False, matching_contact_count=matches)


def resolve_tier2_email_domain_candidate(email: str | None) -> Tier2Result:
    """NEVER authorized to write CrmContact.company_website in this stage
    -- report/evaluate only (see apply_luma_self_report, which never
    reads this function's output). Excludes Luma's/this module's
    conservative free-email-provider list."""
    domain = extract_email_domain(email)
    if not domain:
        return Tier2Result(candidate=None, excluded_free_domain=None)
    if is_free_email_domain(domain):
        return Tier2Result(candidate=None, excluded_free_domain=domain)
    return Tier2Result(candidate=f"https://{domain}", excluded_free_domain=None)


# --- provenance ---------------------------------------------------------------


def _provenance_entry_luma(resolved: ResolvedField) -> dict[str, Any]:
    return {
        "source": LUMA_SELF_REPORT_SOURCE,
        "luma_guest_id": resolved.luma_guest_id,
        "recency_tier": resolved.tier.value,
        "recency_at": resolved.recency_at.isoformat() if resolved.recency_at else None,
    }


def _provenance_entry_website_tier1(matched_normalized_company: str) -> dict[str, Any]:
    return {"source": WEBSITE_SOURCE_INTERNAL_MATCH, "matched_company": matched_normalized_company}


# --- the merge decision (used identically by webhook + backfill) ------------


def resolved_company_if_material_change(contact: CrmContact, resolutions: ContactFieldResolutions) -> str | None:
    """Returns the NEW company value only if this resolution represents a
    genuine, material change to a PREVIOUSLY NON-BLANK company (the
    OldCo -> NewCo scenario that makes an existing company_website worth
    flagging for review) -- None if there's no resolution, an ambiguous
    unresolved case, no actual change, a purely cosmetic difference under
    normalize_company_name, or the company was simply blank before (a
    first-time fill is not a "change" -- nothing to flag)."""
    if resolutions.company.ambiguous_unknown or resolutions.company.resolved is None:
        return None
    new_value = resolutions.company.resolved.value
    original = contact.company
    if not original or new_value == original:
        return None
    if normalize_company_name(new_value) == normalize_company_name(original):
        return None
    return new_value


def apply_luma_self_report(
    contact: CrmContact, resolutions: ContactFieldResolutions, *, tier1_result: Tier1Result | None = None
) -> LumaEnrichmentOutcome:
    """Pure, synchronous, no I/O -- the one merge function used by both
    the live webhook path and the historical backfill.

    Rules: blank/missing Luma values never erase; a resolved nonblank
    value that DIFFERS from what's currently stored ALWAYS replaces it
    (unlike apply_import_mapping's fill-only rule, untouched by this
    module); company and title are independent. Provenance for a field is
    written ONLY alongside an actual value change for that field -- never
    merely because Luma's answer already matches what's stored. A Contact
    with no actual field changes returns changed_field_keys=[] (zero
    Contact save, per this function's own "if not updates" early-out).

    Company Website (V1): an existing NONBLANK company_website is never
    touched, regardless of whether Company changes -- if Company changes
    materially against a previously non-blank one, `website_review_needed`
    is set instead of writing anything. Tier 1 may only populate a
    CURRENTLY BLANK company_website (whether Company just changed or
    merely reconfirmed its existing value) -- `tier1_result`, if provided,
    is consulted only in that blank-website case. Tier 2 is never applied
    here at all. Callers should compute `tier1_result` lazily -- only when
    `contact.company_website` is already blank AND
    `resolutions.company.resolved` is not None -- rather than always
    paying for the contact-pool fetch."""
    updates: dict[str, Any] = {}
    ambiguous_fields: list[str] = []
    provenance = dict(contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {})
    website_review_needed = False
    website_tier1_ambiguous = False

    if resolutions.company.ambiguous_unknown:
        ambiguous_fields.append("company")
    elif resolutions.company.resolved is not None:
        resolved = resolutions.company.resolved
        if resolved.value != (contact.company or ""):
            updates["company"] = resolved.value
            entry = _provenance_entry_luma(resolved)
            if provenance.get("company") != entry:
                provenance["company"] = entry

        # Website handling runs whenever Luma resolved SOME company this
        # round -- independent of whether that value differs from what's
        # already stored (reconfirming an existing company should still
        # let a currently-blank website get filled).
        if contact.company_website:
            if resolved_company_if_material_change(contact, resolutions) is not None:
                website_review_needed = True
        elif tier1_result is not None:
            if tier1_result.candidate:
                updates["company_website"] = tier1_result.candidate
                provenance["company_website"] = _provenance_entry_website_tier1(normalize_company_name(resolved.value))
            elif tier1_result.ambiguous:
                website_tier1_ambiguous = True

    if resolutions.title.ambiguous_unknown:
        ambiguous_fields.append("title")
    elif resolutions.title.resolved is not None:
        resolved = resolutions.title.resolved
        if resolved.value != (contact.title or ""):
            updates["title"] = resolved.value
            entry = _provenance_entry_luma(resolved)
            if provenance.get("title") != entry:
                provenance["title"] = entry

    if provenance != (contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {}):
        updates["custom_fields"] = {**contact.custom_fields, FIELD_PROVENANCE_KEY: provenance}

    if not updates:
        return LumaEnrichmentOutcome(
            contact=contact, changed_field_keys=[], ambiguous_fields=ambiguous_fields,
            website_review_needed=website_review_needed, website_tier1_ambiguous=website_tier1_ambiguous,
        )

    changed_field_keys = sorted({"custom:field_provenance" if k == "custom_fields" else k for k in updates})
    updates["updated_at"] = datetime.now(timezone.utc)
    updated_contact = contact.model_copy(update=updates)
    return LumaEnrichmentOutcome(
        contact=updated_contact,
        changed_field_keys=changed_field_keys,
        ambiguous_fields=ambiguous_fields,
        website_review_needed=website_review_needed,
        website_tier1_ambiguous=website_tier1_ambiguous,
    )
