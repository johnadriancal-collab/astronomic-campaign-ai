"""
Notes / Personal Notes Consolidation (Stage NP-2) -- driver for the frozen
cohort in notes_personal_notes_merge_cohort.py.

SAFE BY DEFAULT: with no arguments, this is a DRY RUN against the fixed,
approved 9-Contact cohort. Writes NOTHING (no Contact save, no Activity Log
entry, no change to `personal_notes` itself -- this module NEVER clears or
modifies `personal_notes`, only copies/merges its value into `notes`).

Real writes require BOTH --write AND --confirm-production-writes (see
scripts/run_notes_personal_notes_merge.py) -- same two-gate convention as
every other operator write CLI in this repo (Stage 3C, Stage 6C, Austin
Forward). There is NO target-id argument -- the write-eligible universe is
always exactly COHORT from the frozen cohort module; this command cannot
be pointed at any other Contact, and it never grows to catch a Contact
that acquires a personal_notes value after this cohort was frozen (that
would require a new investigation/review cycle, by design).

Every row is re-evaluated LIVE at write time: the Contact is refetched and
its CURRENT `notes`/`personal_notes` are compared against the row's frozen
`frozen_notes`/`frozen_personal_notes`. Any difference -- in EITHER field --
is drift, and the row is skipped entirely rather than merging stale frozen
text over whatever is actually there now (see DRIFT_NOTES_CHANGED /
DRIFT_PERSONAL_NOTES_CHANGED). A missing or archived Contact is likewise
skipped, never forced.

Merge semantics (never destructive, `personal_notes` itself untouched):
  - Notes blank, Personal Notes populated: `notes` becomes the Personal
    Notes text, stripped of leading/trailing whitespace only -- no header,
    nothing else changed, since there is nothing to separate it from.
  - Both populated: the EXISTING `notes` value is preserved byte-for-byte,
    then "\\n\\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\\n{personal_notes}" is
    appended. Neither value is summarized, rewritten, or discarded.
  - Duplicate-content protection: if the (stripped) Personal Notes text is
    already present anywhere inside the current Notes value, this row is
    treated as ALREADY_SATISFIED and nothing is appended a second time --
    a defensive check independent of the provenance-marker idempotency
    gate below (see docstring on _plan_row).

Provenance design (see this module's own investigation write-up for the
full reasoning): `field_provenance` is a plain, untyped
`custom_fields["field_provenance"]` dict, one entry per field_key, with NO
existing convention anywhere in this codebase for a field having more than
one contributing source recorded at once (checked luma_contact_enrichment.py,
luma_contact_location_enrichment.py, profile_photo_service.py,
contact_engagement_signal_service.py -- all single-source-per-key). Writing
this migration's own source into `field_provenance["notes"]` would
therefore SILENTLY DESTROY whatever is already recorded there -- in
particular, any of the 161 Austin Forward `field_provenance["notes"]`
entries on a Contact that also happens to have a personal_notes value.
This driver deliberately does NOT touch `field_provenance["notes"]` at
all, ever. Instead it records the merge under its OWN, separate key --
`field_provenance["personal_notes_merge"]` (see PROVENANCE_MERGE_KEY) --
which:
  (a) never collides with or overwrites any existing `notes` provenance,
  (b) is itself the idempotency gate for this migration (a Contact that
      already has this key is ALREADY_SATISFIED, no re-merge attempted),
  (c) requires zero schema change -- `field_provenance` is already an
      unstructured dict; this is simply one more key in it, the same
      pattern every other module already uses for ITS OWN field.
This is the smallest safe representation given the existing single-
source-per-field-key convention; a genuine multi-source-per-field
provenance model would be a broader redesign, out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact, NOTES_PERSONAL_NOTES_MERGE_HEADER
from app.repositories.crm_contact_store import CrmContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.notes_personal_notes_merge_cohort import (
    COHORT,
    PROVENANCE_MERGE_KEY,
    PROVENANCE_MERGE_SOURCE,
)

FIELD_PROVENANCE_KEY = "field_provenance"


class RowOutcome(str, Enum):
    WOULD_MERGE = "would_merge"
    MERGED = "merged"
    ALREADY_SATISFIED = "already_satisfied"
    DRIFT_CONTACT_MISSING = "drift_contact_missing"
    DRIFT_CONTACT_ARCHIVED = "drift_contact_archived"
    DRIFT_NOTES_CHANGED = "drift_notes_changed"
    DRIFT_PERSONAL_NOTES_CHANGED = "drift_personal_notes_changed"


def _contact_display_name(contact: CrmContact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return name or contact.email or contact.crm_contact_id


def _has_merge_provenance(contact: CrmContact) -> bool:
    prov = (contact.custom_fields or {}).get(FIELD_PROVENANCE_KEY) or {}
    entry = prov.get(PROVENANCE_MERGE_KEY)
    return isinstance(entry, dict) and entry.get("source") == PROVENANCE_MERGE_SOURCE


def _compute_merge(current_notes: str | None, personal_notes: str) -> str:
    """The proposed new `notes` value. Never called once duplicate-content
    protection or the provenance gate has already decided this row is a
    no-op -- see _plan_row."""
    personal = personal_notes.strip()
    current = current_notes.strip() if current_notes else ""
    if not current:
        return personal
    return f"{current}\n\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\n{personal}"


@dataclass
class RowResult:
    crm_contact_id: str
    outcome: RowOutcome
    merge_type: str | None = None  # "copy" | "append" | None (no merge attempted)
    new_notes_preview_len: int | None = None  # length only, never the text itself, for reporting
    detail: str = ""


@dataclass
class MergeCounts:
    cohort_total: int = 0
    would_merge: int = 0
    copy_merges: int = 0
    append_merges: int = 0
    already_satisfied: int = 0
    drift: int = 0


@dataclass
class DryRunReport:
    counts: MergeCounts
    results: list[RowResult]


def _plan_row(row, contact: CrmContact | None) -> RowResult:
    if contact is None:
        return RowResult(row.crm_contact_id, RowOutcome.DRIFT_CONTACT_MISSING)
    if contact.archived:
        return RowResult(row.crm_contact_id, RowOutcome.DRIFT_CONTACT_ARCHIVED)

    cf = contact.custom_fields or {}
    current_notes = cf.get("notes")
    current_personal_notes = cf.get("personal_notes")

    # Idempotency gate: a Contact this driver has already merged is left
    # completely alone on any later run, regardless of what notes/
    # personal_notes look like now -- matching the Austin Forward
    # reconciliation's own provenance-gated idempotency convention exactly.
    if _has_merge_provenance(contact):
        return RowResult(row.crm_contact_id, RowOutcome.ALREADY_SATISFIED, detail="already merged (provenance marker present)")

    current_notes_norm = (current_notes or "").strip() if isinstance(current_notes, str) else ""

    # Defensive duplicate-content protection, independent of the provenance
    # gate above and checked against LIVE current Notes (not the frozen
    # snapshot) -- covers the case where the Personal Notes text is already
    # present inside Notes by some other means (e.g. a manual edit, or a
    # provenance marker that was somehow lost). Checked BEFORE the strict
    # drift comparison below: content already being present is itself
    # sufficient reason to skip, regardless of what else about `notes` may
    # have changed since this cohort was frozen -- skipping is always the
    # safe choice, never a data-loss risk.
    personal_stripped = row.frozen_personal_notes.strip()
    if personal_stripped and personal_stripped in current_notes_norm:
        return RowResult(row.crm_contact_id, RowOutcome.ALREADY_SATISFIED, detail="personal_notes content already present in notes")

    frozen_notes_norm = (row.frozen_notes or "").strip()
    if frozen_notes_norm != current_notes_norm:
        return RowResult(row.crm_contact_id, RowOutcome.DRIFT_NOTES_CHANGED)

    current_personal_norm = current_personal_notes.strip() if isinstance(current_personal_notes, str) else ""
    if row.frozen_personal_notes.strip() != current_personal_norm:
        return RowResult(row.crm_contact_id, RowOutcome.DRIFT_PERSONAL_NOTES_CHANGED)

    merge_type = "copy" if not current_notes_norm else "append"
    new_notes = _compute_merge(current_notes, row.frozen_personal_notes)
    return RowResult(
        row.crm_contact_id,
        RowOutcome.WOULD_MERGE,
        merge_type=merge_type,
        new_notes_preview_len=len(new_notes),
    )


async def compute_dry_run_report(contact_store: CrmContactStore) -> DryRunReport:
    counts = MergeCounts(cohort_total=len(COHORT))
    results: list[RowResult] = []
    for row in COHORT:
        contact = await contact_store.get(row.crm_contact_id)
        result = _plan_row(row, contact)
        results.append(result)
        if result.outcome == RowOutcome.WOULD_MERGE:
            counts.would_merge += 1
            if result.merge_type == "copy":
                counts.copy_merges += 1
            else:
                counts.append_merges += 1
        elif result.outcome == RowOutcome.ALREADY_SATISFIED:
            counts.already_satisfied += 1
        else:
            counts.drift += 1
    return DryRunReport(counts=counts, results=results)


def _new_provenance_entry() -> dict[str, Any]:
    return {"source": PROVENANCE_MERGE_SOURCE, "merged_at": datetime.now(timezone.utc).isoformat(), "original_field": "personal_notes"}


async def apply_frozen_cohort_merge(
    contact_store: CrmContactStore,
    activity_log: ActivityLogService,
) -> DryRunReport:
    """The write path. Reuses the exact same per-row planning as
    compute_dry_run_report() (never a separate/diverging code path), then
    persists each planned merge. A row whose plan is ALREADY_SATISFIED or
    drifted is never saved and never logged -- true no-op."""
    report = await compute_dry_run_report(contact_store)

    for result in report.results:
        if result.outcome != RowOutcome.WOULD_MERGE:
            continue
        row = next(r for r in COHORT if r.crm_contact_id == result.crm_contact_id)
        contact = await contact_store.get(result.crm_contact_id)
        if contact is None:
            continue  # drifted away between planning and write within this same call -- skip, don't force

        cf = dict(contact.custom_fields or {})
        new_notes = _compute_merge(cf.get("notes"), row.frozen_personal_notes)
        cf["notes"] = new_notes

        # Never touches field_provenance["notes"] -- see module docstring.
        # personal_notes itself is never read from `cf` for the merge (we
        # merge the frozen, already-revalidated value) and never written/
        # cleared here.
        prov = dict(cf.get(FIELD_PROVENANCE_KEY) or {})
        prov[PROVENANCE_MERGE_KEY] = _new_provenance_entry()
        cf[FIELD_PROVENANCE_KEY] = prov

        updated_contact = contact.model_copy(update={"custom_fields": cf, "updated_at": datetime.now(timezone.utc)})
        await contact_store.save(updated_contact)
        result.outcome = RowOutcome.MERGED

        name = _contact_display_name(updated_contact)
        await activity_log.record(
            event_type="notes.personal_notes_merged",
            category=ActivityCategory.CONTACTS,
            source=ActivitySource.SYSTEM,
            summary=f"{name}'s Personal Notes were merged into Notes.",
            entity_type="contact",
            entity_id=result.crm_contact_id,
            entity_name=name,
            metadata={"fields_updated": ["custom:notes", "custom:field_provenance"], "merge_type": result.merge_type, "source": PROVENANCE_MERGE_SOURCE},
        )

    return report
