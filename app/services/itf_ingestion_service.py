"""
ItfIngestionService -- turns one Google Apps Script webhook call (one ITF
Form submission) into a call to CrmImportService.import_one_row(), reusing
the exact same classification/dedup/merge pipeline CSV import uses (see
crm_import_service.py/crm_service.py/crm_classification_rules.py). No
ITF-specific classification, dedup, or merge logic exists anywhere -- the
only ITF-specific code in this whole pipeline is: turning the Apps Script
payload into a raw_row dict, the dietary-preferences delimiter bridge
(_normalize_dietary_preferences_delimiter -- see its docstring; it
retargets one column's format only, never touches classify_dietary_
preferences itself or anything CSV import relies on), the idempotency
ledger below, and the two post-resolution automations below
(ITF_EMAIL_STATUS_VALUE / _add_to_itf_submissions_list): every real
ITF submission that resolves to a written contact (created or updated,
never a possible_duplicate) gets email_status filled in via the same
extra_fields mechanism "source"/itf_submitted_at already use (so it goes
through CrmService's existing, unmodified fill-only-if-blank merge rule --
no bespoke overwrite logic), and is added to the "ITF Submissions" CRM
list via CrmService's existing, already-idempotent list API. Neither
touches CSV import, manual contact creation, Email Intake, or Apollo
imports -- both are reachable only from this file.

No Google credentials of any kind live in this backend -- a Google Apps
Script bound to the ITF response Sheet (an installable onFormSubmit
trigger) does the only reading of the Sheet, and POSTs the result to
POST /sync/itf-contact (app/api/sync.py), authenticated by a shared-secret
bearer token (ITF_WEBHOOK_TOKEN). This service never talks to Google.

Header disambiguation
----------------------
Section 2 (private) and Section 3 (institutional) of the real Investor
Thesis Form ask several questions with IDENTICAL wording (asset types,
business models, industries, deal stages, meeting preferences, demographic
preferences, and likely others) -- the Sheet therefore has two columns
sharing the exact same header text. Apps Script sends `headers`/`values` as
POSITIONAL parallel arrays specifically to avoid this collision (its own
e.namedValues is a plain object keyed by header text and would silently
drop one section's answer). _disambiguate_headers() below resolves it the
same way the original polling design did, with no hardcoded assumption
about exactly which questions repeat or where Section 3 starts/ends:
scanning left to right, the FIRST time a header text is seen it's used
as-is; every later time the exact same text is seen, " (Institutional)" is
appended before it's used as a dict key. This is the same convention
crm_classification_rules.py's classify_check_size already uses for "Check
Size" / "Check Size (Institutional)" -- any rule that needs to distinguish
the two sections' answers for a shared question just declares both alias
spellings (see classify_thesis_checklist_fields).

The header wording below (both in _SCALAR_COLUMN_ALIASES and in the
classification rules' own aliases) was verified 2026-08-11 against the real
Sheet's actual header row (26 columns, A-Z, read via a live read-only audit
-- see that audit for the full column-by-column comparison). Confirmed real
duplicate pairs (byte-identical private/institutional wording): asset
types, business models, industries, check size, deal stages, and meeting
preferences. Demographic preferences is NOT a byte-identical duplicate --
the institutional wording drops the word "any" -- so its two column keys
never collide in _disambiguate_headers() at all; classify_thesis_
checklist_fields declares both exact strings explicitly rather than relying
on the auto-suffix pattern for that one field. A wrong alias is still
self-correcting and never silently wrong: it just leaves that field
unmapped for every row (visible in a dry run's `warnings`), never a value
written to the wrong field.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact, CrmImportRowStatus, CustomFieldType
from app.models.itf import ItfIngestionLogEntry, ItfRowStatus, ItfWebhookResult
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.itf_ingestion_log_store import ItfIngestionLogStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_classification_rules import (
    _normalize_header,
    build_classification_context,
    thesis_checklist_aliases,
)
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmDuplicateFieldKeyError


def _entity_name_from_mapped(mapped: dict[str, Any]) -> str | None:
    """Best-effort human label for the Activity Log -- first/last name if either is
    present, else the email, else None (never a blank string)."""
    name = " ".join(part for part in (mapped.get("first_name"), mapped.get("last_name")) if part).strip()
    return name or mapped.get("email") or None

# Best-effort scalar column_mapping -- see module docstring. Multi-select/checklist/
# check-size/dietary-preference fields are deliberately NOT here; those are handled
# by classification rules (crm_classification_rules.py), which read directly from
# the raw row independent of this mapping.
_SCALAR_COLUMN_ALIASES: dict[str, str] = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Email Address": "email",
    "LinkedIn Profile URL": "linkedin_url",
    "Which city/cities do you live in or frequent?": "thesis_cities",
    "Do you invest privately or institutionally (or both)?": "thesis_investor_mode",
    "Do you have any other criteria or feedback?": "thesis_private_other_criteria",
    "Do you also invest institutionally (via a fund)?": "thesis_also_invests_institutionally",
    "Do you have any other criteria or feedback? (Institutional)": "thesis_institutional_other_criteria",
    "Want us to invite/include other investor-friends? If so, enter their email(s) here.": "thesis_referral_emails",
}

_TIMESTAMP_COLUMN_ALIASES = ("Timestamp",)
# Best-effort Google Forms timestamp formats -- exact format/timezone is one of the
# explicit field-mapping-audit verification items. An unparseable value is never
# guessed at: the submission is still processed, itf_submitted_at is simply left
# unset for it, and a warning is added to the result so this list can be corrected
# once the real format is seen.
_TIMESTAMP_FORMATS = ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M")

_STATUS_STRINGS = {
    CrmImportRowStatus.NEW: ItfRowStatus.CREATED,
    CrmImportRowStatus.EXISTING: ItfRowStatus.UPDATED,
    CrmImportRowStatus.POSSIBLE_DUPLICATE: ItfRowStatus.POSSIBLE_DUPLICATE,
    CrmImportRowStatus.ERROR: ItfRowStatus.ERROR,
}

ITF_SUBMITTED_AT_FIELD_KEY = "itf_submitted_at"

# The email address on an ITF submission was typed by the person it belongs to --
# treat it as valid unless later evidence changes that status. `email_status` is
# CrmContact's existing, pre-existing free-text field (see app/models/crm.py); no new
# field is introduced. Setting it via `extra_fields` (below) is deliberate: it is the
# SAME mechanism already used for "source"/itf_submitted_at, so it flows through
# CrmService.apply_import_mapping's existing, unmodified EXTERNAL_FIELD_NAMES
# fill-only-if-blank rule -- the exact same rule every other external field (email,
# phone, company, ...) already gets on every import path. That rule is what actually
# implements the safety requirement here: a contact whose email_status is already
# ANY nonblank value (Valid, Invalid, Bounced, Do Not Contact, ...) is left untouched;
# only a currently blank/unset email_status is filled in with ITF_EMAIL_STATUS_VALUE.
# No bespoke overwrite-protection logic exists (or is needed) in this file.
ITF_EMAIL_STATUS_VALUE = "Valid"

# Exact list name requested -- looked up by exact string match in
# _ensure_itf_submissions_list_id below; never created twice for the same name (see
# that method's docstring for the one small residual race it does not close, and
# why it's an acceptable tradeoff here).
ITF_SUBMISSIONS_LIST_NAME = "ITF Submissions"


def _content_hash(header_ordered_values: list[str]) -> str:
    """Stable hash of a submission's raw cell values (in header order) --
    detects a resubmission with genuinely different content vs. a retry of
    the exact same trigger firing. \\x1f (unit separator) is used as the
    join delimiter specifically because it can't be typed into a normal
    Sheet cell -- and even in the vanishingly unlikely case of a collision,
    the only consequence is an unnecessary reprocess, never a wrongly-
    skipped one, so exact delimiter safety isn't security-critical here."""
    return hashlib.sha256("\x1f".join(header_ordered_values).encode("utf-8")).hexdigest()


def _disambiguate_headers(headers: list[str]) -> list[str]:
    """See module docstring. The FIRST occurrence of a header text is used
    as-is; every later occurrence gets ' (Institutional)' appended before
    use, so both sections' answers survive as distinct dict keys."""
    seen: set[str] = set()
    result: list[str] = []
    for raw_header in headers:
        header = raw_header.strip()
        if header in seen:
            header = f"{header} (Institutional)"
        seen.add(header)
        result.append(header)
    return result


def _zip_row(headers: list[str], values: list[str]) -> dict[str, str]:
    """Builds the raw_row dict CrmImportService expects from `headers`
    (already disambiguated) and `values` (Apps Script's e.values, positional
    and parallel to the ORIGINAL header row) -- a `values` shorter than
    `headers` (Google Sheets omits trailing empty cells) is treated as blank
    for its missing trailing columns."""
    row: dict[str, str] = {}
    for i, header in enumerate(headers):
        value = values[i] if i < len(values) else ""
        if value and value.strip():
            row[header] = value.strip()
    return row


def _build_column_mapping(headers: list[str]) -> dict[str, str]:
    """Matches each real header against _SCALAR_COLUMN_ALIASES by normalized
    text -- an unmatched real header is simply left out of column_mapping,
    never guessed."""
    normalized_aliases = {_normalize_header(k): v for k, v in _SCALAR_COLUMN_ALIASES.items()}
    mapping: dict[str, str] = {}
    for header in headers:
        target = normalized_aliases.get(_normalize_header(header))
        if target:
            mapping[header] = target
    return mapping


def _find_raw(raw_row: dict[str, str], *aliases: str) -> str | None:
    normalized_aliases = {_normalize_header(a) for a in aliases}
    for header, value in raw_row.items():
        if _normalize_header(header) in normalized_aliases and value:
            return value
    return None


def _parse_submitted_at(raw_row: dict[str, str]) -> str | None:
    raw = _find_raw(raw_row, *_TIMESTAMP_COLUMN_ALIASES)
    if not raw:
        return None
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


_DIETARY_COLUMN_ALIASES = ("Do you have dietary preferences?", "Dietary Preferences", "Dietary Restrictions")


def _normalize_dietary_preferences_delimiter(raw_row: dict[str, str]) -> dict[str, str]:
    """
    ITF-only pre-classifier bridge -- deliberately NOT a change to
    classify_dietary_preferences (crm_classification_rules.py), which stays
    exactly as CSV import already relies on it: every other multi-select
    thesis field in this codebase uses a ';'-separated CSV cell (see
    crm_import_service.py's module docstring -- the convention exists
    because several Investor Thesis option lists contain literal commas,
    and it's applied uniformly across every such field, dietary preferences
    included, rather than decided per-field). CSV behavior must not change.

    Google Forms, on the other hand, joins a checkbox question's selections
    with ', ' -- confirmed live against 6 other multi-select ITF questions
    in the real Sheet (2026-08-11 audit); no real non-blank example existed
    for dietary preferences specifically, so this is the same, consistently-
    observed Google Forms behavior applied to the one question that
    happened to be left blank in the only real response so far.

    This function retargets ONLY the dietary-preferences column (if
    present) from ITF's ', '-joined format into the ';'-joined format the
    shared classifier expects, then lets that classifier do everything else
    unchanged: recognition against DIETARY_PREFERENCE_OPTIONS, "Other"
    overflow handling, all of it. It uses greedy known-option matching
    (_split_known_options) rather than a blind comma-split specifically so a
    human's free-typed "Other" answer that happens to contain its own comma
    (e.g. "No pork, no shellfish") is never shredded into two garbage
    fragments -- DIETARY_PREFERENCE_OPTIONS itself has no internal commas,
    so every recognized option still matches cleanly regardless. A no-op
    (returns raw_row unchanged) for any row without a dietary-preferences
    column at all -- i.e. this function is never reachable from CSV import,
    which never calls it.
    """
    from app.models.crm import DIETARY_PREFERENCE_OPTIONS
    from app.services.crm_classification_rules import _split_known_options

    normalized_aliases = {_normalize_header(a) for a in _DIETARY_COLUMN_ALIASES}
    key = next((h for h in raw_row if _normalize_header(h) in normalized_aliases), None)
    if key is None or not raw_row[key]:
        return raw_row

    matched, leftover = _split_known_options(raw_row[key], list(DIETARY_PREFERENCE_OPTIONS))
    segments = list(matched)
    if leftover:
        segments.append(leftover)
    if not segments:
        return raw_row

    return {**raw_row, key: "; ".join(segments)}


class ItfIngestionService:
    """
    process_submission() is the whole surface area: one Apps Script webhook
    call in, one ItfWebhookResult out. Skips (reports ALREADY_PROCESSED,
    never re-processes) a row whose ingestion-log entry already has a
    matching content_hash and a non-error status -- see
    ItfIngestionLogEntry's docstring for the full skip/retry/reprocess rule.
    Never writes to the CRM, the ingestion log, or the itf_submitted_at
    custom field definition when `dry_run=True`.
    """

    def __init__(
        self,
        import_service: CrmImportService,
        log_store: ItfIngestionLogStore,
        activity_log: ActivityLogService | None = None,
    ):
        self.import_service = import_service
        self.log_store = log_store
        self.activity_log = activity_log or ActivityLogService(MemoryActivityEventStore())

    async def process_submission(
        self,
        headers: list[str],
        values: list[str],
        row_number: int,
        response_id: str | None = None,
        dry_run: bool = False,
    ) -> ItfWebhookResult:
        disambiguated_headers = _disambiguate_headers(headers)
        raw_row = _zip_row(disambiguated_headers, values)
        # Hash the ORIGINAL submitted content, before the dietary-preferences
        # delimiter bridge below -- idempotency should reflect what was actually
        # submitted, not a normalized derivative of it (the two are equivalent for
        # correctness either way, since normalization is deterministic, but hashing
        # the original keeps the ledger's content_hash meaningful for debugging).
        content_hash = _content_hash([raw_row.get(h, "") for h in disambiguated_headers])
        raw_row = _normalize_dietary_preferences_delimiter(raw_row)

        prior = await self.log_store.get(row_number)
        if prior is not None and prior.status != ItfRowStatus.ERROR and prior.content_hash == content_hash:
            return ItfWebhookResult(
                status=ItfRowStatus.ALREADY_PROCESSED.value,
                dry_run=dry_run,
                contact_id=prior.crm_contact_id,
            )

        if not dry_run:
            await self._ensure_submitted_at_field_exists()

        column_mapping = _build_column_mapping(disambiguated_headers)
        classification_context = await build_classification_context(self.import_service.crm_service.custom_field_store)

        warnings: list[str] = []
        submitted_at = _parse_submitted_at(raw_row)
        extra_fields: dict[str, Any] = {"source": "itf", "email_status": ITF_EMAIL_STATUS_VALUE}
        if submitted_at:
            extra_fields[f"custom:{ITF_SUBMITTED_AT_FIELD_KEY}"] = submitted_at
        else:
            warnings.append(f"Timestamp value not recognized -- {ITF_SUBMITTED_AT_FIELD_KEY} left unset")

        normalized_real_headers = {_normalize_header(h) for h in disambiguated_headers}
        unmapped = [
            alias for alias in _SCALAR_COLUMN_ALIASES if _normalize_header(alias) not in normalized_real_headers
        ]
        if unmapped:
            warnings.append(
                "No matching column found for expected question(s) -- verify exact wording against "
                f"the real header row: {', '.join(unmapped)}"
            )

        # Same check for the checklist-field aliases (asset types, business models,
        # deal stages, meeting/demographic preferences, ...) -- these live in
        # crm_classification_rules.py, not _SCALAR_COLUMN_ALIASES, so without this
        # they'd silently produce missing/misplaced fields with zero warning at all.
        # This is exactly the class of bug a wrong private/institutional alias pair
        # caused for demographic preferences (fixed 2026-08-12): the institutional
        # alias collided with the private column's real key, so institutional data
        # silently overwrote private data and the private target was left unset --
        # with no warning to catch it. This check exists so that never happens again
        # unnoticed.
        unmapped_checklist = [
            alias for alias in thesis_checklist_aliases() if _normalize_header(alias) not in normalized_real_headers
        ]
        if unmapped_checklist:
            warnings.append(
                "No matching column found for expected checklist question(s) -- verify exact "
                f"wording against the real header row: {', '.join(unmapped_checklist)}"
            )

        now = datetime.now(timezone.utc)
        mapped: dict[str, Any] = {}
        error_message: str | None = None
        try:
            status, contact, matched_on, mapped = await self.import_service.import_one_row(
                raw_row, column_mapping, classification_context, extra_fields=extra_fields, dry_run=dry_run
            )
            itf_status = _STATUS_STRINGS[status]
        except Exception as e:
            itf_status, contact, matched_on = ItfRowStatus.ERROR, None, None
            error_message = str(e)

        # Only for a submission that was actually resolved to a real, written
        # contact -- CREATED or UPDATED. Deliberately excludes POSSIBLE_DUPLICATE
        # (contact here is the unmodified existing candidate flagged for human
        # review, never written -- see import_one_row) and ERROR (contact is None
        # above). Best-effort: a failure here never flips itf_status/error_message,
        # since the contact write itself already genuinely succeeded -- it's
        # surfaced as a warning instead (see _add_to_itf_submissions_list).
        if not dry_run and itf_status in (ItfRowStatus.CREATED, ItfRowStatus.UPDATED) and contact is not None:
            await self._add_to_itf_submissions_list(contact, warnings)

        if not dry_run:
            await self.log_store.save(
                ItfIngestionLogEntry(
                    row_number=row_number,
                    content_hash=content_hash,
                    status=itf_status,
                    response_id=response_id,
                    crm_contact_id=contact.crm_contact_id if contact else None,
                    email=mapped.get("email"),
                    error_message=error_message,
                    processed_at=now,
                )
            )
            await self._record_activity(itf_status, contact, mapped, error_message)

        return ItfWebhookResult(
            status=itf_status.value,
            dry_run=dry_run,
            contact_id=contact.crm_contact_id if contact else None,
            matched_on=matched_on,
            mapped_fields=mapped if dry_run else None,
            warnings=warnings,
            error=error_message,
        )

    async def _record_activity(
        self,
        itf_status: ItfRowStatus,
        contact: CrmContact | None,
        mapped: dict[str, Any],
        error_message: str | None,
    ) -> None:
        """One `itf.submission_received` event for every real (non-dry-run,
        non-already-processed -- see the early-return above, which happens
        before this is ever reached) submission, plus exactly one outcome
        event: `itf.contact_created`/`itf.contact_updated` when a contact was
        created/matched, or `itf.processing_failed` on error. POSSIBLE_DUPLICATE
        rows get only the submission_received event -- nothing was actually
        created or updated, so no contact.* event would be honest here.
        Best-effort throughout (ActivityLogService.record() never raises)."""
        name = _entity_name_from_mapped(mapped)
        await self.activity_log.record(
            event_type="itf.submission_received",
            category=ActivityCategory.ITF,
            source=ActivitySource.ITF_AUTOMATION,
            summary=f"{name or 'Someone'} submitted the Investor Thesis Form.",
            entity_type="contact" if contact else None,
            entity_id=contact.crm_contact_id if contact else None,
            entity_name=name,
            metadata={"submitted_at": mapped.get(f"custom:{ITF_SUBMITTED_AT_FIELD_KEY}")},
        )

        if itf_status == ItfRowStatus.CREATED and contact is not None:
            await self.activity_log.record(
                event_type="itf.contact_created",
                category=ActivityCategory.ITF,
                source=ActivitySource.ITF_AUTOMATION,
                summary=f"{name or 'A contact'} was created in the CRM from the Investor Thesis Form.",
                entity_type="contact",
                entity_id=contact.crm_contact_id,
                entity_name=name,
                metadata={"result": "created"},
            )
        elif itf_status == ItfRowStatus.UPDATED and contact is not None:
            await self.activity_log.record(
                event_type="itf.contact_updated",
                category=ActivityCategory.ITF,
                source=ActivitySource.ITF_AUTOMATION,
                summary=f"{name or 'An existing contact'} was updated from a new Investor Thesis Form submission.",
                entity_type="contact",
                entity_id=contact.crm_contact_id,
                entity_name=name,
                metadata={"result": "updated"},
            )
        elif itf_status == ItfRowStatus.ERROR:
            await self.activity_log.record(
                event_type="itf.processing_failed",
                category=ActivityCategory.ERRORS,
                source=ActivitySource.ITF_AUTOMATION,
                summary=f"ITF submission from {name or 'an unknown submitter'} failed to process.",
                entity_type="contact" if contact else None,
                entity_id=contact.crm_contact_id if contact else None,
                entity_name=name,
                metadata={"error": error_message},
            )

    async def _add_to_itf_submissions_list(self, contact: CrmContact, warnings: list[str]) -> None:
        """
        Best-effort membership add for the 'ITF Submissions' list -- never
        raises, and never flips the submission's overall status. The contact
        create/update this runs after already genuinely succeeded (see the
        call site's guard); a failure here (e.g. a transient store error) is
        surfaced only as a warning on the response, exactly like every other
        non-fatal issue this service already reports (unmapped columns,
        unparsed timestamp) -- never a reason to report ERROR for a row whose
        CRM write already committed.

        Idempotent by construction, with zero new mechanism: CrmService.
        bulk_add_to_list() -- the CRM's existing, already-idempotent list API
        (see crm_contact_list_member_store.py: add() is keyed on
        (list_id, crm_contact_id), a repeat add is a genuine no-op at the store
        layer) -- is called with this one contact_id. A repeat ITF submission
        for the same contact (whether a retried row or a second real
        submission from the same person) calls this again with the same
        (list_id, contact_id) pair and creates no duplicate membership row,
        and fires no duplicate activity event (bulk_add_to_list only records
        list.contacts_added when it actually added someone).
        """
        try:
            list_id = await self._ensure_itf_submissions_list_id()
            await self.import_service.crm_service.bulk_add_to_list(list_id, [contact.crm_contact_id])
        except Exception as e:
            warnings.append(f"Could not add this contact to the '{ITF_SUBMISSIONS_LIST_NAME}' list: {e}")

    async def _ensure_itf_submissions_list_id(self) -> str:
        """
        Get-or-create for the 'ITF Submissions' CRM list, by exact name match
        against CrmService.list_contact_lists() -- the CRM's existing list
        registry, not a parallel one. Scoped entirely to this ITF service; the
        generic Lists feature (crm_service.py, GET/POST /crm/lists) is
        unmodified, so every other caller of Lists is unaffected.

        Residual note: list names have no store-level uniqueness constraint
        today (unlike, e.g., custom field keys -- see
        _ensure_submitted_at_field_exists's CrmDuplicateFieldKeyError handling
        just below), so two truly concurrent first-ever ITF submissions could
        theoretically both observe "not found" and both create a list named
        'ITF Submissions'. Real ITF submissions arrive one at a time from a
        single Google Apps Script onFormSubmit trigger firing per Sheet row,
        so this is not a realistic risk in practice -- flagged here rather
        than silently assumed away, and closing it fully would mean adding a
        uniqueness constraint to the shared Lists store, which is out of
        scope for an ITF-only change.
        """
        existing_lists = await self.import_service.crm_service.list_contact_lists()
        for contact_list in existing_lists:
            if contact_list.name == ITF_SUBMISSIONS_LIST_NAME:
                return contact_list.list_id
        created = await self.import_service.crm_service.create_contact_list(
            name=ITF_SUBMISSIONS_LIST_NAME,
            description="Contacts who have submitted the Investor Thesis Form.",
        )
        return created.list_id

    async def _ensure_submitted_at_field_exists(self) -> None:
        """
        Get-or-create for the itf_submitted_at custom field DEFINITION (DATE
        type) -- needed only so it shows up in the CRM's admin UI / More
        Filters registry; the underlying data write (custom_fields dict key)
        works regardless of whether a definition exists. Idempotent, and
        deliberately never called when dry_run=True -- a dry run must never
        write anything to the CRM, including a field definition.
        """
        custom_field_store = self.import_service.crm_service.custom_field_store
        existing = await custom_field_store.get_by_field_key(ITF_SUBMITTED_AT_FIELD_KEY)
        if existing is not None:
            return
        try:
            await self.import_service.crm_service.create_custom_field(
                field_key=ITF_SUBMITTED_AT_FIELD_KEY,
                label="ITF Submitted At",
                field_type=CustomFieldType.DATE,
                description="Timestamp of this contact's most recent Investor Thesis Form submission.",
            )
        except CrmDuplicateFieldKeyError:
            pass  # created concurrently by another request -- harmless, already exists
