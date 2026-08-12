"""
CSV import: upload -> preview -> commit, each step persisted (see
CrmImportBatchStore's docstring). No LLM involved anywhere in this file --
column-mapping suggestions come from a deterministic header-alias table.

Multi-value fields (technologies, every thesis "which X do you invest in"
list) expect SEMICOLON-separated values in a CSV cell, not commas -- several
of the Investor Thesis form's own canonical options contain literal commas
in their text (e.g. "Collectibles (e.g., art, wine, watches)"), so splitting
on comma would shred a single selected option into several garbage values.
"""

import csv
import io
import uuid
from datetime import datetime, timezone
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import (
    CrmContact,
    CrmImportBatch,
    CrmImportBatchStatus,
    CrmImportReport,
    CrmImportRowPreview,
    CrmImportRowStatus,
    CustomFieldType,
    normalize_email,
    normalize_linkedin_url,
    normalize_name_company,
)
from app.repositories.crm_import_batch_store import CrmImportBatchStore
from app.services.crm_classification_rules import apply_classification_rules, build_classification_context
from app.services.crm_service import CUSTOM_FIELD_PREFIX, CrmService

LIST_FIELD_NAMES = frozenset(
    {
        "technologies",
        # thesis_private_check_sizes / thesis_institutional_check_sizes deliberately
        # excluded -- deprecated as of the 2026-08-06 Check Size consolidation.
        # check_size_personal/check_size_institutional (custom fields, via
        # classify_check_size) are now the sole canonical import destinations.
        "thesis_private_asset_types", "thesis_private_business_models", "thesis_private_industries",
        "thesis_private_deal_stages", "thesis_private_meeting_preferences",
        "thesis_private_demographic_preferences",
        "thesis_institutional_asset_types", "thesis_institutional_business_models", "thesis_institutional_industries",
        "thesis_institutional_deal_stages", "thesis_institutional_meeting_preferences",
        "thesis_institutional_demographic_preferences",
        # thesis_dietary_preferences: classify_dietary_preferences (crm_classification_rules.py)
        # is the authoritative path (validates against DIETARY_PREFERENCE_OPTIONS and always
        # wins), but this entry keeps the plain column-mapping coercion path consistent too.
        "thesis_dietary_preferences",
    }
)

BOOLEAN_FIELD_NAMES = frozenset({"thesis_also_invests_institutionally"})

# Deterministic header-alias table -- keys are normalized (lowercase, non-alnum -> space,
# collapsed whitespace) before lookup. No LLM, no fuzzy matching.
HEADER_ALIASES: dict[str, str] = {
    "first name": "first_name", "firstname": "first_name", "given name": "first_name",
    "last name": "last_name", "lastname": "last_name", "surname": "last_name", "family name": "last_name",
    "email": "email", "email address": "email", "e mail": "email",
    "email status": "email_status", "email verification status": "email_status", "verification status": "email_status",
    "phone": "phone", "phone number": "phone", "mobile": "phone", "mobile phone": "phone",
    "linkedin": "linkedin_url", "linkedin url": "linkedin_url",
    "linkedin profile": "linkedin_url", "linkedin profile url": "linkedin_url",
    "person linkedin url": "linkedin_url",
    "title": "title", "job title": "title", "position": "title",
    "company": "company", "organization": "company", "employer": "company", "company name": "company",
    "company name for emails": "company",
    "company website": "company_website", "website": "company_website",
    "domain": "company_website", "company domain": "company_website",
    "city": "city", "state": "state", "country": "country", "industry": "industry",
    # "company size" (bare) deliberately does NOT alias to company_size -- confirmed via
    # the 2026-08-06 Contacts 3 (Investors) audit that this is a DIFFERENT concept from
    # "# Employees"/"employees"/"employee count"/"number of employees": those are Apollo's
    # real headcount number, while "Company Size" is the Investor Thesis form's own
    # free-text bucketed answer (e.g. "51-200 employees"). Every populated company_size
    # value in production (484/528 contacts) is a bare number, proving "# Employees" has
    # been the sole real-world source all along; CSV A and B never had a single "Company
    # Size" value, and CSV C's 3 rows all had "# Employees" populated too, so removing
    # this alias loses zero data across every CSV imported so far. Left genuinely
    # unmapped (no destination) rather than repointed -- no CRM field for the thesis-form
    # answer exists yet; that's a separate decision, not resolved here.
    "employees": "company_size",
    "employee count": "company_size", "number of employees": "company_size",
    "revenue": "revenue", "annual revenue": "revenue",
    # "stage" (bare) deliberately does NOT alias to funding_stage -- confirmed via the
    # 2026-08-06 two-CSV audit that neither CSV even HAS a "Funding Stage" column; their
    # "Stage" column holds outreach/engagement values (Interested/Cold/Unresponsive/
    # Replied), which is exactly what the custom field engagement_stage exists for (see
    # its description: "Our own outreach/engagement pipeline stage -- NOT a funding
    # stage."). See classify_engagement_stage in crm_classification_rules.py -- that's
    # the real destination for "Stage", validated against its live options.
    "funding stage": "funding_stage",
    "funding amount": "funding_amount", "total funding": "funding_amount",
    "technologies": "technologies", "tech stack": "technologies",
    "seniority": "seniority", "department": "department",
    "job function": "job_function", "function": "job_function",
    "apollo contact id": "apollo_contact_id", "apollo id": "apollo_contact_id",
    "dietary preferences": "thesis_dietary_preferences", "dietary restrictions": "thesis_dietary_preferences",
    "secondary email": f"{CUSTOM_FIELD_PREFIX}secondary_email",
    "corporate phone": f"{CUSTOM_FIELD_PREFIX}corporate_phone",
    # 2026-08-06 broader-audit Phase 1 -- ten plain scalar custom fields with a real
    # CRM destination and zero prior mapping. Every one is a simple text/boolean/date
    # scalar with no comma-parsing or legacy-wording concern, so a bare alias plus the
    # existing generic _coerce_value()/apply_import_mapping() fill-only-if-empty policy
    # is sufficient -- no classification rule needed (unlike Accredited Status, a
    # validated single-select, which gets its own rule below in
    # crm_classification_rules.py). "Chris Knows Personally"/"Do Not Call" rely on
    # _coerce_value's existing generic boolean coercion for CustomFieldType.BOOLEAN
    # ("yes"/"true"/"1" -> True, everything else -> False).
    "work direct phone": f"{CUSTOM_FIELD_PREFIX}work_direct_phone",
    "do not call": f"{CUSTOM_FIELD_PREFIX}do_not_call",
    "last raised at": f"{CUSTOM_FIELD_PREFIX}last_raised_at",
    "how often do you invest": f"{CUSTOM_FIELD_PREFIX}how_often_do_you_invest",
    "personal notes": f"{CUSTOM_FIELD_PREFIX}personal_notes",
    "notes": f"{CUSTOM_FIELD_PREFIX}notes",
    "who were you referred to constellation dinners by": f"{CUSTOM_FIELD_PREFIX}referred_to_constellation_dinners_by",
    "geographic preference": f"{CUSTOM_FIELD_PREFIX}investment_geography_preference",
    "chris knows personally": f"{CUSTOM_FIELD_PREFIX}chris_knows_personally",
    # 2026-08-06 Contacts 3 (Investors) audit -- two more plain scalar custom fields
    # with a real CRM destination and zero prior mapping. `qualify_contact` (TEXT) and
    # `do_not_invest_in` (LONG_TEXT) have no options to validate against, so -- same
    # reasoning as Secondary Email/Corporate Phone/Notes above -- a bare alias plus the
    # existing generic _coerce_value()/apply_import_mapping() fill-only-if-empty policy
    # is sufficient; no classification rule needed. `Revenue Stage` is NOT here -- it's
    # a validated single_select field, handled by classify_revenue_stage() instead, same
    # pattern as Accredited Status/Chris Degree Connection/Age Range/Gender/Engagement Stage.
    "qualify contact": f"{CUSTOM_FIELD_PREFIX}qualify_contact",
    "do not invest in": f"{CUSTOM_FIELD_PREFIX}do_not_invest_in",
}


def _same_identity(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    False only if BOTH first and last name disagree between two mapped-row
    dicts -- i.e. no name overlap at all (the real James Feldkamp/Shawn Riely
    shape: two completely unrelated names sharing one identifier due to a
    source-data error). A partial match (same first name, different last
    name, or vice versa) is treated as the SAME identity, not a conflict --
    that's more likely a nickname/data-entry variant of one real person
    (confirmed real shape: Carlos Oviedo's CSV also had a "Carlos Cardenas"
    row under his email) than two different people, and the merge rule
    already protects against a wrong overwrite regardless (a populated
    external field is never overwritten). If either row has no name at all,
    there's nothing to conflict on either. Used to decide whether a shared
    confident dedup key within one file represents the same real person or a
    source-data collision (see preview()); mirrors CrmService.
    _conflicts_on_identity, which applies the identical rule against an
    already-existing DB contact.
    """
    a_first, a_last = (a.get("first_name") or "").strip().lower(), (a.get("last_name") or "").strip().lower()
    b_first, b_last = (b.get("first_name") or "").strip().lower(), (b.get("last_name") or "").strip().lower()
    if not (a_first or a_last) or not (b_first or b_last):
        return True
    return not (a_first != b_first and a_last != b_last)


def _normalize_header(header: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in header.strip().lower())
    return " ".join(cleaned.split())


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    suggestions: dict[str, str] = {}
    for header in headers:
        target = HEADER_ALIASES.get(_normalize_header(header))
        if target:
            suggestions[header] = target
    return suggestions


class CrmImportBatchNotFound(Exception):
    def __init__(self, import_batch_id: str):
        self.import_batch_id = import_batch_id
        super().__init__(f"CRM import batch not found: {import_batch_id}")


class CrmImportService:
    def __init__(self, crm_service: CrmService, batch_store: CrmImportBatchStore):
        self.crm_service = crm_service
        self.batch_store = batch_store

    async def _require_batch(self, import_batch_id: str) -> CrmImportBatch:
        batch = await self.batch_store.get(import_batch_id)
        if batch is None:
            raise CrmImportBatchNotFound(import_batch_id)
        return batch

    async def get_batch(self, import_batch_id: str) -> CrmImportBatch:
        return await self._require_batch(import_batch_id)

    async def upload(self, filename: str, content: bytes) -> CrmImportBatch:
        text = content.decode("utf-8-sig")  # -sig strips a BOM if Excel added one
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        rows = [dict(row) for row in reader]

        batch = CrmImportBatch(
            import_batch_id=str(uuid.uuid4()),
            filename=filename,
            uploaded_at=datetime.now(timezone.utc),
            status=CrmImportBatchStatus.UPLOADED,
            headers=headers,
            rows=rows,
            row_count=len(rows),
            suggested_mapping=suggest_mapping(headers),
        )
        await self.batch_store.create(batch)
        return batch

    async def _coerce_value(self, target_field: str, raw_value: str) -> Any:
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None

        if target_field.startswith(CUSTOM_FIELD_PREFIX):
            field_key = target_field[len(CUSTOM_FIELD_PREFIX) :]
            definition = await self.crm_service.custom_field_store.get_by_field_key(field_key)
            if definition is None:
                return raw_value
            if definition.field_type == CustomFieldType.MULTI_SELECT:
                return [v.strip() for v in raw_value.split(";") if v.strip()]
            if definition.field_type == CustomFieldType.BOOLEAN:
                return raw_value.lower() in ("yes", "true", "1")
            if definition.field_type == CustomFieldType.NUMBER:
                try:
                    return float(raw_value)
                except ValueError:
                    return raw_value
            return raw_value

        if target_field in LIST_FIELD_NAMES:
            return [v.strip() for v in raw_value.split(";") if v.strip()]
        if target_field in BOOLEAN_FIELD_NAMES:
            return raw_value.lower() in ("yes", "true", "1")
        return raw_value

    async def _apply_mapping(
        self, raw_row: dict[str, str], column_mapping: dict[str, str], classification_context: dict[str, Any]
    ) -> dict[str, Any]:
        mapped: dict[str, Any] = {}
        for csv_header, target_field in column_mapping.items():
            if not target_field:
                continue
            value = await self._coerce_value(target_field, raw_row.get(csv_header, ""))
            if value is not None and value != []:
                mapped[target_field] = value
        # Classification rules read the raw row directly, independent of
        # column_mapping, and always win for the fields they touch -- this
        # is what makes them apply automatically to every future upload
        # regardless of whatever mapping the human chose. See
        # crm_classification_rules.py.
        mapped.update(apply_classification_rules(raw_row, classification_context))
        mapped["source_snapshot"] = dict(raw_row)
        return mapped

    @staticmethod
    def _confident_keys(mapped_fields: dict[str, Any]) -> list[str]:
        keys = []
        email_key = normalize_email(mapped_fields.get("email"))
        if email_key:
            keys.append(f"email:{email_key}")
        if mapped_fields.get("apollo_contact_id"):
            keys.append(f"apollo:{mapped_fields['apollo_contact_id']}")
        linkedin_key = normalize_linkedin_url(mapped_fields.get("linkedin_url"))
        if linkedin_key:
            keys.append(f"linkedin:{linkedin_key}")
        return keys

    async def preview(self, import_batch_id: str, column_mapping: dict[str, str]) -> CrmImportBatch:
        batch = await self._require_batch(import_batch_id)
        batch.column_mapping = column_mapping

        # Reference data classification rules need (e.g. classify_role's
        # live approved-options set) -- fetched ONCE per batch, not per row.
        classification_context = await build_classification_context(self.crm_service.custom_field_store)

        seen_confident: dict[str, int] = {}
        seen_fallback: dict[str, int] = {}
        previews: list[CrmImportRowPreview] = []
        counts = {status: 0 for status in CrmImportRowStatus}

        for row_index, raw_row in enumerate(batch.rows):
            try:
                mapped = await self._apply_mapping(raw_row, column_mapping, classification_context)
            except Exception as e:  # malformed row under this mapping -- isolated, doesn't abort the batch
                previews.append(
                    CrmImportRowPreview(row_index=row_index, mapped_fields={}, status=CrmImportRowStatus.ERROR, error=str(e))
                )
                counts[CrmImportRowStatus.ERROR] += 1
                continue

            status, matched_contact, matched_on = await self.crm_service.classify_match(mapped)

            if status == CrmImportRowStatus.NEW:
                # Within-file dedup: does an EARLIER row in this same batch already claim
                # one of this row's confident-tier keys, or its fallback key?
                confident_keys = self._confident_keys(mapped)
                first_row = next((seen_confident[k] for k in confident_keys if k in seen_confident), None)
                if first_row is not None:
                    # A shared confident key (usually email) normally means "the same
                    # person, safe to auto-merge" -- EXCEPT when the two rows' names
                    # don't match. That's a source-data error (e.g. one row's email
                    # column was accidentally filled with a DIFFERENT person's address --
                    # confirmed by exactly this shape of row in the 2026-08-06 two-CSV
                    # audit: two different names sharing one email, where only one
                    # name's own email domain/company actually matches it). Blindly
                    # merging would silently fold a second real person into the first's
                    # record, so this downgrades to POSSIBLE_DUPLICATE (defaults to
                    # skip, always human-reviewed) instead of auto-merging.
                    if _same_identity(mapped, previews[first_row].mapped_fields):
                        status, matched_on = CrmImportRowStatus.EXISTING, f"within_file_row_{first_row}"
                    else:
                        status, matched_on = (
                            CrmImportRowStatus.POSSIBLE_DUPLICATE,
                            f"within_file_row_{first_row}_conflicting_identity",
                        )
                else:
                    fallback_key = normalize_name_company(
                        mapped.get("first_name"), mapped.get("last_name"), mapped.get("company")
                    )
                    fallback_row = seen_fallback.get(fallback_key) if fallback_key else None
                    if fallback_row is not None:
                        status, matched_on = CrmImportRowStatus.POSSIBLE_DUPLICATE, f"within_file_row_{fallback_row}"
                    else:
                        for key in confident_keys:
                            seen_confident[key] = row_index
                        if fallback_key:
                            seen_fallback[fallback_key] = row_index

            previews.append(
                CrmImportRowPreview(
                    row_index=row_index,
                    mapped_fields=mapped,
                    status=status,
                    matched_contact_id=matched_contact.crm_contact_id if matched_contact else None,
                    matched_on=matched_on,
                )
            )
            counts[status] += 1

        batch.preview = previews
        batch.new_count = counts[CrmImportRowStatus.NEW]
        batch.existing_count = counts[CrmImportRowStatus.EXISTING]
        batch.possible_duplicate_count = counts[CrmImportRowStatus.POSSIBLE_DUPLICATE]
        batch.error_count = counts[CrmImportRowStatus.ERROR]
        batch.status = CrmImportBatchStatus.MAPPED
        await self.batch_store.save(batch)
        return batch

    async def import_one_row(
        self,
        raw_row: dict[str, str],
        column_mapping: dict[str, str],
        classification_context: dict[str, Any],
        extra_fields: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> tuple[CrmImportRowStatus, CrmContact | None, str | None, dict[str, Any]]:
        """
        Single-row equivalent of preview()+commit() combined, for any caller
        ingesting one row at a time against the LIVE contact store rather than
        a CrmImportBatch -- today, ItfIngestionService (see
        app/services/itf_ingestion_service.py). Reuses _apply_mapping,
        crm_service.classify_match, crm_service.create_contact_from_import, and
        crm_service.apply_import_mapping VERBATIM -- no ITF-specific
        classification/dedup/merge logic exists anywhere.

        `extra_fields` are merged into `mapped` AFTER classification rules run
        (so a rule can never silently override them) but BEFORE
        classify_match/apply_import_mapping -- e.g. the ITF caller passes
        {"source": "itf", "custom:itf_submitted_at": ...} here, letting them
        flow through the exact same merge rule as every other field (fill-only
        for "source" since it's in EXTERNAL_FIELD_NAMES; always-latest for the
        custom field per LATEST_WINS_CUSTOM_FIELDS in crm_service.py) with zero
        special-casing in this method.

        No within-run dedup bookkeeping (unlike preview(), which tracks
        confident keys across a whole CrmImportBatch) -- each call sees the
        CRM's actual current state, so a contact created by an earlier call in
        the same run is already visible to this call's classify_match. The one
        gap this leaves: two Sheet rows sharing an email/LinkedIn/Apollo id
        within a SINGLE dry_run=True call would both independently report NEW,
        since a dry run never writes the first one for the second to see --
        self-corrects on the next real (non-dry) run regardless, and is
        visible in the dry run's own row-by-row report (both rows list the
        same email) rather than hidden.

        Never writes when `dry_run=True`, regardless of status -- returns
        classification only, so the caller can report exactly what WOULD
        happen without touching the CRM.
        """
        mapped = await self._apply_mapping(raw_row, column_mapping, classification_context)
        if extra_fields:
            mapped.update(extra_fields)

        status, matched_contact, matched_on = await self.crm_service.classify_match(mapped)

        if dry_run:
            return status, matched_contact, matched_on, mapped

        if status == CrmImportRowStatus.NEW:
            contact = await self.crm_service.create_contact_from_import(mapped)
            return status, contact, matched_on, mapped
        if status == CrmImportRowStatus.EXISTING:
            assert matched_contact is not None
            merged = self.crm_service.apply_import_mapping(matched_contact, mapped, is_new=False)
            await self.crm_service.contact_store.save(merged)
            return status, merged, matched_on, mapped

        return status, matched_contact, matched_on, mapped

    @staticmethod
    def _default_decision(status: CrmImportRowStatus) -> str:
        # POSSIBLE_DUPLICATE defaults to skip -- "never silently create duplicates"
        # means an unreviewed possible duplicate must not be auto-created OR auto-merged.
        return {
            CrmImportRowStatus.NEW: "create",
            CrmImportRowStatus.EXISTING: "update",
            CrmImportRowStatus.POSSIBLE_DUPLICATE: "skip",
            CrmImportRowStatus.ERROR: "skip",
        }[status]

    async def commit(self, import_batch_id: str, decisions: dict[int, str] | None = None) -> CrmImportReport:
        batch = await self._require_batch(import_batch_id)
        if batch.preview is None:
            raise ValueError("Must call preview() before commit()")
        decisions = decisions or {}

        created = updated = skipped = errors = 0
        created_in_this_commit: dict[int, str] = {}

        for row in batch.preview:
            if row.status == CrmImportRowStatus.ERROR:
                skipped += 1
                continue
            decision = decisions.get(row.row_index, self._default_decision(row.status))

            try:
                if decision == "skip":
                    skipped += 1
                elif decision == "create":
                    contact: CrmContact = await self.crm_service.create_contact_from_import(row.mapped_fields)
                    created_in_this_commit[row.row_index] = contact.crm_contact_id
                    created += 1
                elif decision == "update":
                    target_id = row.matched_contact_id
                    if target_id is None and row.matched_on and row.matched_on.startswith("within_file_row_"):
                        ref_row = int(row.matched_on.removeprefix("within_file_row_"))
                        target_id = created_in_this_commit.get(ref_row)
                    if target_id is None:
                        errors += 1
                        continue
                    existing = await self.crm_service.contact_store.get(target_id)
                    if existing is None:
                        errors += 1
                        continue
                    merged = self.crm_service.apply_import_mapping(existing, row.mapped_fields, is_new=False)
                    await self.crm_service.contact_store.save(merged)
                    updated += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

        batch.status = CrmImportBatchStatus.COMMITTED
        await self.batch_store.save(batch)

        # ONE summary event per commit() call, never one per row -- reuses
        # CrmService's own ActivityLogService rather than taking a separate
        # dependency, since this service is always constructed with a
        # crm_service that already has one (see app/main.py's lifespan wiring).
        await self.crm_service.activity_log.record(
            event_type="import.completed",
            category=ActivityCategory.IMPORTS,
            source=ActivitySource.CSV_IMPORT,
            summary=(
                f"CSV import completed: {created} created, {updated} updated, "
                f"{skipped} skipped, {errors} error{'s' if errors != 1 else ''} "
                f'("{batch.filename}").'
            ),
            entity_type="import_batch",
            entity_id=batch.import_batch_id,
            entity_name=batch.filename,
            metadata={"created": created, "updated": updated, "skipped": skipped, "errors": errors},
        )
        return CrmImportReport(created=created, updated=updated, skipped=skipped, errors=errors)
