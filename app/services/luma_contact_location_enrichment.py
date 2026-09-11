"""
Luma Event structured geo -> CrmContact city/state/country fill-only
enrichment (Client CRM Stage 6B.2). Deliberately separate from
luma_contact_enrichment.py (Company/Job Title self-report) -- that
module's merge policy is REPLACE-if-different (a higher-confidence,
self-reported signal, the person telling us directly). This signal is the
opposite: lower-confidence and inferred (derived only from which dinner
someone registered for), so it uses FILL-ONLY-IF-BLANK instead -- the
SAME merge policy apply_import_mapping() already uses for these same
three fields on CSV import. It does not share code with
luma_contact_enrichment.py beyond FIELD_PROVENANCE_KEY (the existing,
already-3x-reused custom_fields["field_provenance"] convention) -- no
recency/tie-break machinery is needed here, since this module is scoped
to a single registration/event pair, never a batch across a contact's
full registration history.

Eligibility (locked product decision): only a LumaRegistration whose
`registered_at` is not None is eligible -- see
app/services/luma_decline_origin.py's own precedent for why
`registered_at`, not `approval_status`/`invited_at`, is the correct
"did this person actually express intent to attend" signal. Someone
merely invited (`registered_at` null), even if later declined, is never
eligible -- regardless of the final approval/rsvp status. Someone who
registered and was LATER declined BY THE HOST remains eligible, since
they did actively register; final RSVP status is never the deciding
factor.

Source: LumaEvent's own structured geo fields (`location_city`/
`location_region`/`location_country` -- see app/models/luma.py and Stage
6B.1), never `Engagement.location` (a single free-text field, never
synced from Luma, entered manually) and never
`LumaEvent.location_summary` (a lossy, city-only, best-effort legacy
field). No free-text parsing of any kind, in either direction.

city/state/country are filled entirely independently -- a field already
non-blank on the Contact is never touched, regardless of whether the
other two get filled this round, and partial event geo (e.g. city+region
present, country absent) fills only what's actually available. "First
fill wins" (V1's documented limitation: a later, different dinner's
location never overwrites an already-filled field) falls out naturally
from "only ever touch a currently-blank field" -- this module tracks no
separate history to enforce it explicitly.

Provenance: the same custom_fields["field_provenance"] mechanism
luma_contact_enrichment.py already established (imported from there, not
redefined), written independently per field, ONLY for a field this call
actually fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models.crm import CrmContact
from app.models.luma import LumaEvent, LumaRegistration
from app.services.luma_contact_enrichment import FIELD_PROVENANCE_KEY

LUMA_EVENT_LOCATION_SOURCE = "luma_event_location"

# Luma's geo_address_json.country is an ISO-3166-1 alpha-2 code (e.g.
# "US") -- confirmed against a real production payload (Stage 6B
# investigation; see tests/test_luma_response_shapes.py's own
# REAL_SHAPED_EVENT_ENTRY fixture, itself confirmed by direct live API
# calls against Astronomic's production Luma calendar). No other shape
# has ever been observed. AstroHub's own CrmContact.country convention
# was confirmed by a read-only production check (2026-09-11: 2342
# non-blank values, "United States" x2298, plus a long tail of other
# full English country names -- zero ISO codes anywhere) to be full
# English country names, never codes.
#
# No ISO-3166 conversion dependency exists anywhere in this backend
# (confirmed 2026-09-11 by enumerating every installed package in
# .venv/lib/*/site-packages -- fastapi/pydantic/boto3/cryptography/
# Pillow/loguru/aiosqlite and their transitive dependencies only; no
# pycountry, babel, phonenumbers, or iso3166 anywhere) and requirements.txt
# declares none either. Per this review's own explicit rule, a new
# dependency is NOT added here without a separate approval -- this
# hand-written table remains the V1 design. KNOWN LIMITATION: covers only
# the countries already present in production plus a handful of common
# additions (23 total); a Luma event geo-tagged to a country outside this
# table fills nothing for `country` (city/state, if present, still fill
# independently) until this table is extended.
#
# This table normalizes an incoming Luma code to EXACTLY the spelling
# already used in production for that country -- never inventing a new,
# third spelling. A code with no entry here is deliberately left
# unfilled (conservative: never write a raw, unnormalized code that
# would create a second, inconsistent representation of the same country
# alongside the existing convention) -- extend this table if/when a new
# country genuinely needs it.
_COUNTRY_CODE_TO_NAME: dict[str, str] = {
    "US": "United States",
    "IL": "Israel",
    "GB": "United Kingdom",
    "MX": "Mexico",
    "PR": "Puerto Rico",
    "CA": "Canada",
    "NL": "Netherlands",
    "KR": "South Korea",
    "GR": "Greece",
    "UA": "Ukraine",
    "ES": "Spain",
    "BZ": "Belize",
    "HK": "Hong Kong",
    "PK": "Pakistan",
    "AU": "Australia",
    "AR": "Argentina",
    "IN": "India",
    "DK": "Denmark",
    "SG": "Singapore",
    "PT": "Portugal",
    "DE": "Germany",
    "CH": "Switzerland",
    "CO": "Colombia",
}

# The exact CrmContact field <-> LumaEvent structured-geo field pairing.
# Deliberately explicit (not zipped by position) so a reader can see the
# mapping without cross-referencing field order in two different models.
_LOCATION_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("city", "location_city"),
    ("state", "location_region"),
    ("country", "location_country"),
)


# The set of full-name spellings this table itself produces -- reused
# below so an incoming value that ALREADY matches AstroHub's own
# convention exactly (e.g. Luma someday sending "United States" instead
# of "US") is preserved rather than discarded. No evidence this has ever
# happened (see module docstring -- the one confirmed live fixture is a
# 2-letter code) -- this is a defensive, not observed, case.
_KNOWN_FULL_NAMES: frozenset[str] = frozenset(_COUNTRY_CODE_TO_NAME.values())


def normalize_country_code(raw: str | None) -> str | None:
    """Maps a Luma country value to AstroHub's existing full-name
    Contact.country convention. Two recognized shapes: (1) an
    ISO-3166-1 alpha-2 code (Luma's confirmed, only-ever-observed
    shape) -- looked up in _COUNTRY_CODE_TO_NAME; (2) a value that
    ALREADY EXACTLY matches one of that table's own full-name outputs --
    passed through unchanged, so a hypothetical future full-name payload
    is never wrongly rejected. Anything else (an unrecognized code, an
    unrecognized or malformed string, blank) returns None -- never the
    raw input itself, conservative by design, see module docstring."""
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned in _KNOWN_FULL_NAMES:
        return cleaned
    return _COUNTRY_CODE_TO_NAME.get(cleaned.upper())


@dataclass(frozen=True)
class LumaLocationEnrichmentOutcome:
    contact: CrmContact
    changed_field_keys: list[str] = field(default_factory=list)


def is_eligible_for_location_enrichment(registration: LumaRegistration) -> bool:
    """Locked eligibility: `registered_at is not None`. Independent of
    `approval_status`/rsvp -- a person who registered and was later
    declined by the host is still eligible; a person who was only ever
    invited (`registered_at` null) never is, regardless of final state."""
    return registration.registered_at is not None


def apply_luma_event_location_enrichment(
    contact: CrmContact,
    luma_event: LumaEvent,
    registration: LumaRegistration,
    *,
    engagement_id: str | None = None,
) -> LumaLocationEnrichmentOutcome:
    """Pure, synchronous, no I/O -- mirrors the calling convention of
    luma_contact_enrichment.py's own merge function. Returns the contact UNCHANGED
    (changed_field_keys == []) whenever: the registration isn't eligible
    (see is_eligible_for_location_enrichment), the LumaEvent carries no
    structured geo data at all, or every field a fillable value exists
    for is already non-blank on the contact -- a genuine, silent no-op in
    all three cases, never an error.

    `engagement_id`, when the caller has one (the Luma event happens to
    be linked to a Client CRM Engagement), is recorded in provenance
    only -- this function never requires it, never looks anything up
    itself, and enriches identically whether or not it's given; the
    caller decides whether/how to resolve it (see
    LumaSyncService._apply_luma_location_enrichment)."""
    if not is_eligible_for_location_enrichment(registration):
        return LumaLocationEnrichmentOutcome(contact=contact, changed_field_keys=[])

    candidate_values = {
        "city": luma_event.location_city,
        "state": luma_event.location_region,
        "country": normalize_country_code(luma_event.location_country),
    }
    if not any(candidate_values.values()):
        return LumaLocationEnrichmentOutcome(contact=contact, changed_field_keys=[])

    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {}
    provenance = dict(contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {})

    for contact_field, _luma_field in _LOCATION_FIELD_MAP:
        candidate = candidate_values[contact_field]
        if not candidate:
            continue  # this field wasn't available on the event at all
        if getattr(contact, contact_field):
            continue  # fill-only -- never overwrite an existing value
        updates[contact_field] = candidate
        entry: dict[str, Any] = {
            "source": LUMA_EVENT_LOCATION_SOURCE,
            "luma_event_id": luma_event.luma_event_id,
            "luma_guest_id": registration.luma_guest_id,
            "inferred_at": now.isoformat(),
        }
        if engagement_id:
            entry["engagement_id"] = engagement_id
        provenance[contact_field] = entry

    if not updates:
        return LumaLocationEnrichmentOutcome(contact=contact, changed_field_keys=[])

    if provenance != (contact.custom_fields.get(FIELD_PROVENANCE_KEY) or {}):
        updates["custom_fields"] = {**contact.custom_fields, FIELD_PROVENANCE_KEY: provenance}

    changed_field_keys = sorted({"custom:field_provenance" if k == "custom_fields" else k for k in updates})
    updates["updated_at"] = now
    updated_contact = contact.model_copy(update=updates)
    return LumaLocationEnrichmentOutcome(contact=updated_contact, changed_field_keys=changed_field_keys)
