"""
MailCampaignService -- Astronomic Mail Phase 1's campaign/sequence/audience-
review orchestration. Deliberately mirrors CrmService's existing
conventions (list CRUD, patch-key allowlists, activity-log call sites) but
touches NOTHING in that file or in the Apollo-oriented CampaignService
(app/services/campaign_service.py) -- this is a fully separate, additive
service. CrmService is a read-only dependency here (get_contact_list,
get_list_contacts) -- this file never calls anything that mutates a
CrmContact or a CrmContactList/membership.

State machine (see app/models/mail.py's MailCampaignStatus docstring for
why ACTIVE/PAUSED/COMPLETED don't exist as enum members at all yet):

    DRAFT --mark_ready()--> READY --archive_campaign()--> ARCHIVED
      |                        |
      +--archive_campaign()-->ARCHIVED
      |                        |
      +<--unlock_campaign()----+   (deletes the enrollment snapshot)

Only DRAFT is ever editable (create/update campaign fields, add/edit/
delete/reorder steps). mark_ready() is the ONE place that both validates
completeness and creates the audience snapshot (MailEnrollment rows) --
see mark_ready()'s own docstring below for exactly why that boundary was
chosen over snapshotting on Review-view or on every edit.

Nothing in this file calls Gmail, SMTP, or any queue/worker -- there is no
such capability anywhere in this module, by construction, not merely by
omission of a button.
"""

import uuid
from datetime import datetime, time, timezone
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import (
    ALLOWED_MAIL_TEMPLATE_VARIABLES,
    MailCampaign,
    MailCampaignReview,
    MailCampaignSharing,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailScheduleValidationError,
    MailSequenceStep,
    find_unknown_mail_template_variables,
    validate_mail_schedule,
    validate_mail_timezone,
)
from app.repositories.mail_campaign_store import MailCampaignNotFoundError, MailCampaignStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_sequence_step_store import (
    DuplicateMailSequenceStepNumberError,
    MailSequenceStepNotFoundError,
    MailSequenceStepStore,
)
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmContactListNotFound, CrmService

# Practically "everything in the list" -- CrmService.get_list_contacts()
# paginates in-memory with no hard cap, so a large page_size in one call is
# the simplest way to read a full list's membership without inventing a
# second "get all" method on CrmService (which this file must not modify).
_ALL_CONTACTS_PAGE_SIZE = 100_000


class MailCampaignNotFound(Exception):
    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(f"MailCampaign not found: {mail_campaign_id}")


class MailSequenceStepNotFound(Exception):
    def __init__(self, step_id: str):
        self.step_id = step_id
        super().__init__(f"MailSequenceStep not found: {step_id}")


class MailCampaignNotEditableError(Exception):
    """Raised by any mutation (update_campaign, add/update/delete/reorder
    step) attempted while the campaign is not DRAFT. The only way back to
    DRAFT is the explicit unlock_campaign() transition."""

    def __init__(self, mail_campaign_id: str, status: MailCampaignStatus):
        self.mail_campaign_id = mail_campaign_id
        self.status = status
        super().__init__(
            f"MailCampaign {mail_campaign_id} is {status.value} and not editable -- unlock it back to draft first."
        )


class MailCampaignInvalidTransitionError(Exception):
    def __init__(self, mail_campaign_id: str, from_status: MailCampaignStatus, action: str):
        self.mail_campaign_id = mail_campaign_id
        self.from_status = from_status
        self.action = action
        super().__init__(f"Cannot {action} MailCampaign {mail_campaign_id} from status {from_status.value}.")


class MailCampaignNotReadyError(Exception):
    """Raised by mark_ready() when validation fails -- `reasons` lists every
    problem found (not just the first), so the UI can show a complete
    checklist rather than forcing the user to fix issues one at a time."""

    def __init__(self, mail_campaign_id: str, reasons: list[str]):
        self.mail_campaign_id = mail_campaign_id
        self.reasons = reasons
        super().__init__(f"MailCampaign {mail_campaign_id} is not ready: {'; '.join(reasons)}")


class InvalidMailTemplateVariableError(ValueError):
    def __init__(self, unknown_variables: list[str]):
        self.unknown_variables = unknown_variables
        allowed = ", ".join(sorted(ALLOWED_MAIL_TEMPLATE_VARIABLES))
        super().__init__(
            f"Unknown variable(s) {unknown_variables} -- only {{{{...}}}} placeholders from [{allowed}] are allowed."
        )


_CAMPAIGN_PATCH_FIELDS = {
    "name",
    "source_list_id",
    "sending_days",
    "start_time",
    "end_time",
    "timezone",
    "all_hours",
    "sharing",
    "start_immediately",
    "daily_lead_start_limit",
}

_ALL_DAY_SENTINEL_START = time(0, 0)
_ALL_DAY_SENTINEL_END = time(23, 59)


def _parse_time_of_day(value: str) -> time:
    """Accepts 'HH:MM' or 'HH:MM:SS' (time.fromisoformat handles both on
    Python 3.11+) -- raises MailScheduleValidationError (not a bare
    ValueError) on anything else, so the API layer's existing
    MailScheduleValidationError -> 400 mapping covers this too."""
    try:
        return time.fromisoformat(value)
    except ValueError as e:
        raise MailScheduleValidationError(f"'{value}' is not a valid time of day (expected HH:MM).") from e


class MailCampaignService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        step_store: MailSequenceStepStore,
        enrollment_store: MailEnrollmentStore,
        crm_service: CrmService,
        activity_log: ActivityLogService,
    ):
        self.campaign_store = campaign_store
        self.step_store = step_store
        self.enrollment_store = enrollment_store
        self.crm_service = crm_service
        self.activity_log = activity_log

    async def _require_campaign(self, mail_campaign_id: str) -> MailCampaign:
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)
        return campaign

    def _require_draft(self, campaign: MailCampaign) -> None:
        if campaign.status != MailCampaignStatus.DRAFT:
            raise MailCampaignNotEditableError(campaign.mail_campaign_id, campaign.status)

    # --- Campaign CRUD -------------------------------------------------

    async def create_campaign(self, name: str) -> MailCampaign:
        now = datetime.now(timezone.utc)
        campaign = MailCampaign(
            mail_campaign_id=str(uuid.uuid4()), name=name, status=MailCampaignStatus.DRAFT, created_at=now, updated_at=now
        )
        await self.campaign_store.create(campaign)
        await self.activity_log.record(
            event_type="mail_campaign.created",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{campaign.name}" was created.',
            entity_type="mail_campaign",
            entity_id=campaign.mail_campaign_id,
            entity_name=campaign.name,
        )
        return campaign

    async def get_campaign(self, mail_campaign_id: str) -> MailCampaign:
        return await self._require_campaign(mail_campaign_id)

    async def list_campaigns(self) -> list[MailCampaign]:
        return await self.campaign_store.list()

    async def update_campaign(self, mail_campaign_id: str, patch: dict[str, Any]) -> MailCampaign:
        """
        DRAFT-only (see MailCampaignNotEditableError). Unknown/disallowed
        patch keys (including `status` -- status only ever changes via
        mark_ready()/unlock_campaign()/archive_campaign()) are silently
        dropped, matching CrmService.update_contact_list()'s exact
        convention rather than erroring on them.

        Schedule fields get "soft" validation here -- individually
        checkable combinations (start_time/end_time ordering, a non-empty
        sending_days list's values, a non-empty timezone string) are
        rejected immediately, but a field being left unset/empty is fine;
        completeness is mark_ready()'s job, not this method's, since a
        DRAFT campaign is legitimately mid-configuration.

        Setting `source_list_id` to a value that doesn't resolve to a real
        CrmContactList is rejected immediately (CrmContactListNotFound) --
        no reason to let a campaign point at a dangling id even transiently.

        The four Campaign Manager Integration Phase additions get the same
        "soft" treatment: `sharing`/`all_hours`/`start_immediately` are
        booleans/enums with no incompleteness to check, and
        `daily_lead_start_limit` is rejected immediately if provided but
        not a positive integer (None -- "unlimited" -- is always fine).
        `all_hours=True` forces `start_time`/`end_time` to the literal
        full-day bounds regardless of whatever this same patch also
        requested for those two keys -- see MailCampaign's docstring for
        why this needs no change to validate_mail_schedule() or mark_ready().
        """
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)

        allowed = {k: v for k, v in patch.items() if k in _CAMPAIGN_PATCH_FIELDS}

        # `patch` is a plain, untyped dict (matching CrmService.update_contact's
        # exact convention) -- model_copy(update=...) below does NOT re-validate
        # or coerce types, so a JSON body's "09:00" string must be turned into a
        # real datetime.time here, explicitly, or every later time comparison
        # (this method's start<end check, mark_ready()'s strict validation) would
        # silently compare strings instead of times.
        for time_key in ("start_time", "end_time"):
            if time_key in allowed and isinstance(allowed[time_key], str):
                allowed[time_key] = _parse_time_of_day(allowed[time_key])

        # Same reasoning as the time-string coercion above: a raw PATCH body
        # carries "everyone"/"only_me" as a plain string, not yet the enum
        # model_copy(update=...) expects -- coerce explicitly rather than let
        # a bare string silently sit in a field typed as MailCampaignSharing.
        if "sharing" in allowed and isinstance(allowed["sharing"], str):
            try:
                allowed["sharing"] = MailCampaignSharing(allowed["sharing"])
            except ValueError as e:
                raise ValueError(f"'{allowed['sharing']}' is not a valid sharing value.") from e

        if "daily_lead_start_limit" in allowed and allowed["daily_lead_start_limit"] is not None:
            limit = allowed["daily_lead_start_limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("Number of leads to start daily must be a positive integer.")

        if "source_list_id" in allowed and allowed["source_list_id"] is not None:
            await self.crm_service.get_contact_list(allowed["source_list_id"])  # raises CrmContactListNotFound

        merged_all_hours = allowed.get("all_hours", campaign.all_hours)
        if merged_all_hours:
            allowed["start_time"] = _ALL_DAY_SENTINEL_START
            allowed["end_time"] = _ALL_DAY_SENTINEL_END

        merged_start = allowed.get("start_time", campaign.start_time)
        merged_end = allowed.get("end_time", campaign.end_time)
        if merged_start is not None and merged_end is not None and merged_start >= merged_end:
            raise MailScheduleValidationError("Start time must be before end time.")

        if "sending_days" in allowed and allowed["sending_days"]:
            if any(d < 0 or d > 6 for d in allowed["sending_days"]):
                raise MailScheduleValidationError("Sending days must be 0 (Monday) through 6 (Sunday).")

        if "timezone" in allowed and allowed["timezone"]:
            validate_mail_timezone(allowed["timezone"])

        updated = campaign.model_copy(update={**allowed, "updated_at": datetime.now(timezone.utc)})
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.updated",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was updated.',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
        )
        return updated

    # --- Sequence steps --------------------------------------------------

    async def list_steps(self, mail_campaign_id: str) -> list[MailSequenceStep]:
        await self._require_campaign(mail_campaign_id)
        return await self.step_store.list_for_campaign(mail_campaign_id)

    async def add_step(
        self, mail_campaign_id: str, subject: str, body: str, delay_days: int = 0, reply_in_thread: bool = True
    ) -> MailSequenceStep:
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)
        self._validate_variables(subject, body)

        existing = await self.step_store.list_for_campaign(mail_campaign_id)
        next_number = (max((s.step_number for s in existing), default=0)) + 1

        now = datetime.now(timezone.utc)
        step = MailSequenceStep(
            step_id=str(uuid.uuid4()),
            mail_campaign_id=mail_campaign_id,
            step_number=next_number,
            subject=subject,
            body=body,
            delay_days=delay_days,
            reply_in_thread=reply_in_thread,
            created_at=now,
            updated_at=now,
        )
        await self.step_store.create(step)
        return step

    async def update_step(self, mail_campaign_id: str, step_id: str, patch: dict[str, Any]) -> MailSequenceStep:
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)
        step = await self._require_step(mail_campaign_id, step_id)

        allowed = {k: v for k, v in patch.items() if k in ("subject", "body", "delay_days", "reply_in_thread")}
        new_subject = allowed.get("subject", step.subject)
        new_body = allowed.get("body", step.body)
        self._validate_variables(new_subject, new_body)

        updated = step.model_copy(update={**allowed, "updated_at": datetime.now(timezone.utc)})
        await self.step_store.save(updated)
        return updated

    async def delete_step(self, mail_campaign_id: str, step_id: str) -> list[MailSequenceStep]:
        """Deletes the step, then renumbers the remaining steps to stay
        contiguous 1..N (no gaps) -- uses the same two-phase offset-then-
        reassign renumbering as reorder_steps() to never transiently
        collide with the UNIQUE(mail_campaign_id, step_number) constraint."""
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)
        await self._require_step(mail_campaign_id, step_id)

        await self.step_store.delete(step_id)
        remaining = await self.step_store.list_for_campaign(mail_campaign_id)
        ordered_ids = [s.step_id for s in remaining]
        return await self._renumber(mail_campaign_id, ordered_ids)

    async def reorder_steps(self, mail_campaign_id: str, ordered_step_ids: list[str]) -> list[MailSequenceStep]:
        """`ordered_step_ids` must be exactly the campaign's current step
        ids, in the desired new order -- any mismatch (missing id, extra
        id, wrong campaign) is rejected rather than guessed at."""
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)

        existing = await self.step_store.list_for_campaign(mail_campaign_id)
        existing_ids = {s.step_id for s in existing}
        if set(ordered_step_ids) != existing_ids:
            raise ValueError("reorder_steps requires exactly this campaign's current step ids, no more, no fewer.")

        return await self._renumber(mail_campaign_id, ordered_step_ids)

    async def _renumber(self, mail_campaign_id: str, ordered_step_ids: list[str]) -> list[MailSequenceStep]:
        """Two-phase renumber -- first offsets every step far out of the way
        (by a constant larger than any realistic step count), then assigns
        1..N in the requested order. Neither phase can ever collide with
        the UNIQUE(mail_campaign_id, step_number) constraint, unlike a
        naive direct 1..N reassignment (which could transiently collide
        with a step that hasn't been renumbered yet)."""
        steps_by_id = {s.step_id: s for s in await self.step_store.list_for_campaign(mail_campaign_id)}
        offset = 100_000
        now = datetime.now(timezone.utc)

        for i, step_id in enumerate(ordered_step_ids):
            step = steps_by_id[step_id]
            await self.step_store.save(step.model_copy(update={"step_number": offset + i, "updated_at": now}))

        renumbered: list[MailSequenceStep] = []
        for i, step_id in enumerate(ordered_step_ids):
            step = steps_by_id[step_id]
            updated = step.model_copy(update={"step_number": i + 1, "updated_at": now})
            await self.step_store.save(updated)
            renumbered.append(updated)
        return renumbered

    async def _require_step(self, mail_campaign_id: str, step_id: str) -> MailSequenceStep:
        step = await self.step_store.get(step_id)
        if step is None or step.mail_campaign_id != mail_campaign_id:
            raise MailSequenceStepNotFound(step_id)
        return step

    @staticmethod
    def _validate_variables(subject: str, body: str) -> None:
        unknown = sorted({v for t in (subject, body) for v in find_unknown_mail_template_variables(t)})
        if unknown:
            raise InvalidMailTemplateVariableError(unknown)

    # --- State transitions -------------------------------------------------

    async def mark_ready(self, mail_campaign_id: str, suppressed_emails: set[str]) -> MailCampaign:
        """
        `suppressed_emails` is the CURRENT set of active-suppression
        normalized emails, supplied by the caller (the API layer, which
        also owns MailSuppressionService) -- this service never imports or
        depends on MailSuppressionService directly, so the two stay
        decoupled the same way MailCampaignService never depends on
        anything sending-related.

        The ONE explicit action that both validates a campaign is complete
        AND materializes its audience snapshot (MailEnrollment rows) --
        deliberately the same action, not two. Why here, and nowhere else:

          - Review (get_review(), below) is a live, always-current
            calculation against the campaign's list -- it must NEVER
            mutate anything, so it cannot be where the snapshot happens.
          - Snapshotting on every edit (add_step, update_campaign, ...)
            would mean re-materializing potentially hundreds of rows on
            every keystroke-adjacent save while still mid-configuration --
            wasteful, and it would blur "still drafting" with "audience is
            locked in," which is exactly the distinction READY exists to
            make crisp.
          - mark_ready() is already the one meaningful, explicit,
            human-triggered state transition this phase has -- reusing it
            avoids inventing a second, separate "snapshot audience" action
            with its own ambiguous relationship to campaign status.

        Validates (collecting every failure, not just the first):
          - source_list_id is set and resolves to a real CrmContactList
          - at least one sequence step exists
          - sending_days/start_time/end_time/timezone are ALL present and
            mutually valid (validate_mail_schedule)
        Raises MailCampaignNotReadyError listing every problem if any check
        fails -- no partial snapshot is ever created on a failed attempt.

        Snapshot: for every contact CURRENTLY in the list with a non-blank
        email, creates (idempotently -- see MailEnrollmentStore.create())
        a MailEnrollment row, PENDING unless that email is currently
        suppressed (then SUPPRESSED). Contacts with no usable email are not
        enrolled at all. Safe to call again on an already-READY campaign
        only via re-entering DRAFT first (mark_ready itself refuses a
        non-DRAFT campaign -- see MailCampaignInvalidTransitionError); the
        underlying create() being idempotent is defense in depth, not the
        primary safeguard.
        """
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.DRAFT:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "mark_ready")

        reasons: list[str] = []

        if not campaign.source_list_id:
            reasons.append("No audience (CRM List) has been selected.")
            contacts = []
        else:
            try:
                await self.crm_service.get_contact_list(campaign.source_list_id)
                contacts = (
                    await self.crm_service.get_list_contacts(campaign.source_list_id, page=1, page_size=_ALL_CONTACTS_PAGE_SIZE)
                ).items
            except CrmContactListNotFound:
                reasons.append("The selected CRM List no longer exists.")
                contacts = []

        steps = await self.step_store.list_for_campaign(mail_campaign_id)
        if not steps:
            reasons.append("At least one sequence step is required.")

        try:
            validate_mail_schedule(campaign.sending_days, campaign.start_time, campaign.end_time, campaign.timezone)
        except MailScheduleValidationError as e:
            reasons.append(str(e))

        if reasons:
            raise MailCampaignNotReadyError(mail_campaign_id, reasons)

        enrolled = already_enrolled = suppressed_at_enrollment = 0
        now = datetime.now(timezone.utc)
        for contact in contacts:
            normalized = normalize_email(contact.email)
            if not normalized:
                continue
            status = MailEnrollmentStatus.SUPPRESSED if normalized in suppressed_emails else MailEnrollmentStatus.PENDING
            enrollment = MailEnrollment(
                enrollment_id=str(uuid.uuid4()),
                mail_campaign_id=mail_campaign_id,
                crm_contact_id=contact.crm_contact_id,
                email_at_enrollment=contact.email,
                status=status,
                enrolled_at=now,
                created_at=now,
            )
            is_new = await self.enrollment_store.create(enrollment)
            if is_new:
                enrolled += 1
                if status == MailEnrollmentStatus.SUPPRESSED:
                    suppressed_at_enrollment += 1
            else:
                already_enrolled += 1

        updated = campaign.model_copy(update={"status": MailCampaignStatus.READY, "ready_at": now, "updated_at": now})
        await self.campaign_store.save(updated)

        await self.activity_log.record(
            event_type="mail_campaign.ready",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was marked ready ({enrolled} contact{"s" if enrolled != 1 else ""} enrolled).',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
            metadata={"enrolled": enrolled, "already_enrolled": already_enrolled, "suppressed_at_enrollment": suppressed_at_enrollment},
        )
        if enrolled > 0:
            await self.activity_log.record(
                event_type="mail_enrollment.enrolled",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary=f'{enrolled} contact{"s" if enrolled != 1 else ""} enrolled in "{updated.name}".',
                entity_type="mail_campaign",
                entity_id=updated.mail_campaign_id,
                entity_name=updated.name,
                metadata={"enrolled": enrolled, "suppressed_at_enrollment": suppressed_at_enrollment},
            )
        return updated

    async def unlock_campaign(self, mail_campaign_id: str) -> MailCampaign:
        """READY -> DRAFT. Deletes every MailEnrollment row for this
        campaign FIRST (the snapshot is only ever valid while READY; once
        unlocked, the audience/sequence/schedule can change, making the old
        snapshot meaningless) -- a subsequent mark_ready() re-snapshots
        fresh against whatever the list looks like at that point."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.READY:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "unlock")

        await self.enrollment_store.delete_for_campaign(mail_campaign_id)
        updated = campaign.model_copy(
            update={"status": MailCampaignStatus.DRAFT, "ready_at": None, "updated_at": datetime.now(timezone.utc)}
        )
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.updated",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was unlocked back to draft for editing.',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
        )
        return updated

    async def archive_campaign(self, mail_campaign_id: str) -> MailCampaign:
        """DRAFT or READY -> ARCHIVED (terminal in this phase -- no
        un-archive). Enrollment rows, if any, are left as-is -- harmless
        historical record, never read by anything once archived."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status == MailCampaignStatus.ARCHIVED:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "archive")

        now = datetime.now(timezone.utc)
        updated = campaign.model_copy(update={"status": MailCampaignStatus.ARCHIVED, "archived_at": now, "updated_at": now})
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.archived",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was archived.',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
        )
        return updated

    async def list_enrollments(self, mail_campaign_id: str) -> list[MailEnrollment]:
        await self._require_campaign(mail_campaign_id)
        return await self.enrollment_store.list_for_campaign(mail_campaign_id)

    # --- Review (pure, read-only) ----------------------------------------

    async def get_review(self, mail_campaign_id: str, suppressed_emails: set[str]) -> MailCampaignReview:
        """
        Pure calculation -- reads the campaign, its CURRENT list membership
        (live, not a snapshot), its CURRENT sequence step count, and the
        CURRENT suppressed-email set passed in by the caller (see
        MailSuppressionService.list_active_suppressed_emails() at the API
        layer) -- and returns numbers. No store's create/save/delete is
        ever called here, regardless of campaign status. Callable even for
        a campaign with no source_list_id yet (reports zeros) or whose list
        was since deleted (reports source_list_exists=False).
        """
        campaign = await self._require_campaign(mail_campaign_id)
        steps = await self.step_store.list_for_campaign(mail_campaign_id)

        total_contacts = missing_email = suppressed = 0
        source_list_name: str | None = None
        source_list_exists = False

        if campaign.source_list_id:
            try:
                summary = await self.crm_service.get_contact_list(campaign.source_list_id)
                source_list_name = summary.name
                source_list_exists = True
                contacts = (
                    await self.crm_service.get_list_contacts(
                        campaign.source_list_id, page=1, page_size=_ALL_CONTACTS_PAGE_SIZE
                    )
                ).items
                total_contacts = len(contacts)
                for contact in contacts:
                    normalized = normalize_email(contact.email)
                    if not normalized:
                        missing_email += 1
                    elif normalized in suppressed_emails:
                        suppressed += 1
            except CrmContactListNotFound:
                source_list_exists = False

        eligible = total_contacts - missing_email - suppressed
        step_count = len(steps)

        return MailCampaignReview(
            mail_campaign_id=mail_campaign_id,
            source_list_id=campaign.source_list_id,
            source_list_name=source_list_name,
            source_list_exists=source_list_exists,
            total_contacts=total_contacts,
            contacts_missing_email=missing_email,
            contacts_suppressed=suppressed,
            contacts_eligible=eligible,
            sequence_step_count=step_count,
            theoretical_total_sends=eligible * step_count,
            daily_capacity_estimate=None,
            daily_capacity_note=(
                "Add mailbox daily-send limits (Phase 2) to estimate real daily capacity -- "
                "no mailbox is configured yet."
            ),
        )
