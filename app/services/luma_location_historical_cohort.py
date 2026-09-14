"""
Stage 6C (2026-09-14) -- the EXPLICIT, frozen, human-reviewed write cohort
for historical Luma location reconciliation. This is data, not logic: the
17 rows below are copied verbatim from the approved Stage 6C Phase 1
investigation report (and its Phase 1 clarification pass) -- nothing here
is computed, scanned, or derived at runtime.

This module intentionally has NO function that discovers additional
candidates. The write-eligible universe for Stage 6C is exactly
FROZEN_COHORT, forever, until a human explicitly edits this file and a
NEW investigation/review cycle approves the change -- see
app/services/luma_location_historical_reconciliation.py's own docstring
for why the write path can only ever touch these exact rows.

Each row's `expected_values` are the EXACT structured-geo values observed
on its `luma_event_id` at investigation time (already normalized to
AstroHub's own Contact.country convention, e.g. "US" -> "United States")
-- these are compared against the LIVE event data immediately before any
write (see _revalidate_and_prepare()'s "check 10"), so a row is skipped
as drift rather than trusted blindly if the underlying event data were
ever to change.

The one Contact identified as blocked (zero structured geo on its only
historical event, evt-hKRtbvrUo3tp1ND) is deliberately NOT a row here --
crm_contact_id 155132ee-cc78-4de7-b3fb-601f8d8df344 does not appear
below, and this module has no mechanism to add it without a code change
and a new review.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FrozenReconciliationRow:
    crm_contact_id: str
    luma_guest_id: str
    luma_event_id: str
    engagement_id: str | None
    fields: tuple[str, ...]  # subset of ("city", "state", "country"), approved for THIS row only
    expected_values: dict[str, str] = field(default_factory=dict)  # field -> expected normalized value


# --- source event structured geo, for reference only (baked into each row's own expected_values below) ---
# evt-ZWviVlhfy5Wstc9 ("Hot Shot Investor Dinner ATX"):     city=Austin,        state=Texas,      country=United States
# evt-d2LBzV0sWzTxoFD ("Hive Asmbld Investor Dinner SF"):   city=San Francisco, state=California,  country=United States
# evt-LtlLFXz5Gm3HnjO ("Applied Curiosity Investor Dinner"):city=San Francisco, state=California,  country=United States

_ATX = {"city": "Austin", "state": "Texas", "country": "United States"}
_SF = {"city": "San Francisco", "state": "California", "country": "United States"}

FROZEN_COHORT: tuple[FrozenReconciliationRow, ...] = (
    FrozenReconciliationRow(
        crm_contact_id="e0aeae6e-8ea2-4194-982f-e8a345e67354",
        luma_guest_id="gst-3OeIiS29yTUad3F",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="b3990576-fea9-44a2-bab3-c276afe3a71b",
        luma_guest_id="gst-7KK6I09bECH8sG6",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="40d226df-8276-4739-82db-6a5350df881f",
        luma_guest_id="gst-Fz9GTULMhWoUddI",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="73f9433d-4c64-42e1-a2e3-05644520db99",
        luma_guest_id="gst-SRRPSuUZTbOMmgz",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="131d22b4-e336-4c84-89a1-2048b6c4b693",
        luma_guest_id="gst-SCT0vQcOuc7L0yM",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="d019e0c1-62f6-438c-a236-ecbbe36ff18a",
        luma_guest_id="gst-2CFtBM8XrSHIX6p",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="3b05e615-9a6f-4711-8536-652bc5a20dba",
        luma_guest_id="gst-uztKIjgfG6waZr6",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="d55a0095-b690-42be-81ca-e540d31db922",
        luma_guest_id="gst-NF36LPSpA6B2F6R",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="44e07581-7e87-4ff0-b955-7506fc800744",
        luma_guest_id="gst-Ss5ocOooRwP7wsb",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="6a7da971-5e56-4aec-a051-f3a047349431",
        luma_guest_id="gst-HfFMtbx1qr3r30x",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="95cfa83b-e5ab-44a7-90ae-b4a2def9ba46",
        luma_guest_id="gst-rjV5dsJLNDh5plH",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="51d486d1-12b2-468e-966d-b8a5114c0d84",
        luma_guest_id="gst-NTJOOpgrUFG5ElZ",
        luma_event_id="evt-ZWviVlhfy5Wstc9",
        engagement_id="c5333157-664e-45d8-9894-45e62f0634dd",
        fields=("city", "state", "country"),
        expected_values=_ATX,
    ),
    FrozenReconciliationRow(
        crm_contact_id="2868029c-c6f0-4a4d-a914-0e120007efa7",
        luma_guest_id="gst-ZyYN4bWSqfCyXmn",
        luma_event_id="evt-d2LBzV0sWzTxoFD",
        engagement_id="5f89a109-3192-40a2-97d4-040828fc52b1",
        fields=("city", "state", "country"),
        expected_values=_SF,
    ),
    FrozenReconciliationRow(
        crm_contact_id="d904e233-7da5-41bb-b2a9-8211549e3b94",
        luma_guest_id="gst-6xNL9cqnMOUg6NS",
        luma_event_id="evt-d2LBzV0sWzTxoFD",
        engagement_id="5f89a109-3192-40a2-97d4-040828fc52b1",
        fields=("city", "state", "country"),
        expected_values=_SF,
    ),
    FrozenReconciliationRow(
        crm_contact_id="4da369d5-912b-48a3-b9e4-f6dc6576a695",
        luma_guest_id="gst-SPTA81D6eDlDfjA",
        luma_event_id="evt-d2LBzV0sWzTxoFD",
        engagement_id="5f89a109-3192-40a2-97d4-040828fc52b1",
        fields=("state", "country"),  # city already populated on this Contact -- preserved, never approved for write
        expected_values={"state": _SF["state"], "country": _SF["country"]},
    ),
    FrozenReconciliationRow(
        crm_contact_id="e441013b-5002-4ddc-8a83-369bb244e1dd",
        luma_guest_id="gst-Qrohj1QKvMzPiRA",
        luma_event_id="evt-d2LBzV0sWzTxoFD",
        engagement_id="5f89a109-3192-40a2-97d4-040828fc52b1",
        fields=("city", "state", "country"),
        expected_values=_SF,
    ),
    FrozenReconciliationRow(
        crm_contact_id="9ee3f825-23f2-4b62-aa06-8b35fd114838",
        luma_guest_id="gst-49vcKsAwjnxEmik",
        luma_event_id="evt-LtlLFXz5Gm3HnjO",
        engagement_id="b5d3120d-fe25-4e93-bf60-f34e06609274",
        fields=("city", "state", "country"),
        expected_values=_SF,
    ),
)

# Structural constants a caller/test can assert against without recounting the tuple above.
FROZEN_COHORT_SIZE = 17
FROZEN_COHORT_FIELD_TOTALS = {"city": 16, "state": 17, "country": 17}
BLOCKED_CONTACT_ID_NOT_IN_COHORT = "155132ee-cc78-4de7-b3fb-601f8d8df344"
