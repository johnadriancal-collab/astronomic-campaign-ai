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
from app.services.crm_service import CUSTOM_FIELD_PREFIX, CrmService

LIST_FIELD_NAMES = frozenset(
    {
        "technologies",
        "thesis_private_asset_types", "thesis_private_business_models", "thesis_private_industries",
        "thesis_private_check_sizes", "thesis_private_deal_stages", "thesis_private_meeting_preferences",
        "thesis_private_demographic_preferences",
        "thesis_institutional_asset_types", "thesis_institutional_business_models", "thesis_institutional_industries",
        "thesis_institutional_check_sizes", "thesis_institutional_deal_stages", "thesis_institutional_meeting_preferences",
        "thesis_institutional_demographic_preferences",
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
    "title": "title", "job title": "title", "position": "title",
    "company": "company", "organization": "company", "employer": "company", "company name": "company",
    "company website": "company_website", "website": "company_website",
    "domain": "company_website", "company domain": "company_website",
    "city": "city", "state": "state", "country": "country", "industry": "industry",
    "company size": "company_size", "employees": "company_size",
    "employee count": "company_size", "number of employees": "company_size",
    "revenue": "revenue", "annual revenue": "revenue",
    "funding stage": "funding_stage", "stage": "funding_stage",
    "funding amount": "funding_amount", "total funding": "funding_amount",
    "technologies": "technologies", "tech stack": "technologies",
    "seniority": "seniority", "department": "department",
    "job function": "job_function", "function": "job_function",
    "apollo contact id": "apollo_contact_id", "apollo id": "apollo_contact_id",
    "dietary preferences": "thesis_dietary_preferences", "dietary restrictions": "thesis_dietary_preferences",
}


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

    async def _apply_mapping(self, raw_row: dict[str, str], column_mapping: dict[str, str]) -> dict[str, Any]:
        mapped: dict[str, Any] = {}
        for csv_header, target_field in column_mapping.items():
            if not target_field:
                continue
            value = await self._coerce_value(target_field, raw_row.get(csv_header, ""))
            if value is not None and value != []:
                mapped[target_field] = value
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

        seen_confident: dict[str, int] = {}
        seen_fallback: dict[str, int] = {}
        previews: list[CrmImportRowPreview] = []
        counts = {status: 0 for status in CrmImportRowStatus}

        for row_index, raw_row in enumerate(batch.rows):
            try:
                mapped = await self._apply_mapping(raw_row, column_mapping)
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
                    status, matched_on = CrmImportRowStatus.EXISTING, f"within_file_row_{first_row}"
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
        return CrmImportReport(created=created, updated=updated, skipped=skipped, errors=errors)
