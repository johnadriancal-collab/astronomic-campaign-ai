"""
EmailIntakeService -- orchestrates the Email -> CRM Intake pipeline:
ingest (idempotent) -> match (CrmService.classify_match(), unmodified) ->
extract (a swappable EmailExtractor) -> human review -> approve/reject.

The one rule that governs everything below: NOTHING in ingest(),
list_items(), get_item(), or manual_match() ever calls
CrmService.update_contact() or any other CRM-mutating method. The ONLY
call site that writes to the CRM in this entire module is inside
approve(), and only after re-fetching the live contact and confirming
every approved field's snapshot still matches. Rejecting, viewing, or
leaving an item in NEEDS_MATCH forever has zero effect on any CrmContact.

Approve() deliberately builds its own small patch dict and calls
CrmService.update_contact() directly -- the same manual-edit path a human
using the contact edit page already goes through -- rather than
CrmImportService.apply_import_mapping()'s fill-only-if-empty merge rule.
That merge rule exists to protect against an UNREVIEWED bulk import
silently overwriting confirmed data; an Email Intake approval is the
opposite case, a human explicitly confirming "yes, overwrite this exact
value" after reading a current -> proposed diff. Using update_contact()
also means every approval automatically gets update_contact()'s own
Activity Log emission (`contact.updated`) for free, IN ADDITION to this
service's own `email_intake.approved` event -- both are correct and
neither suppresses the other (see this module's approve() docstring).
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmImportRowStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.activity_log_service import ActivityLogService
from app.models.email_intake import (
    ApproveEmailIntakeResult,
    EmailAttachmentMeta,
    EmailIntakeItem,
    EmailIntakeStatus,
    EmailIntakeWebhookRequest,
    EmailIntakeWebhookResult,
    StaleFieldConflict,
)
from app.repositories.email_intake_store import EmailIntakeDuplicateError, EmailIntakeStore
from app.services.crm_service import CUSTOM_FIELD_PREFIX, CrmContactNotFound, CrmService, _contact_display_name
from app.services.email_intake_extraction import (
    _LINKEDIN_RE,
    DeterministicEmailExtractor,
    EmailExtractor,
    build_email_extraction_context,
)


class EmailIntakeItemNotFound(Exception):
    pass


class EmailIntakeInvalidStateError(Exception):
    """Raised when an action is attempted against an item in a status that
    doesn't support it -- e.g. approving an already-Rejected item, or
    manually matching an item that isn't NEEDS_MATCH."""


def _parse_sender(sender: str) -> tuple[str | None, str | None]:
    """"Display Name <email>" or a bare email string -> (email, display_name)."""
    match = re.match(r'^\s*"?([^"<]*)"?\s*<([^>]+)>\s*$', sender)
    if match:
        display_name = match.group(1).strip() or None
        return match.group(2).strip() or None, display_name
    return sender.strip() or None, None


def _split_display_name(display_name: str | None) -> tuple[str | None, str | None]:
    if not display_name:
        return None, None
    parts = display_name.split()
    if len(parts) < 2:
        return None, None
    return parts[0], " ".join(parts[1:])


def _read_contact_field(contact: Any, field_key: str) -> Any:
    if field_key.startswith(CUSTOM_FIELD_PREFIX):
        return contact.custom_fields.get(field_key[len(CUSTOM_FIELD_PREFIX) :])
    return getattr(contact, field_key, None)


def _build_patch(field_keys: set[str], proposal_by_key: dict[str, Any]) -> dict[str, Any]:
    """Translates a set of approved EmailCrmFieldChange.field_key values
    into the flat/nested patch shape CrmService.update_contact() expects
    -- core/thesis fields stay top-level, "custom:x" fields nest under
    "custom_fields" (stripped of the prefix, which is this module's own
    wire convention, not update_contact()'s)."""
    patch: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}
    for key in field_keys:
        change = proposal_by_key[key]
        if key.startswith(CUSTOM_FIELD_PREFIX):
            custom_fields[key[len(CUSTOM_FIELD_PREFIX) :]] = change.proposed_value
        else:
            patch[key] = change.proposed_value
    if custom_fields:
        patch["custom_fields"] = custom_fields
    return patch


class EmailIntakeService:
    def __init__(
        self,
        store: EmailIntakeStore,
        crm_service: CrmService,
        activity_log: ActivityLogService | None = None,
        extractor: EmailExtractor | None = None,
    ):
        self.store = store
        self.crm_service = crm_service
        self.activity_log = activity_log or ActivityLogService(MemoryActivityEventStore())
        self.extractor = extractor or DeterministicEmailExtractor()

    # ---- ingestion ---------------------------------------------------------

    async def ingest(self, payload: EmailIntakeWebhookRequest) -> EmailIntakeWebhookResult:
        """
        Idempotent on gmail_message_id: a second call with the same id
        (a retried webhook, or the same synthetic test payload sent twice)
        NEVER creates a second item, NEVER re-runs extraction, and NEVER
        emits a second Activity Log event -- it just returns the existing
        item's current status with already_processed=True.
        """
        existing = await self.store.get_by_gmail_message_id(payload.gmail_message_id)
        if existing:
            return self._webhook_result(existing, already_processed=True)

        now = datetime.now(timezone.utc)
        item = EmailIntakeItem(
            intake_id=str(uuid.uuid4()),
            gmail_message_id=payload.gmail_message_id,
            gmail_thread_id=payload.gmail_thread_id,
            received_at=payload.received_at,
            sender=payload.sender,
            recipients=payload.recipients,
            subject=payload.subject,
            body_text=payload.body_text,
            attachments=payload.attachments,
            status=EmailIntakeStatus.NEEDS_MATCH,
            created_at=now,
        )

        try:
            item = await self._match_and_extract(item)
        except Exception as e:
            logger.error(f"Email intake processing failed for gmail_message_id={payload.gmail_message_id}: {e}")
            item.status = EmailIntakeStatus.ERROR
            item.error_message = str(e)

        try:
            await self.store.create(item)
        except EmailIntakeDuplicateError:
            # Race: a near-simultaneous second call for the same message.
            # Whoever's create() lost returns the winner's item -- still
            # exactly one item, never two.
            existing = await self.store.get_by_gmail_message_id(payload.gmail_message_id)
            return self._webhook_result(existing, already_processed=True)

        await self._record_ingestion_activity(item)
        return self._webhook_result(item, already_processed=False)

    def _webhook_result(self, item: EmailIntakeItem, already_processed: bool) -> EmailIntakeWebhookResult:
        return EmailIntakeWebhookResult(
            intake_id=item.intake_id,
            status=item.status,
            already_processed=already_processed,
            matched_contact_id=item.matched_contact_id,
            matched_on=item.matched_on,
            proposal_field_count=len(item.proposal),
        )

    async def _match_and_extract(self, item: EmailIntakeItem) -> EmailIntakeItem:
        email, display_name = _parse_sender(item.sender)
        first_name, last_name = _split_display_name(display_name)
        linkedin_match = _LINKEDIN_RE.search(item.body_text)
        mapped_fields = {
            "email": email,
            "apollo_contact_id": None,
            "linkedin_url": linkedin_match.group(0) if linkedin_match else None,
            "first_name": first_name,
            "last_name": last_name,
            "company": None,  # no reliable company-for-MATCHING signal from a bare email in Phase 1
        }
        status, matched_contact, matched_on = await self.crm_service.classify_match(mapped_fields)

        if status == CrmImportRowStatus.EXISTING and matched_contact is not None:
            item.matched_contact_id = matched_contact.crm_contact_id
            item.matched_contact_name = _contact_display_name(matched_contact)
            item.matched_on = matched_on
            context = await build_email_extraction_context(self.crm_service.custom_field_store)
            item.proposal = await self.extractor.extract(item.body_text, matched_contact, context)
            item.status = EmailIntakeStatus.PENDING_REVIEW
            return item

        # POSSIBLE_DUPLICATE or NEW -- never auto-attached, per the audit's
        # explicit "do not silently attach an email to a CRM contact when
        # identity is ambiguous" rule. No proposal is generated until a
        # human resolves the match (see manual_match() below).
        item.status = EmailIntakeStatus.NEEDS_MATCH
        item.matched_on = matched_on
        return item

    async def _record_ingestion_activity(self, item: EmailIntakeItem) -> None:
        if item.status == EmailIntakeStatus.ERROR:
            await self.activity_log.record(
                event_type="email_intake.processing_failed",
                category=ActivityCategory.EMAIL_INTAKE,
                source=ActivitySource.EMAIL_INTAKE,
                entity_type="email_intake_item",
                entity_id=item.intake_id,
                summary=f"Email intake processing failed for a message from {item.sender}.",
                metadata={"intake_id": item.intake_id, "sender": item.sender, "subject": item.subject, "error": item.error_message},
            )
        elif item.status == EmailIntakeStatus.NEEDS_MATCH:
            await self.activity_log.record(
                event_type="email_intake.needs_match",
                category=ActivityCategory.EMAIL_INTAKE,
                source=ActivitySource.EMAIL_INTAKE,
                entity_type="email_intake_item",
                entity_id=item.intake_id,
                summary=f"An email from {item.sender} needs manual contact matching.",
                metadata={"intake_id": item.intake_id, "sender": item.sender, "subject": item.subject},
            )
        else:
            await self.activity_log.record(
                event_type="email_intake.proposal_created",
                category=ActivityCategory.EMAIL_INTAKE,
                source=ActivitySource.EMAIL_INTAKE,
                entity_type="email_intake_item",
                entity_id=item.intake_id,
                entity_name=item.matched_contact_name,
                summary=(
                    f"Email intake matched {item.matched_contact_name} with "
                    f"{len(item.proposal)} proposed change(s)."
                    if item.proposal
                    else f"Email intake matched {item.matched_contact_name} with no confidently extracted changes."
                ),
                metadata={
                    "intake_id": item.intake_id,
                    "sender": item.sender,
                    "subject": item.subject,
                    "matched_contact_id": item.matched_contact_id,
                    "field_count": len(item.proposal),
                },
            )

    # ---- queue/review --------------------------------------------------

    async def list_items(self, status: EmailIntakeStatus | None = None, q: str | None = None) -> list[EmailIntakeItem]:
        """Filtering happens here, in Python, over the store's full
        (already newest-first) result -- same convention as
        ActivityLogService.list_events()."""
        items = await self.store.list()

        def matches(item: EmailIntakeItem) -> bool:
            if status and item.status != status:
                return False
            if q:
                haystack = f"{item.sender} {item.subject} {item.matched_contact_name or ''}".lower()
                if q.lower() not in haystack:
                    return False
            return True

        return [i for i in items if matches(i)]

    async def get_item(self, intake_id: str) -> EmailIntakeItem:
        item = await self.store.get(intake_id)
        if item is None:
            raise EmailIntakeItemNotFound(intake_id)
        return item

    # ---- manual match ---------------------------------------------------

    async def manual_match(self, intake_id: str, crm_contact_id: str) -> EmailIntakeItem:
        """
        Reviewer-selected match for a NEEDS_MATCH item. Generates the
        proposal against the CHOSEN contact and moves the item to
        PENDING_REVIEW -- the exact same extraction call ingest() would
        have made had classify_match() been confident on its own. Never
        creates a new contact (out of scope for Phase 1, per the audit).
        """
        item = await self.get_item(intake_id)
        if item.status != EmailIntakeStatus.NEEDS_MATCH:
            raise EmailIntakeInvalidStateError(f"Cannot manually match an item in status {item.status.value}")

        contact = await self.crm_service.get_contact(crm_contact_id)  # raises CrmContactNotFound if missing
        context = await build_email_extraction_context(self.crm_service.custom_field_store)
        item.matched_contact_id = contact.crm_contact_id
        item.matched_contact_name = _contact_display_name(contact)
        item.matched_on = "manual"
        item.proposal = await self.extractor.extract(item.body_text, contact, context)
        item.status = EmailIntakeStatus.PENDING_REVIEW
        await self.store.save(item)

        await self.activity_log.record(
            event_type="email_intake.proposal_created",
            category=ActivityCategory.EMAIL_INTAKE,
            source=ActivitySource.EMAIL_INTAKE,
            entity_type="email_intake_item",
            entity_id=item.intake_id,
            entity_name=item.matched_contact_name,
            summary=(
                f"Email intake manually matched to {item.matched_contact_name} with "
                f"{len(item.proposal)} proposed change(s)."
                if item.proposal
                else f"Email intake manually matched to {item.matched_contact_name} with no confidently extracted changes."
            ),
            metadata={
                "intake_id": item.intake_id,
                "sender": item.sender,
                "subject": item.subject,
                "matched_contact_id": item.matched_contact_id,
                "field_count": len(item.proposal),
            },
        )
        return item

    # ---- approve / reject ------------------------------------------------

    async def approve(self, intake_id: str, field_keys: list[str]) -> ApproveEmailIntakeResult:
        """
        1. Re-fetch the matched contact LIVE (never trust the proposal's
           own snapshot for the write).
        2. Compare each REQUESTED field's live value to what this
           proposal's current_value snapshot recorded. If ANY requested
           field drifted, stop entirely -- no partial-apply of the
           non-stale ones either -- and return status="stale" with every
           drifted field's before/after/proposed values. The returned
           `item` has every proposal row's current_value refreshed to the
           live contact, which IS this feature's "Refresh Proposal": the
           smallest safe behavior is to fold the refresh into the same
           call that discovered the staleness, rather than requiring a
           separate refresh round-trip before the reviewer can even see
           what changed.
        3. Only if nothing is stale: build one patch dict from the
           approved fields and call CrmService.update_contact() ONCE --
           this writes nothing else, mutates no other field.
        """
        item = await self.get_item(intake_id)
        if item.status != EmailIntakeStatus.PENDING_REVIEW:
            raise EmailIntakeInvalidStateError(f"Cannot approve an item in status {item.status.value}")
        if not item.matched_contact_id:
            raise EmailIntakeInvalidStateError("Item has no matched contact to apply changes to.")
        if not field_keys:
            # Enforced here too, not just by ApproveEmailIntakeRequest's
            # Field(min_length=1) -- this service method is a real public
            # entry point in its own right (tests call it directly), so it
            # must never depend solely on the API layer's request-body
            # validation to reject a no-op "Approve" click.
            raise ValueError("At least one field must be selected to approve.")

        proposal_by_key = {c.field_key: c for c in item.proposal}
        unknown = [k for k in field_keys if k not in proposal_by_key]
        if unknown:
            raise ValueError(f"Unknown field_key(s) in approval request: {unknown}")

        try:
            contact = await self.crm_service.get_contact(item.matched_contact_id)
        except CrmContactNotFound as e:
            raise EmailIntakeInvalidStateError(f"Matched contact no longer exists: {item.matched_contact_id}") from e

        requested = set(field_keys)
        conflicts: list[StaleFieldConflict] = []
        refreshed_proposal = []
        for change in item.proposal:
            live_value = _read_contact_field(contact, change.field_key)
            if change.field_key in requested and live_value != change.current_value:
                conflicts.append(
                    StaleFieldConflict(
                        field_key=change.field_key,
                        field_label=change.field_label,
                        reviewed_value=change.current_value,
                        live_value=live_value,
                        proposed_value=change.proposed_value,
                    )
                )
            refreshed_proposal.append(change.model_copy(update={"current_value": live_value}))

        if conflicts:
            item.proposal = refreshed_proposal
            await self.store.save(item)
            return ApproveEmailIntakeResult(status="stale", item=item, conflicts=conflicts)

        patch = _build_patch(requested, proposal_by_key)
        await self.crm_service.update_contact(item.matched_contact_id, patch)

        item.status = EmailIntakeStatus.APPROVED
        item.reviewed_at = datetime.now(timezone.utc)
        await self.store.save(item)

        await self.activity_log.record(
            event_type="email_intake.approved",
            category=ActivityCategory.EMAIL_INTAKE,
            source=ActivitySource.EMAIL_INTAKE,
            entity_type="contact",
            entity_id=item.matched_contact_id,
            entity_name=item.matched_contact_name,
            summary=f"CRM update approved from email intake for {item.matched_contact_name or item.matched_contact_id}.",
            metadata={
                "intake_id": item.intake_id,
                "sender": item.sender,
                "subject": item.subject,
                "matched_contact_id": item.matched_contact_id,
                "fields_updated": sorted(requested),
            },
        )
        return ApproveEmailIntakeResult(status="approved", item=item, conflicts=[])

    async def reject(self, intake_id: str) -> EmailIntakeItem:
        """Rejectable from PENDING_REVIEW *or* NEEDS_MATCH -- a reviewer must
        be able to dismiss an unmatched/irrelevant email without being
        forced to falsely match it to a CRM contact first. Terminal states
        (APPROVED, already REJECTED, ERROR) remain blocked. Never touches
        the CRM in either case -- only this item's own status/reviewed_at."""
        item = await self.get_item(intake_id)
        if item.status not in (EmailIntakeStatus.PENDING_REVIEW, EmailIntakeStatus.NEEDS_MATCH):
            raise EmailIntakeInvalidStateError(f"Cannot reject an item in status {item.status.value}")

        item.status = EmailIntakeStatus.REJECTED
        item.reviewed_at = datetime.now(timezone.utc)
        await self.store.save(item)

        await self.activity_log.record(
            event_type="email_intake.rejected",
            category=ActivityCategory.EMAIL_INTAKE,
            source=ActivitySource.EMAIL_INTAKE,
            entity_type="email_intake_item",
            entity_id=item.intake_id,
            entity_name=item.matched_contact_name,
            summary=f"Email intake proposal for {item.matched_contact_name or item.sender} was rejected.",
            metadata={"intake_id": item.intake_id, "sender": item.sender, "subject": item.subject, "matched_contact_id": item.matched_contact_id},
        )
        return item
