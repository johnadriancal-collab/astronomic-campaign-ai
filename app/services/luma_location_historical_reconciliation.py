"""
Stage 6C (2026-09-14) -- historical Luma location reconciliation for the
EXPLICIT, frozen cohort in app/services/luma_location_historical_cohort.py.
Two operations, mirroring the established Stage 3C convention
(app/services/contact_engagement_stage_backfill.py):

compute_dry_run_report() -- READ-ONLY. Revalidates every row in
FROZEN_COHORT against CURRENT production data and classifies it; makes
zero writes, structurally (it never calls contact_store.save() or
activity_log.record()).

apply_frozen_cohort_reconciliation() -- the WRITE path. Iterates ONLY
FROZEN_COHORT -- there is no parameter, flag, or code path that accepts a
caller-supplied target list, unlike Stage 3C's own write path. This is a
deliberate, stricter design for Stage 6C specifically: the cohort was
reviewed and approved as a fixed set, and this module makes it
structurally impossible for an operator invocation to expand or
substitute it.

Reuses app.services.luma_contact_location_enrichment.apply_luma_event_location_enrichment()
completely unmodified -- Stage 6C inherits Stage 6B's own
already-production-proven contract byte-for-byte (registered_at
required, structured LumaEvent geo only, independent fill-only
city/state/country, country normalization, source="luma_event_location"
provenance). Field-level restriction to exactly a row's own approved
`fields` is achieved by passing a temporarily-restricted LumaEvent view
(every OTHER geo attribute forced to None) into that same function --
never a modification to the function itself, and never reliant on the
Contact's OTHER fields happening to already be non-blank.

Every row is revalidated LIVE, immediately before use, via
_revalidate_and_prepare() -- see its own docstring for the exact 10
checks. A row failing any check is skipped and reported as drift (or, if
its approved fields are simply already filled, reported as
already-satisfied) -- this module never substitutes a different
registration/event and never expands the cohort to compensate.
"""

from dataclasses import dataclass, field
from enum import Enum

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact
from app.models.luma import LumaEvent, LumaRegistration
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.luma_event_store import LumaEventStore
from app.repositories.luma_registration_store import LumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.luma_contact_location_enrichment import (
    FIELD_PROVENANCE_KEY,
    apply_luma_event_location_enrichment,
    normalize_country_code,
)
from app.services.luma_location_historical_cohort import FROZEN_COHORT, FrozenReconciliationRow

_EVENT_FIELD_ATTR = {"city": "location_city", "state": "location_region", "country": "location_country"}


def _blank(value: str | None) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _contact_display_name(contact: CrmContact) -> str:
    """Small local copy, not an import of luma_sync_service.py's own
    private helper -- same "don't reach across modules for a private,
    leading-underscore function" precedent already used by
    luma_engagement_participant_sync_service.py's own
    _participant_display_name()."""
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return name or contact.email or contact.crm_contact_id


class RowOutcome(str, Enum):
    WOULD_FILL = "would_fill"  # dry-run: eligible now, would write these exact fields
    FILLED = "filled"  # write mode: actually wrote
    ALREADY_SATISFIED = "already_satisfied"  # every approved field already non-blank -- idempotent no-op
    DRIFT_CONTACT_MISSING = "drift_contact_missing"
    DRIFT_REGISTRATION_MISSING = "drift_registration_missing"
    DRIFT_REGISTRATION_CONTACT_MISMATCH = "drift_registration_contact_mismatch"
    DRIFT_NOT_REGISTERED = "drift_not_registered"
    DRIFT_EVENT_ID_MISMATCH = "drift_event_id_mismatch"
    DRIFT_EVENT_MISSING = "drift_event_missing"
    DRIFT_GEO_UNAVAILABLE = "drift_geo_unavailable"
    DRIFT_VALUE_CHANGED = "drift_value_changed"
    DRIFT_PROVENANCE_CONFLICT = "drift_provenance_conflict"
    ERROR = "error"


_DRIFT_OUTCOMES = frozenset(
    o for o in RowOutcome if o not in (RowOutcome.WOULD_FILL, RowOutcome.FILLED, RowOutcome.ALREADY_SATISFIED, RowOutcome.ERROR)
)


def _restricted_event_view(event: LumaEvent, approved_fields: tuple[str, ...]) -> LumaEvent:
    """A shallow copy of `event` with every structured geo attribute NOT
    in `approved_fields` forced to None. This is what makes it
    structurally impossible for apply_luma_event_location_enrichment()
    (reused unmodified below) to fill anything beyond THIS row's own
    approved fields -- even if some other Contact field happens to also
    be blank at write time, it is never touched, because the function
    itself never sees a non-None candidate value for it."""
    update = {attr: None for field_name, attr in _EVENT_FIELD_ATTR.items() if field_name not in approved_fields}
    return event.model_copy(update=update)


@dataclass(frozen=True)
class RevalidationResult:
    outcome: RowOutcome
    contact: CrmContact | None = None
    restricted_event: LumaEvent | None = None
    registration: LumaRegistration | None = None


async def _revalidate_and_prepare(
    row: FrozenReconciliationRow,
    contact_store: CrmContactStore,
    registration_store: LumaRegistrationStore,
    event_store: LumaEventStore,
) -> RevalidationResult:
    """The exact 10-point pre-write revalidation:
    1. Contact still exists.
    2. Registration still exists.
    3. Registration still belongs to that Contact.
    4. registered_at is still not None.
    5. Registration still references the frozen luma_event_id.
    6. LumaEvent still exists.
    7. Structured geo for every approved field is still present.
    8. At least one approved field is still blank on the Contact (if
       ALL are already non-blank, that's the idempotent already-
       satisfied case -- an approved field that's already non-blank on
       ONLY some, not all, fields is normal and expected, same
       independent-fill-only spirit as Stage 6B itself: whichever
       fields are already set are simply left alone, never treated as
       drift).
    9. No conflicting field_provenance entry already exists for any
       approved field that is still blank.
    10. The live, normalized value for every approved field still
        matches this row's own frozen `expected_values` exactly.

    Returns a RevalidationResult whose contact/restricted_event/
    registration are non-None ONLY when outcome == WOULD_FILL -- every
    other outcome (drift, already-satisfied, or otherwise) returns None
    for all three, so a caller can never accidentally act on stale or
    partial data merely by forgetting to check the outcome first."""
    contact = await contact_store.get(row.crm_contact_id)
    if contact is None:
        return RevalidationResult(RowOutcome.DRIFT_CONTACT_MISSING)

    registration = await registration_store.get(row.luma_guest_id)
    if registration is None:
        return RevalidationResult(RowOutcome.DRIFT_REGISTRATION_MISSING)
    if registration.crm_contact_id != row.crm_contact_id:
        return RevalidationResult(RowOutcome.DRIFT_REGISTRATION_CONTACT_MISMATCH)
    if registration.registered_at is None:
        return RevalidationResult(RowOutcome.DRIFT_NOT_REGISTERED)
    if registration.luma_event_id != row.luma_event_id:
        return RevalidationResult(RowOutcome.DRIFT_EVENT_ID_MISMATCH)

    event = await event_store.get(row.luma_event_id)
    if event is None:
        return RevalidationResult(RowOutcome.DRIFT_EVENT_MISSING)

    for f in row.fields:
        raw = getattr(event, _EVENT_FIELD_ATTR[f])
        current_normalized = normalize_country_code(raw) if f == "country" else raw
        if not current_normalized:
            return RevalidationResult(RowOutcome.DRIFT_GEO_UNAVAILABLE)
        if current_normalized != row.expected_values.get(f):
            return RevalidationResult(RowOutcome.DRIFT_VALUE_CHANGED)

    # A row's approved fields are independent (same fill-only spirit as
    # Stage 6B itself) -- one of them ALREADY being non-blank (e.g. a
    # Contact whose city was already set before this row was even
    # frozen) is the normal, expected case, not drift: that field is
    # simply left alone, exactly like apply_luma_event_location_enrichment()
    # itself already does. ALREADY_SATISFIED means every approved field
    # is non-blank -- nothing at all left to do for this row.
    blank_fields_in_row = [f for f in row.fields if _blank(getattr(contact, f))]
    if not blank_fields_in_row:
        return RevalidationResult(RowOutcome.ALREADY_SATISFIED)

    provenance = (contact.custom_fields or {}).get(FIELD_PROVENANCE_KEY) or {}
    for f in blank_fields_in_row:
        if provenance.get(f):
            return RevalidationResult(RowOutcome.DRIFT_PROVENANCE_CONFLICT)

    restricted_event = _restricted_event_view(event, row.fields)
    return RevalidationResult(RowOutcome.WOULD_FILL, contact=contact, restricted_event=restricted_event, registration=registration)


@dataclass
class DryRunRow:
    crm_contact_id: str
    luma_event_id: str
    fields: tuple[str, ...]
    outcome: RowOutcome


@dataclass
class DryRunCounts:
    frozen_rows: int = 0
    eligible_now: int = 0
    already_satisfied: int = 0
    drifted_or_skipped: int = 0
    contacts_that_would_change: int = 0
    total_fields_that_would_change: int = 0
    city: int = 0
    state: int = 0
    country: int = 0


@dataclass
class DryRunReport:
    counts: DryRunCounts
    rows: list[DryRunRow] = field(default_factory=list)

    @property
    def drifted(self) -> list[DryRunRow]:
        return [r for r in self.rows if r.outcome in _DRIFT_OUTCOMES]


async def compute_dry_run_report(
    contact_store: CrmContactStore, registration_store: LumaRegistrationStore, event_store: LumaEventStore
) -> DryRunReport:
    """READ-ONLY -- never calls contact_store.save() or any Activity
    Log method. Revalidates every row in FROZEN_COHORT (never any other
    Contact/registration) against current production data."""
    counts = DryRunCounts(frozen_rows=len(FROZEN_COHORT))
    rows: list[DryRunRow] = []
    for row in FROZEN_COHORT:
        result = await _revalidate_and_prepare(row, contact_store, registration_store, event_store)
        rows.append(DryRunRow(crm_contact_id=row.crm_contact_id, luma_event_id=row.luma_event_id, fields=row.fields, outcome=result.outcome))
        if result.outcome == RowOutcome.WOULD_FILL:
            counts.eligible_now += 1
            counts.contacts_that_would_change += 1
            counts.total_fields_that_would_change += len(row.fields)
            for f in row.fields:
                setattr(counts, f, getattr(counts, f) + 1)
        elif result.outcome == RowOutcome.ALREADY_SATISFIED:
            counts.already_satisfied += 1
        else:
            counts.drifted_or_skipped += 1
    return DryRunReport(counts=counts, rows=rows)


@dataclass
class WriteResult:
    crm_contact_id: str
    outcome: RowOutcome
    detail: str


@dataclass
class WriteReport:
    results: list[WriteResult] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        return counts


async def apply_frozen_cohort_reconciliation(
    contact_store: CrmContactStore,
    registration_store: LumaRegistrationStore,
    event_store: LumaEventStore,
    activity_log: ActivityLogService,
) -> WriteReport:
    """The WRITE path. Iterates ONLY FROZEN_COHORT -- takes no target-id
    parameter of any kind, by design (see this module's own docstring
    for why Stage 6C is deliberately stricter here than Stage 3C's own
    operator-suppliable-id convention). Each row is independently
    revalidated live (never trusting any earlier snapshot); one row's
    failure is caught and reported, never stopping the rest of the
    batch."""
    report = WriteReport()
    for row in FROZEN_COHORT:
        try:
            result = await _revalidate_and_prepare(row, contact_store, registration_store, event_store)
            if result.outcome != RowOutcome.WOULD_FILL:
                report.results.append(WriteResult(row.crm_contact_id, result.outcome, "skipped -- see outcome"))
                continue

            outcome = apply_luma_event_location_enrichment(
                result.contact, result.restricted_event, result.registration, engagement_id=row.engagement_id
            )
            if not outcome.changed_field_keys:
                # Structurally shouldn't happen given WOULD_FILL just confirmed
                # every approved field is blank with usable, expected-matching
                # geo -- handled defensively as a no-op rather than assumed.
                report.results.append(WriteResult(row.crm_contact_id, RowOutcome.ALREADY_SATISFIED, "no changed fields at write time"))
                continue

            await contact_store.save(outcome.contact)
            display_name = _contact_display_name(outcome.contact)
            await activity_log.record(
                event_type="luma.contact.enriched",
                category=ActivityCategory.LUMA,
                source=ActivitySource.LUMA_SYNC,
                summary=f"{display_name} was enriched from a Luma registration.",
                entity_type="contact",
                entity_id=outcome.contact.crm_contact_id,
                entity_name=display_name,
                metadata={"fields_updated": outcome.changed_field_keys},
            )
            report.results.append(WriteResult(row.crm_contact_id, RowOutcome.FILLED, f"filled {sorted(outcome.changed_field_keys)}"))
        except Exception as exc:  # noqa: BLE001 -- one row's failure must never stop the batch
            report.results.append(WriteResult(row.crm_contact_id, RowOutcome.ERROR, f"{type(exc).__name__}: {exc}"))
    return report
