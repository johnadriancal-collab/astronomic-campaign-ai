"""
Historical reconciliation for the four investor-related Luma custom
fields whose LumaQuestionMapping rows previously carried a stale
question_type guard (the "Deploying Capital" investigation):

    custom:investor_type
    custom:check_size_personal
    custom:deploying_capital
    custom:investment_industry

Luma began sending question_type="select" for these questions instead of
their historical "dropdown"/"multi-select" -- the mapping's own
question_type guard silently stopped matching once that happened. The
fix is a MAPPING CONFIG change (question_type -> None on the four
mapping rows), not a code change -- see app/services/luma_sync_service.py's
_build_mapped_fields(), whose `if mapping.question_type and
mapping.question_type != question_type` guard already treats None as
"matches any type". Once that config fix is applied, this module
recovers the historical registrations that were already silently
dropped, using ONLY already-stored data.

Deliberately reuses LumaSyncService._build_mapped_fields() and
CrmService.apply_import_mapping() COMPLETELY UNMODIFIED -- no new merge
algorithm, no code duplication of the matching/normalizer/constraint
pipeline for the actual write decision. This is the SAME "existing
generic mapping/import semantics" every other pipeline in this app
already relies on (fill-only for single_select fields like
deploying_capital, union-merge for multi_select fields like
investor_type/check_size_personal/investment_industry) -- NOT the
Company/Title self-report's "latest wins, always replaces" semantics
(app/services/luma_contact_enrichment.py), which is a deliberately
different, separate feature for different fields.

Scope is narrow by construction: mapped_fields from _build_mapped_fields()
is filtered down to ONLY the four TARGET_FIELD_KEYS below before ever
reaching apply_import_mapping() -- company/title/linkedin_url/role and
any other mapped field from the same registration are never touched by
this module, no matter what else that registration's answers contain.

Makes ZERO Luma API calls -- reads only already-persisted
LumaRegistration.registration_answers. Makes ZERO writes when
dry_run=True (the default). A Contact whose registrations, once
reprocessed, resolve to the exact same custom_fields values it already
has produces zero changes and zero save -- idempotent by construction
(apply_import_mapping()'s own fill-only/union-merge rules are themselves
idempotent; re-deriving from the same stored data can only ever converge
to the same result).

A small classification pass (`_classify_answer`) mirrors -- for REPORTING
ONLY, never for the real write decision -- a slice of _build_mapped_fields's
own per-answer logic (reusing its own apply_normalizer/
_enforce_custom_field_constraints functions directly, not reimplementing
them) so the report can distinguish an answer that couldn't be
interpreted at all ("ambiguous/unmappable") from one that was
successfully interpreted but rejected by the destination field's own
option/constraint rules ("invalid, rejected by field definition").
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models.crm import CrmCustomFieldDefinition
from app.models.luma import LumaQuestionMapping, LumaRegistration, LumaRegistrationAnswer
from app.repositories.luma_registration_store import LumaRegistrationStore
from app.services.luma_answer_normalizers import apply_normalizer
from app.services.luma_sync_service import LumaSyncService, _enforce_custom_field_constraints

TARGET_FIELD_KEYS = frozenset(
    {
        "custom:investor_type",
        "custom:check_size_personal",
        "custom:deploying_capital",
        "custom:investment_industry",
    }
)


@dataclass
class FieldChange:
    field_key: str  # bare key, e.g. "investor_type" (no "custom:" prefix)
    old_value: Any
    new_value: Any


@dataclass
class ReconciliationExample:
    crm_contact_id: str
    changes: list[FieldChange] = field(default_factory=list)


@dataclass
class ReconciliationCounts:
    registrations_examined: int = 0
    registrations_unresolved_skipped: int = 0
    contacts_examined: int = 0
    contacts_would_change: int = 0
    contacts_unchanged: int = 0  # already-correct/no-op, including contacts who never answered any of the 4 questions
    changes_by_field: dict[str, int] = field(default_factory=dict)
    ambiguous_unmappable_answers: int = 0
    invalid_values_rejected: int = 0
    contacts_saved: int = 0  # only nonzero when dry_run=False


@dataclass
class ReconciliationReport:
    counts: ReconciliationCounts = field(default_factory=ReconciliationCounts)
    examples: list[ReconciliationExample] = field(default_factory=list)
    dry_run: bool = True


def _classify_answer(
    answer: LumaRegistrationAnswer,
    target_mappings: list[LumaQuestionMapping],
    custom_field_defs: dict[str, CrmCustomFieldDefinition],
) -> str | None:
    """Returns "valid", "ambiguous", "invalid", or None (no matching
    target mapping, or blank -- not one of our 4 questions, not counted
    at all). Report-only -- see module docstring."""
    label = (answer.label or "").strip().lower()
    value = answer.value
    if value is None or label == "":
        return None

    matched: LumaQuestionMapping | None = None
    for mapping in target_mappings:
        if mapping.question_label.strip().lower() != label:
            continue
        if mapping.question_type and mapping.question_type != answer.question_type:
            continue
        matched = mapping
        break
    if matched is None:
        return None

    extracted: Any = value
    if matched.extract_key:
        if not isinstance(value, dict):
            return "ambiguous"
        extracted = value.get(matched.extract_key)
        if extracted is None:
            return "ambiguous"

    if not isinstance(extracted, (str, list)):
        return "ambiguous"

    if matched.normalizer is not None:
        normalized = apply_normalizer(matched.normalizer, extracted)
        if not normalized:
            return "invalid"
        extracted = normalized

    field_def = custom_field_defs.get(matched.target_field_key.removeprefix("custom:"))
    if field_def is not None:
        constrained = _enforce_custom_field_constraints(matched.target_field_key, extracted, {field_def.field_key: field_def})
        if constrained is None:
            return "invalid"

    return "valid"


async def run_investor_fields_reconciliation(
    luma_sync_service: LumaSyncService,
    registration_store: LumaRegistrationStore,
    *,
    dry_run: bool = True,
    example_cap: int = 100,
) -> ReconciliationReport:
    contact_store = luma_sync_service.crm_service.contact_store
    active_mappings = await luma_sync_service.mapping_store.list(include_inactive=False)
    target_mappings = [m for m in active_mappings if m.target_field_key in TARGET_FIELD_KEYS]
    custom_field_defs = {f.field_key: f for f in await luma_sync_service.crm_service.list_custom_fields()}

    all_registrations = await registration_store.list()

    counts = ReconciliationCounts()
    regs_by_contact: dict[str, list[LumaRegistration]] = defaultdict(list)
    for reg in all_registrations:
        counts.registrations_examined += 1
        if not reg.crm_contact_id:
            counts.registrations_unresolved_skipped += 1
            continue
        regs_by_contact[reg.crm_contact_id].append(reg)
        for answer in reg.registration_answers:
            classification = _classify_answer(answer, target_mappings, custom_field_defs)
            if classification == "ambiguous":
                counts.ambiguous_unmappable_answers += 1
            elif classification == "invalid":
                counts.invalid_values_rejected += 1

    report = ReconciliationReport(counts=counts, dry_run=dry_run)

    for crm_contact_id, regs in regs_by_contact.items():
        original_contact = await contact_store.get(crm_contact_id)
        if original_contact is None:
            continue  # dangling crm_contact_id reference -- data-integrity edge, not expected; skip defensively
        counts.contacts_examined += 1

        working_contact = original_contact
        for reg in sorted(regs, key=lambda r: r.registered_at or datetime.min.replace(tzinfo=timezone.utc)):
            guest_shaped = {
                "registration_answers": [
                    {"label": a.label, "question_id": a.question_id, "question_type": a.question_type, "value": a.value}
                    for a in reg.registration_answers
                ]
            }
            mapped_fields_full = await luma_sync_service._build_mapped_fields(guest_shaped)
            scoped = {k: v for k, v in mapped_fields_full.items() if k in TARGET_FIELD_KEYS}
            if not scoped:
                continue
            working_contact = luma_sync_service.crm_service.apply_import_mapping(working_contact, scoped, is_new=False)

        changes: list[FieldChange] = []
        for target_key in sorted(TARGET_FIELD_KEYS):
            short_key = target_key.removeprefix("custom:")
            old_value = original_contact.custom_fields.get(short_key)
            new_value = working_contact.custom_fields.get(short_key)
            if old_value != new_value:
                changes.append(FieldChange(field_key=short_key, old_value=old_value, new_value=new_value))
                counts.changes_by_field[short_key] = counts.changes_by_field.get(short_key, 0) + 1

        if changes:
            counts.contacts_would_change += 1
            if len(report.examples) < example_cap:
                report.examples.append(ReconciliationExample(crm_contact_id=crm_contact_id, changes=changes))
            if not dry_run:
                await contact_store.save(working_contact)
                counts.contacts_saved += 1
        else:
            counts.contacts_unchanged += 1

    return report
