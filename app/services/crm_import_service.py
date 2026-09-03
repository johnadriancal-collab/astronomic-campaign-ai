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
from collections import Counter
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
        if batch.status in (CrmImportBatchStatus.COMMITTING, CrmImportBatchStatus.COMMITTED):
            # preview() unconditionally REPLACES batch.preview with a fresh
            # list (see the end of this method) -- allowing that once
            # commit() has started would silently destroy the durable
            # per-row commit_outcome/resolved_contact_id progress that
            # commit()'s own resumability relies on (Stage 4A, 2026-09-03).
            raise ValueError(
                f"CrmImportBatch {import_batch_id} has already been committed (or is mid-commit) -- "
                "re-running preview() would discard durable commit progress. Upload a new CSV instead."
            )
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

    async def _find_existing_by_confident_identifiers(self, mapped_fields: dict[str, Any]) -> CrmContact | None:
        """RAW identifier lookup only -- email, then apollo_contact_id, then
        linkedin_url, the same order and normalization as classify_match()'s
        first three tiers, but deliberately NOT classify_match() itself:
        no conflict-downgrade (_conflicts_on_identity), no name+company
        fallback tier. Used ONLY by commit()'s "creating"-resume path (see
        that method's own docstring) to recover a contact a PRIOR, already-
        decided create attempt for this exact row produced -- never to
        classify or influence a row's decision in the first place, so it
        can never reinterpret what a human (or _default_decision) already
        chose for this row."""
        email = normalize_email(mapped_fields.get("email"))
        if email:
            found = await self.crm_service.contact_store.get_by_email(email)
            if found is not None:
                return found
        apollo_id = mapped_fields.get("apollo_contact_id")
        if apollo_id:
            found = await self.crm_service.contact_store.get_by_apollo_contact_id(apollo_id)
            if found is not None:
                return found
        linkedin = normalize_linkedin_url(mapped_fields.get("linkedin_url"))
        if linkedin:
            found = await self.crm_service.contact_store.get_by_linkedin_url(linkedin)
            if found is not None:
                return found
        return None

    async def _attempt_create(self, row: CrmImportRowPreview, created_in_this_commit: dict[int, str]) -> None:
        """Mutates `row` in place to a terminal outcome ("created" or
        "error") -- never leaves it at "creating". Caller owns the durable
        checkpoint save() both before (the "creating" marker) and after
        calling this."""
        try:
            contact = await self.crm_service.create_contact_from_import(row.mapped_fields)
            created_in_this_commit[row.row_index] = contact.crm_contact_id
            row.commit_outcome = "created"
            row.resolved_contact_id = contact.crm_contact_id
        except Exception:
            row.commit_outcome = "error"

    @staticmethod
    def _compute_report(preview: list[CrmImportRowPreview]) -> CrmImportReport:
        """Derives the aggregate report entirely from durable per-row
        commit_outcome values -- never from any in-memory counter -- so the
        SAME report is returned whether this is the call that just finished
        committing or a later call against an already-COMMITTED batch (see
        commit()'s own docstring). Every row is assumed to already have a
        non-None commit_outcome when this is called."""
        counts = Counter(row.commit_outcome for row in preview)
        return CrmImportReport(
            created=counts.get("created", 0),
            updated=counts.get("updated", 0),
            skipped=counts.get("skipped", 0),
            errors=counts.get("error", 0),
        )

    async def commit(self, import_batch_id: str, decisions: dict[int, str] | None = None) -> CrmImportReport:
        """
        Idempotent and resumable (Stage 4A, 2026-09-03) -- a row's
        `commit_outcome`, once set, is PERMANENT: this method never
        revisits, re-decides, or re-applies a row that already has one,
        regardless of how many times commit() is called against this batch
        or what `decisions` a later call happens to supply for that same
        row_index. This is what makes a retry safe after a crash, and what
        makes a batch already fully COMMITTED a true no-op on every
        subsequent call (zero contact creates/updates, zero Activity Log
        re-emission, the exact same CrmImportReport returned every time --
        computed fresh from durable state via _compute_report(), never from
        an in-memory counter carried across calls).

        DURABILITY MECHANISM: each row's outcome is persisted via
        `batch_store.save()` immediately after that ONE row is decided --
        not once at the end of the whole loop (the pre-Stage-4A behavior,
        which left a crash anywhere in a 500-row commit with ZERO durable
        progress, so a naive retry would blindly re-run every "create"
        decision and produce real duplicate contacts). This bounds the
        general unsafe crash window to a single row.

        CREATE gets one further, deliberate refinement (2026-09-03,
        post-review): a "create" decision is NOT applied in one step. This
        method first durably marks the row `commit_outcome = "creating"`
        (a TRANSIENT, non-terminal value -- see CrmImportRowPreview's own
        docstring) BEFORE attempting `create_contact_from_import()`, then
        only afterward resolves it to a terminal "created"/"error". This
        is what lets a resumed call tell "this row's create was already
        ATTEMPTED once" (commit_outcome == "creating") apart from "this
        row has never been decided at all" (commit_outcome is None) --
        an ambiguity that matters because those two situations must be
        handled completely differently: the former is safe to recover
        automatically (see below); reinterpreting the latter would
        silently override a human's (or _default_decision()'s) own
        create/update/skip choice, which this method must never do.

        For a row found in the "creating" state, this method recovers
        rather than blindly re-creating: `_find_existing_by_confident_
        identifiers()` looks the row's OWN email/apollo_contact_id/
        linkedin_url up directly against CrmContactStore -- the same
        three tiers classify_match() uses, in the same order, but
        deliberately NOT classify_match() itself (no conflict-downgrade,
        no name+company fallback tier -- see that helper's own docstring
        for exactly why re-running that POLICY here would risk
        reinterpreting the row's already-made decision). If found, that
        IS this row's own earlier create having actually landed --
        recovered as "created" with the correct resolved_contact_id,
        zero new CrmContact rows. If not found, no attempt has actually
        landed yet (the crash was before create_contact_from_import()
        ever ran, or before it returned) -- attempt it now, for real.

        This closes the duplicate-creation gap for every row carrying at
        least one confident identifier (email, apollo_contact_id, or
        linkedin_url) -- which CrmContactStore's own UNIQUE constraints
        (see SQLiteCrmContactStore's schema) ALSO already independently
        guard: even without this recovery logic, a blind second `create_
        contact_from_import()` for such a row would fail with a UNIQUE
        violation rather than silently duplicating (caught by the same
        try/except as any other row-processing failure, landing on
        "error" -- see _attempt_create()). The recovery logic's real
        value is turning that failure into a correct "created" outcome
        with the right resolved_contact_id, instead of an "error" that
        would incorrectly hide an already-successfully-created contact
        from list_resolved_contact_ids().

        REMAINING GAP, quantified precisely: a row with NO confident
        identifier at all (blank email, blank apollo_contact_id, blank
        linkedin_url -- classified NEW or POSSIBLE_DUPLICATE purely via
        the name+company fallback tier, or with no identifying data
        whatsoever) has nothing for CrmContactStore's UNIQUE constraints
        OR this recovery lookup to key off. For such a row ONLY, the
        exact crash window this method cannot close: contact created,
        crash before the "creating" checkpoint's own save() completes,
        retry finds no durable marker at all and no recoverable match,
        genuinely creates a second contact. Deliberately NOT mitigated by
        falling back to a name+company lookup here: unlike email/apollo/
        linkedin, name+company is NOT unique in this CRM (multiple real
        contacts can share it -- that's exactly why it's a fallback tier,
        never confident), so a name+company-based "recovery" could not
        reliably distinguish "the contact THIS row's own earlier attempt
        created" from "a different, unrelated existing contact that
        happens to share a name and company" -- silently merging into the
        wrong one would be worse than the gap itself. This residual gap
        is real but narrow, and irrelevant to this codebase's actual
        current consumer of resolved contacts (a campaign-scoped Add
        Prospects cohort, Stage 3/4B) since that consumer already excludes
        every contact lacking a usable email before freezing its cohort --
        a row that can hit this gap could never have contributed to that
        cohort in the first place.

        Beyond CREATE, this residual-crash-window concern does not apply:
        UPDATE's `apply_import_mapping()` merge is fill-only, so blindly
        re-running it against the same target with the same mapped_fields
        on a genuine retry is a true no-op (already verified by this
        file's own test suite) -- no equivalent "creating" checkpoint is
        needed there.

        `CrmImportBatchStatus.COMMITTING` marks "an attempt has begun, not
        every row is durably resolved yet" -- distinct from MAPPED ("never
        attempted") and COMMITTED ("every row resolved, report finalized,
        event logged"). Set once, on the first call, and left alone on a
        resumed call (no-op re-write avoided). preview() refuses to run
        again once a batch reaches COMMITTING/COMMITTED (see that method's
        own guard) so this progress can never be silently discarded.

        ACTIVITY LOG ORDERING (verified, 2026-09-03): `batch.status` is
        saved as COMMITTED strictly BEFORE the one `import.completed`
        event is recorded -- so a crash in the narrow gap between those
        two statements leaves the batch correctly, durably COMMITTED
        forever, but the completion event permanently unwritten (the
        COMMITTED fast-path at the top of this method returns immediately
        on any later call, never re-entering the code that logs it).
        `import.completed` is therefore AT MOST once, best-effort -- never
        exactly-once, and this is intentional, not an oversight: it
        matches ActivityLogService.record()'s own explicitly documented
        contract ("BEST EFFORT and must NEVER raise... every call site
        invokes this AFTER its own store write, never before or wrapping
        it in a shared transaction") and this codebase's one other
        analogous case (MailCampaignService._reconcile_batch()'s
        "mail_campaign.activated"/reactivated event, logged strictly after
        the campaign's own status flip, with the identical accepted
        lost-on-crash gap). The reverse ordering (log first, flip after)
        was deliberately rejected: it would risk the opposite, and worse,
        failure -- a crash between the log write and the COMMITTED flip
        would leave the batch not-yet-COMMITTED, so a resumed call would
        re-enter the finalize block and log the event a SECOND time.
        Losing an audit-trail entry is an accepted, bounded risk; a
        duplicated one, or worse, a duplicated CRM mutation, is not.

        Matching/classification/merge policy is completely untouched: every
        row's `status`/`matched_contact_id`/`matched_on` (set once, by
        preview()) and the exact same `_default_decision()`/
        `apply_import_mapping()` calls decide what happens to a row the
        FIRST time it's ever processed here -- nothing about WHICH decision
        a row gets, or what create/update actually does, changed in this
        pass. `created_in_this_commit` (resolving a same-file "update the
        row created earlier" reference) is now seeded from every row's
        already-durable `resolved_contact_id`, not just ones created within
        this specific call, so that cross-reference still resolves
        correctly on a resumed call.
        """
        batch = await self._require_batch(import_batch_id)
        if batch.preview is None:
            raise ValueError("Must call preview() before commit()")

        if batch.status == CrmImportBatchStatus.COMMITTED:
            if all(row.commit_outcome is None for row in batch.preview):
                # A genuinely pre-existing production case, not a
                # hypothetical: CrmImportBatch already had real committed
                # rows in production before Stage 4A added commit_outcome
                # tracking, so their preview rows have no durable outcome
                # to reconstruct a report from. Refusing loudly here is
                # strictly safer than the pre-Stage-4A behavior (which
                # would have silently RE-COMMITTED the whole batch, creating
                # real duplicate contacts) and safer than silently
                # returning a misrepresentative all-zero report.
                raise ValueError(
                    f"CrmImportBatch {import_batch_id} was committed before per-row commit "
                    "outcome tracking existed (Stage 4A, 2026-09-03) -- its original report "
                    "cannot be reconstructed from durable state. No action was taken."
                )
            return self._compute_report(batch.preview)

        decisions = decisions or {}

        if batch.status != CrmImportBatchStatus.COMMITTING:
            batch.status = CrmImportBatchStatus.COMMITTING
            await self.batch_store.save(batch)

        created_in_this_commit: dict[int, str] = {
            row.row_index: row.resolved_contact_id for row in batch.preview if row.resolved_contact_id is not None
        }

        for row in batch.preview:
            if row.commit_outcome == "creating":
                # A create attempt for THIS row already began in an earlier,
                # interrupted call -- recover rather than blindly retry. See
                # this method's own docstring for the full reasoning.
                recovered = await self._find_existing_by_confident_identifiers(row.mapped_fields)
                if recovered is not None:
                    row.commit_outcome = "created"
                    row.resolved_contact_id = recovered.crm_contact_id
                    created_in_this_commit[row.row_index] = recovered.crm_contact_id
                else:
                    await self._attempt_create(row, created_in_this_commit)
                await self.batch_store.save(batch)
                continue

            if row.commit_outcome is not None:
                continue  # already durably resolved by this or an earlier call -- never revisited

            if row.status == CrmImportRowStatus.ERROR:
                # Matches the pre-Stage-4A aggregate exactly: a row that
                # failed classification at PREVIEW time counts toward
                # `skipped`, not `errors`, in the final report.
                row.commit_outcome = "skipped"
                await self.batch_store.save(batch)
                continue

            decision = decisions.get(row.row_index, self._default_decision(row.status))

            if decision == "create":
                # Durable "an attempt is starting" marker, written BEFORE
                # the mutation -- see this method's own docstring for why
                # this two-phase write is specifically needed for create
                # (and only create).
                row.commit_outcome = "creating"
                await self.batch_store.save(batch)
                await self._attempt_create(row, created_in_this_commit)
                await self.batch_store.save(batch)
                continue

            try:
                if decision == "skip":
                    row.commit_outcome = "skipped"
                elif decision == "update":
                    target_id = row.matched_contact_id
                    if target_id is None and row.matched_on and row.matched_on.startswith("within_file_row_"):
                        ref_row = int(row.matched_on.removeprefix("within_file_row_"))
                        target_id = created_in_this_commit.get(ref_row)
                    existing = await self.crm_service.contact_store.get(target_id) if target_id is not None else None
                    if existing is None:
                        row.commit_outcome = "error"
                    else:
                        merged = self.crm_service.apply_import_mapping(existing, row.mapped_fields, is_new=False)
                        await self.crm_service.contact_store.save(merged)
                        row.commit_outcome = "updated"
                        row.resolved_contact_id = target_id
                else:
                    row.commit_outcome = "error"
            except Exception:
                row.commit_outcome = "error"

            # Durable checkpoint, immediately after THIS row's own outcome
            # is decided -- see this method's own docstring for exactly
            # what crash window this bounds.
            await self.batch_store.save(batch)

        # Reaching this point means the loop above ran to completion without
        # being interrupted -- every row in batch.preview now has a non-None
        # commit_outcome (either just set, or already durable from an
        # earlier call). Finalize exactly once.
        batch.status = CrmImportBatchStatus.COMMITTED
        await self.batch_store.save(batch)

        report = self._compute_report(batch.preview)

        # ONE summary event per commit() call that actually finalizes a
        # batch -- never one per row, and never again on a later no-op call
        # against an already-COMMITTED batch (that path returns above,
        # before this line is ever reached again). Reuses CrmService's own
        # ActivityLogService rather than taking a separate dependency,
        # since this service is always constructed with a crm_service that
        # already has one (see app/main.py's lifespan wiring).
        await self.crm_service.activity_log.record(
            event_type="import.completed",
            category=ActivityCategory.IMPORTS,
            source=ActivitySource.CSV_IMPORT,
            summary=(
                f"CSV import completed: {report.created} created, {report.updated} updated, "
                f"{report.skipped} skipped, {report.errors} error{'s' if report.errors != 1 else ''} "
                f'("{batch.filename}").'
            ),
            entity_type="import_batch",
            entity_id=batch.import_batch_id,
            entity_name=batch.filename,
            metadata={"created": report.created, "updated": report.updated, "skipped": report.skipped, "errors": report.errors},
        )
        return report

    async def list_resolved_contact_ids(self, import_batch_id: str) -> list[str]:
        """Every unique CrmContact id this import batch actually resolved --
        both newly created and updated/matched-existing rows, in file
        order, deduped (two CSV rows resolving to the SAME contact --
        e.g. a within-file duplicate, or two rows independently matching
        the same existing contact -- contribute only once). Excludes
        skipped/error rows and any row with no resolved contact.

        Requires the batch to be fully COMMITTED (raises ValueError
        otherwise, including for a batch still COMMITTING) -- an
        incomplete commit's outcomes are incomplete by definition, and
        returning a partial list here would silently misrepresent the
        import as more finished than it actually is.

        This is the narrow, read-only entry point a campaign-scoped CSV
        Add Prospects operation (Stage 4B, not yet implemented) will
        consume to resolve its candidate cohort -- deliberately just a
        list of ids, nothing more, so that consumer never needs to touch
        CrmImportRowPreview/mapped_fields/matching internals directly."""
        batch = await self._require_batch(import_batch_id)
        if batch.status != CrmImportBatchStatus.COMMITTED:
            raise ValueError(
                f"CrmImportBatch {import_batch_id} is not fully committed (status={batch.status.value}) -- "
                "resolved contact ids are only available once every row has been durably resolved."
            )
        if batch.preview is None:
            return []
        return list(dict.fromkeys(row.resolved_contact_id for row in batch.preview if row.resolved_contact_id is not None))
