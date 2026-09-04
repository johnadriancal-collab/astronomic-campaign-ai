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
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol

from app.config import settings
from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import (
    ALLOWED_MAIL_TEMPLATE_VARIABLES,
    LegacyLeadStartLimitCampaign,
    MailCampaign,
    MailCampaignReview,
    MailCampaignSchedule,
    MailCampaignSharing,
    MailCampaignStatus,
    MailCampaignWorkload,
    MailEnrollment,
    MailEnrollmentBatch,
    MailEnrollmentBatchMember,
    MailEnrollmentBatchMemberState,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailEnrollmentStatus,
    MailScheduleSource,
    MailScheduleValidationError,
    MailSendWindow,
    MailSequenceStep,
    find_unknown_mail_template_variables,
    validate_mail_timezone,
    validate_send_windows,
)
from app.models.mailbox import Mailbox, MailboxStatus
from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignNotFoundError, MailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import (
    DuplicateBatchIdempotencyKeyError,
    MailEnrollmentBatchNotFoundError,
    MailEnrollmentBatchStore,
)
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_send_window_store import MailSendWindowStore
from app.repositories.mail_sequence_step_store import (
    DuplicateMailSequenceStepNumberError,
    MailSequenceStepNotFoundError,
    MailSequenceStepStore,
)
from app.repositories.mail_suppression_store import MailSuppressionStore
from app.repositories.mailbox_store import MailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmContactListNotFound, CrmService
from app.services.mail_sending_service import MailSendingService

# Practically "everything in the list" -- CrmService.get_list_contacts()
# paginates in-memory with no hard cap, so a large page_size in one call is
# the simplest way to read a full list's membership without inventing a
# second "get all" method on CrmService (which this file must not modify).
_ALL_CONTACTS_PAGE_SIZE = 100_000

# Shared by add_prospects()'s own authoritative eligibility check and
# MailCampaignCsvProspectService's read-only preflight check (Stage 4B,
# 2026-09-03) -- defined exactly once so the two checks can never drift
# apart. See add_prospects()'s own docstring for what each of these three
# statuses means for this operation, and MailCampaignNotEligibleForProspectsError
# for why DRAFT/READY/ARCHIVED are refused.
PROSPECT_ELIGIBLE_CAMPAIGN_STATUSES = frozenset(
    {MailCampaignStatus.ACTIVE, MailCampaignStatus.PAUSED, MailCampaignStatus.COMPLETED}
)


class CrmImportResolutionReader(Protocol):
    """The ONLY CRM-import capability MailCampaignService is ever allowed
    to depend on (Stage 4B, 2026-09-03) -- read-only, and structurally
    incapable of triggering a CRM mutation: this Protocol's surface has no
    commit()/preview()/upload() at all, only the one read this file
    actually needs. CrmImportService satisfies this structurally, with
    ZERO changes to that file -- Python's Protocol typing (PEP 544) needs
    no explicit declaration or inheritance, only a matching method
    signature. This keeps this file's existing, load-bearing invariant
    ("CrmService is a read-only dependency here... this file never calls
    anything that mutates a CrmContact or a CrmContactList/membership")
    true for CRM import too, enforced by the type system rather than by a
    docstring promise plus a structural test alone. The component that
    legitimately needs the FULL, writable CrmImportService is
    MailCampaignCsvProspectService (app/services/
    mail_campaign_csv_prospect_service.py) -- the one place in this
    codebase allowed to trigger a CSV-driven CRM mutation on a campaign's
    behalf."""

    async def list_resolved_contact_ids(self, import_batch_id: str) -> list[str]: ...


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


class MailCampaignNotEligibleForProspectsError(Exception):
    """Raised by add_prospects() for DRAFT, READY, or ARCHIVED (the only
    three statuses this operation refuses -- see that method's own
    docstring for why ACTIVE/PAUSED/COMPLETED are each fine, COMPLETED
    conditionally so). Deliberately its own exception rather than reusing
    MailCampaignInvalidTransitionError -- add_prospects() isn't itself a
    status TRANSITION (it may not change status at all, e.g. against an
    already-ACTIVE campaign), so "cannot transition from X" would be a
    misleading message for what's actually "not an eligible status for
    this OPERATION."""

    def __init__(self, mail_campaign_id: str, status: MailCampaignStatus):
        self.mail_campaign_id = mail_campaign_id
        self.status = status
        super().__init__(
            f"MailCampaign {mail_campaign_id} is {status.value} -- prospects can only be added to an "
            "ACTIVE, PAUSED, or (legacy) COMPLETED campaign."
        )


class MailCampaignNotReadyError(Exception):
    """Raised by mark_ready() when validation fails -- `reasons` lists every
    problem found (not just the first), so the UI can show a complete
    checklist rather than forcing the user to fix issues one at a time."""

    def __init__(self, mail_campaign_id: str, reasons: list[str]):
        self.mail_campaign_id = mail_campaign_id
        self.reasons = reasons
        super().__init__(f"MailCampaign {mail_campaign_id} is not ready: {'; '.join(reasons)}")


class MailSendingEngineDisabledError(Exception):
    """Raised by activate_campaign() when settings.mail_sending_engine_enabled
    is False (the default) -- see that setting's own docstring in
    app/config.py. Distinct from MailCampaignNotReadyError (a real
    readiness problem with THIS campaign) and from
    MailCampaignInvalidTransitionError (wrong status) -- this means the
    engine itself is not turned on for this deployment at all, regardless
    of how ready or valid any campaign is. Mirrors AuthNotConfiguredError's
    exact "service-layer raises, API layer maps to 503" convention (see
    app/services/auth_service.py)."""

    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(
            "The Astronomic Mail sending engine is not enabled on this deployment "
            "(mail_sending_engine_enabled=False) -- activation is refused regardless "
            "of this campaign's own readiness."
        )


class InvalidMailTemplateVariableError(ValueError):
    def __init__(self, unknown_variables: list[str]):
        self.unknown_variables = unknown_variables
        allowed = ", ".join(sorted(ALLOWED_MAIL_TEMPLATE_VARIABLES))
        super().__init__(
            f"Unknown variable(s) {unknown_variables} -- only {{{{...}}}} placeholders from [{allowed}] are allowed."
        )


class InvalidMailSequenceStepDelayError(ValueError):
    """Raised by add_step()/update_step() for a negative delay_days on a
    Step 2+ (a follow-up). Never raised for the step at position 1 -- that
    step's delay_days is unconditionally forced to 0 instead (see
    add_step()/update_step()/_renumber()'s docstrings for the full
    invariant), so a negative value there is silently overridden, not
    rejected."""

    def __init__(self, delay_days: int):
        self.delay_days = delay_days
        super().__init__(f"delay_days must be 0 or greater for a follow-up step (got {delay_days}).")


class MailboxChannelNotFoundError(Exception):
    """Raised by set_channel_mailboxes() when a requested mailbox_id doesn't
    resolve to any real Mailbox at all (never for a merely disconnected one --
    see MailboxChannelNotUsableError for that case)."""

    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox not found: {mailbox_id}")


class MailCampaignChannelsFrozenError(Exception):
    """Raised by set_channel_mailboxes() for an ACTIVE, PAUSED, COMPLETED, or
    ARCHIVED campaign. Channels remain editable at DRAFT and READY (see that
    method's docstring) -- but once a campaign has ever gone ACTIVE, its
    durable execution rows (MailEnrollmentStep.mailbox_id, MailEnrollment.
    assigned_mailbox_id) already reference specific, sticky mailbox
    assignments made from THIS selection; changing the selection out from
    under live/completed execution history would either silently invalidate
    those assignments or require a reassignment policy this phase
    deliberately doesn't build (see MailEnrollment.assigned_mailbox_id's
    "never silently reassigned" docstring). PAUSED and COMPLETED are frozen
    for the same reason as ACTIVE (pausing execution is not the same as
    unlocking configuration -- see MailCampaignStatus's docstring); ARCHIVED
    is additionally terminal (no un-archive transition exists). GET (
    list_channel_mailboxes) is never affected by this -- it always remains
    available so the UI can keep displaying any campaign's current or
    historical selection."""

    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(f"MailCampaign {mail_campaign_id} is not in a status where Channels can be changed.")


class MailboxChannelNotUsableError(Exception):
    """Raised by set_channel_mailboxes() when the request tries to NEWLY
    select a mailbox that isn't currently MailboxStatus.CONNECTED. Never
    raised for a mailbox that was already part of this campaign's selection
    before the call -- an already-selected mailbox may remain selected
    (unchanged) even if it has since become disconnected/needs_reauth; see
    that method's docstring for the full reasoning."""

    def __init__(self, mailbox_id: str, status: MailboxStatus):
        self.mailbox_id = mailbox_id
        self.status = status
        super().__init__(
            f"Mailbox {mailbox_id} cannot be newly selected while its status is '{status.value}' -- "
            "only a currently connected mailbox may be newly added to a campaign's Channels."
        )


class MailCampaignLegacyScheduleLockedError(Exception):
    """Raised by update_campaign() when a PATCH tries to touch a legacy
    schedule field (see _LEGACY_SCHEDULE_PATCH_FIELDS) on a campaign that
    already has explicit MailSendWindow rows. Once in that "window mode",
    those legacy fields are permanently ignored for actual scheduling
    purposes (see MailSendWindow's docstring in app/models/mail.py) -- so
    silently accepting a PATCH that reads as "changing the schedule" while
    doing nothing to the campaign's real, authoritative schedule would be
    actively misleading. This rejects the WHOLE patch (nothing partially
    applied) whenever any legacy schedule key is present, even if other,
    unrelated keys (name, daily_lead_start_limit, ...) were also included --
    callers should split such a request into two calls."""

    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(
            f"MailCampaign {mail_campaign_id} already has an explicit schedule -- legacy schedule fields "
            "(sending_days/start_time/end_time/all_hours/timezone) can no longer be changed via PATCH. "
            "Use PUT /mail/campaigns/{id}/schedule instead."
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

# Once a campaign has ANY explicit MailSendWindow row, these fields are
# frozen/inert (see MailSendWindow's docstring) -- update_campaign() refuses
# to accept a PATCH touching any of them rather than silently no-op'ing.
# `timezone` is included deliberately (not just the four schedule-shape
# fields) so the entire schedule configuration has exactly one authoritative
# write path once migrated -- see PUT .../schedule.
_LEGACY_SCHEDULE_PATCH_FIELDS = {"sending_days", "start_time", "end_time", "all_hours", "timezone"}

_ALL_DAY_SENTINEL_START = time(0, 0)
_ALL_DAY_SENTINEL_END = time(23, 59)

# The step at position 1 always has delay_days = 0 (it has no previous step
# to follow) -- enforced here, in the service, on every add/edit/reorder/
# delete, never only as a frontend display rule (see add_step()/
# update_step()/_renumber()). This is the value a step demoted FROM
# position 1 (via reorder) is reset to, as a real follow-up step -- named
# once so neither this file nor the frontend accumulates an unexplained
# literal 2 (see frontend/lib/mail.ts's DEFAULT_FOLLOWUP_DELAY_DAYS, the
# same constant mirrored for the Add-step form's own default).
DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS = 2


def _parse_time_of_day(value: str) -> time:
    """Accepts 'HH:MM' or 'HH:MM:SS' (time.fromisoformat handles both on
    Python 3.11+) -- raises MailScheduleValidationError (not a bare
    ValueError) on anything else, so the API layer's existing
    MailScheduleValidationError -> 400 mapping covers this too."""
    try:
        return time.fromisoformat(value)
    except ValueError as e:
        raise MailScheduleValidationError(f"'{value}' is not a valid time of day (expected HH:MM).") from e


def _synthesize_legacy_windows(campaign: MailCampaign) -> list[MailSendWindow]:
    """Turns a legacy campaign's `sending_days` + `start_time`/`end_time`
    (already forced to the 00:00/23:59 all_hours sentinel by
    update_campaign() when `all_hours=True` -- nothing special-cased here)
    into the equivalent MailSendWindow-shaped objects, purely computed and
    NEVER persisted -- see MailCampaignService._resolve_schedule()'s
    docstring for the one place this is called from. `window_id` is a
    clearly-synthetic, stable-within-one-resolution string (never a real
    stored id -- these rows can't be individually targeted for edit/delete,
    only ever read as a whole via GET .../schedule or readiness checks).

    Empty (`[]`) whenever the legacy fields don't actually describe a usable
    schedule (no days selected, or no start/end time set) -- callers treat
    that the same as "no schedule configured at all"."""
    if not campaign.sending_days or campaign.start_time is None or campaign.end_time is None:
        return []
    return [
        MailSendWindow(
            window_id=f"legacy-{campaign.mail_campaign_id}-{day}",
            mail_campaign_id=campaign.mail_campaign_id,
            day_of_week=day,
            start_time=campaign.start_time,
            end_time=campaign.end_time,
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
        )
        for day in sorted(set(campaign.sending_days))
    ]


def _compute_readiness_warnings(
    campaign: MailCampaign,
    source_list_exists: bool,
    step_count: int,
    has_connected_mailbox: bool,
    resolved_windows: list[MailSendWindow],
) -> list[str]:
    """The single source of truth for 'why can't this campaign be marked
    ready' -- called identically by mark_ready() (which raises
    MailCampaignNotReadyError if this is non-empty) and get_review() (which
    surfaces the same list as MailCampaignReview.readiness_warnings, so the
    UI can show real problems proactively before the user even attempts
    Mark Ready). Never duplicated -- both callers pass in whatever they've
    already computed about the campaign's list/step/channel/schedule state,
    so this never re-fetches anything itself.

    `has_connected_mailbox` must be True only if at least one of the
    campaign's SELECTED mailboxes is CURRENTLY MailboxStatus.CONNECTED --
    a selection that exists only historically (every selected mailbox now
    disconnected/needs_reauth, or none selected at all) is not enough, since
    there would be no real sender to eventually deliver from.

    `resolved_windows` is whatever MailCampaignService._resolve_schedule()
    already returned for this campaign (real MailSendWindow rows if any
    exist, else synthesized from legacy fields) -- this function never
    cares which; a legacy campaign with a valid old-style schedule reads as
    fully ready, identically to an equivalent explicit-windows campaign."""
    reasons: list[str] = []

    if not campaign.source_list_id:
        reasons.append("No audience (CRM List) has been selected.")
    elif not source_list_exists:
        reasons.append("The selected CRM List no longer exists.")

    if step_count == 0:
        reasons.append("At least one sequence step is required.")

    if not has_connected_mailbox:
        reasons.append("At least one connected sending inbox must be selected.")

    if not campaign.timezone:
        reasons.append("A timezone is required.")
    else:
        try:
            validate_mail_timezone(campaign.timezone)
        except MailScheduleValidationError as e:
            reasons.append(str(e))

    if not resolved_windows:
        reasons.append("At least one send window is required.")
    else:
        try:
            validate_send_windows([(w.day_of_week, w.start_time, w.end_time) for w in resolved_windows])
        except MailScheduleValidationError as e:
            reasons.append(str(e))

    return reasons


class MailCampaignService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        step_store: MailSequenceStepStore,
        enrollment_store: MailEnrollmentStore,
        crm_service: CrmService,
        activity_log: ActivityLogService,
        mailbox_store: MailboxStore,
        channel_store: MailCampaignMailboxStore,
        window_store: MailSendWindowStore,
        enrollment_step_store: MailEnrollmentStepStore,
        sending_service: MailSendingService,
        batch_store: MailEnrollmentBatchStore,
        batch_member_store: MailEnrollmentBatchMemberStore,
        suppression_store: MailSuppressionStore,
        crm_import_reader: CrmImportResolutionReader,
    ):
        self.campaign_store = campaign_store
        self.step_store = step_store
        self.enrollment_store = enrollment_store
        self.window_store = window_store
        self.crm_service = crm_service
        self.activity_log = activity_log
        self.mailbox_store = mailbox_store
        self.channel_store = channel_store
        self.enrollment_step_store = enrollment_step_store
        self.sending_service = sending_service
        self.batch_store = batch_store
        self.batch_member_store = batch_member_store
        # Stage 3 (2026-09-03): a deliberate, narrow exception to this
        # file's own "never depend on MailSuppressionService directly"
        # precedent (see mark_ready()'s docstring, which instead takes a
        # caller-supplied suppressed_emails snapshot) -- reconciliation
        # (_reconcile_batch()) must be fully self-contained, since it can
        # run from contexts with no live request/API layer to supply a
        # fresh snapshot from (app startup, a periodic sweep). Uses the
        # raw store directly, never MailSuppressionService, mirroring
        # exactly how MailSendingService's own early/final suppression
        # checks already do this (self.suppression_store.get(...)).
        self.suppression_store = suppression_store
        # Stage 4B (2026-09-03): read-only CRM-import resolution, typed as
        # the narrow CrmImportResolutionReader Protocol (not the full,
        # writable CrmImportService) -- see that Protocol's own docstring.
        # Used only by add_prospects()'s CSV_UPLOAD branch below.
        self.crm_import_reader = crm_import_reader

    async def _require_campaign(self, mail_campaign_id: str) -> MailCampaign:
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)
        return campaign

    def _require_draft(self, campaign: MailCampaign) -> None:
        if campaign.status != MailCampaignStatus.DRAFT:
            raise MailCampaignNotEditableError(campaign.mail_campaign_id, campaign.status)

    # --- Campaign CRUD -------------------------------------------------

    async def create_campaign(self, name: str, actor: str | None = None) -> MailCampaign:
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
            actor=actor,
        )
        return campaign

    async def get_campaign(self, mail_campaign_id: str) -> MailCampaign:
        return await self._require_campaign(mail_campaign_id)

    async def list_campaigns(self) -> list[MailCampaign]:
        return await self.campaign_store.list()

    async def list_campaigns_with_legacy_lead_start_limit(self) -> list[LegacyLeadStartLimitCampaign]:
        """Read-only Stage 5B compatibility report (2026-09-04): every
        campaign with a configured (non-null) daily_lead_start_limit,
        regardless of lead_start_mode -- for manual review of real usage
        ahead of the eventual 5F/5G removal of that field/its Settings-tab
        control (see MailCampaign.daily_lead_start_limit's own docstring).

        Deliberately NOT a new API route/admin endpoint -- this reuses the
        existing list_campaigns() read this service already exposes and
        simply projects/filters it, matching the 'a local repository/
        service query... may be enough' guidance rather than widening any
        access surface for a one-time review need."""
        campaigns = await self.list_campaigns()
        return [
            LegacyLeadStartLimitCampaign(
                mail_campaign_id=c.mail_campaign_id,
                name=c.name,
                status=c.status,
                lead_start_mode=c.lead_start_mode,
                daily_lead_start_limit=c.daily_lead_start_limit,
            )
            for c in campaigns
            if c.daily_lead_start_limit is not None
        ]

    async def update_campaign(self, mail_campaign_id: str, patch: dict[str, Any], actor: str | None = None) -> MailCampaign:
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
        why this needs no change to mark_ready()'s readiness check.

        Rejects the ENTIRE patch (MailCampaignLegacyScheduleLockedError,
        nothing applied) if it touches any legacy schedule field
        (_LEGACY_SCHEDULE_PATCH_FIELDS) on a campaign that already has
        explicit MailSendWindow rows -- see that exception's docstring.
        A brand-new campaign can never hit this (it cannot have window rows
        before it exists), so campaign creation's own legacy-shaped payload
        is never affected.
        """
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)

        allowed = {k: v for k, v in patch.items() if k in _CAMPAIGN_PATCH_FIELDS}

        if _LEGACY_SCHEDULE_PATCH_FIELDS & allowed.keys():
            if await self.window_store.list_for_campaign(mail_campaign_id):
                raise MailCampaignLegacyScheduleLockedError(mail_campaign_id)

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
            actor=actor,
        )
        return updated

    # --- Sequence steps --------------------------------------------------

    async def list_steps(self, mail_campaign_id: str) -> list[MailSequenceStep]:
        await self._require_campaign(mail_campaign_id)
        return await self.step_store.list_for_campaign(mail_campaign_id)

    async def add_step(
        self, mail_campaign_id: str, subject: str, body: str, delay_days: int = 0, reply_in_thread: bool = True
    ) -> MailSequenceStep:
        """
        Sequence timing invariant: the step landing at position 1 (an
        empty sequence's first add) always gets delay_days=0, regardless of
        what the caller passed -- overridden here, not merely rejected,
        since a first step genuinely has no "previous step" for a delay to
        be relative to. A Step 2+ keeps its caller-supplied delay_days, but
        a negative one is rejected (InvalidMailSequenceStepDelayError) --
        the frontend's numeric input is not the only place this is
        enforced.
        """
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)
        self._validate_variables(subject, body)

        existing = await self.step_store.list_for_campaign(mail_campaign_id)
        next_number = (max((s.step_number for s in existing), default=0)) + 1

        if next_number == 1:
            delay_days = 0
        elif delay_days < 0:
            raise InvalidMailSequenceStepDelayError(delay_days)

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
        """
        Same delay_days invariant as add_step() (see its docstring), applied
        on edit: a step currently at position 1 always ends up with
        delay_days=0 -- forced here unconditionally, even if the patch
        doesn't mention delay_days at all, so editing a legacy Step 1's
        subject/body alone (from before this invariant existed) also
        lazily self-heals its stale nonzero delay_days as a side effect of
        that same save. A Step 2+ patch touching delay_days is rejected
        (InvalidMailSequenceStepDelayError) if negative; left alone
        entirely if the patch doesn't touch it.
        """
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)
        step = await self._require_step(mail_campaign_id, step_id)

        allowed = {k: v for k, v in patch.items() if k in ("subject", "body", "delay_days", "reply_in_thread")}
        new_subject = allowed.get("subject", step.subject)
        new_body = allowed.get("body", step.body)
        self._validate_variables(new_subject, new_body)

        if step.step_number == 1:
            allowed["delay_days"] = 0
        elif "delay_days" in allowed and allowed["delay_days"] < 0:
            raise InvalidMailSequenceStepDelayError(allowed["delay_days"])

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
        with a step that hasn't been renumbered yet).

        Shared by both reorder_steps() and delete_step()'s post-delete
        renumber -- so the delay_days invariant only needs enforcing here,
        once, to cover "move another step above Step 1", "move Step 1
        down", and "delete Step 1 so old Step 2 becomes Step 1" all alike:
        whichever step lands at the new step_number 1 gets delay_days
        forced to 0 (it has no previous step anymore, regardless of what it
        held before); a step that WAS at step_number 1 but is being moved
        away from it gets reset to DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS
        rather than keeping the enforced 0 it only had because it used to
        be first. Every other step's delay_days is left completely alone --
        a real, user-configured follow-up delay must survive a reorder that
        doesn't touch its position relative to "am I first."""
        steps_by_id = {s.step_id: s for s in await self.step_store.list_for_campaign(mail_campaign_id)}
        offset = 100_000
        now = datetime.now(timezone.utc)

        for i, step_id in enumerate(ordered_step_ids):
            step = steps_by_id[step_id]
            await self.step_store.save(step.model_copy(update={"step_number": offset + i, "updated_at": now}))

        renumbered: list[MailSequenceStep] = []
        for i, step_id in enumerate(ordered_step_ids):
            step = steps_by_id[step_id]
            new_number = i + 1
            update: dict[str, Any] = {"step_number": new_number, "updated_at": now}
            if new_number == 1:
                update["delay_days"] = 0
            elif step.step_number == 1:
                update["delay_days"] = DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS
            updated = step.model_copy(update=update)
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

    # --- Channels (selected sending mailboxes) ----------------------------

    async def list_channel_mailboxes(self, mail_campaign_id: str) -> list[Mailbox]:
        """Every mailbox currently selected for this campaign, resolved to
        full Mailbox objects. A linked mailbox_id can never fail to resolve
        (disconnecting a mailbox only flips its status -- see
        MailboxService.disconnect_mailbox() -- it never deletes the row), so
        this never silently drops a row; callers render whatever status
        comes back (including disconnected/needs_reauth) rather than hiding it."""
        await self._require_campaign(mail_campaign_id)
        mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id)
        mailboxes = [await self.mailbox_store.get(mailbox_id) for mailbox_id in mailbox_ids]
        return [m for m in mailboxes if m is not None]

    async def set_channel_mailboxes(self, mail_campaign_id: str, mailbox_ids: list[str]) -> list[Mailbox]:
        """Replaces this campaign's ENTIRE selected mailbox set atomically
        (see MailCampaignMailboxStore.replace_for_campaign()) -- not an
        incremental add/remove, matching the Channels tab's single "Save"
        action.

        Every requested id must resolve to a real Mailbox
        (MailboxChannelNotFoundError otherwise). A request may freely KEEP a
        mailbox that was already selected before this call regardless of its
        current status -- disconnected/needs_reauth mailboxes are never
        silently dropped from a campaign's history just because their
        status changed (see this phase's investigation report). But a
        mailbox that was NOT already selected may only be NEWLY added while
        it is MailboxStatus.CONNECTED (MailboxChannelNotUsableError
        otherwise) -- there's no reason to let someone knowingly assign a
        campaign to a sender that can't currently send.

        Callable on DRAFT and READY only (MailCampaignChannelsFrozenError on
        ACTIVE/PAUSED/COMPLETED/ARCHIVED -- see that exception's docstring
        for why execution locks this beyond just the terminal ARCHIVED
        case). Unlike audience/sequence/schedule, mailbox assignment isn't
        part of the mark_ready() snapshot, so there's nothing at READY for
        a lock to protect; this is deliberate, so a READY campaign whose
        only usable mailbox becomes disconnected can be fixed by selecting
        a new one without an unlock/re-snapshot round trip."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status in (
            MailCampaignStatus.ACTIVE,
            MailCampaignStatus.PAUSED,
            MailCampaignStatus.COMPLETED,
            MailCampaignStatus.ARCHIVED,
        ):
            raise MailCampaignChannelsFrozenError(mail_campaign_id)

        deduped = list(dict.fromkeys(mailbox_ids))
        currently_selected = set(await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id))

        resolved: list[Mailbox] = []
        for mailbox_id in deduped:
            mailbox = await self.mailbox_store.get(mailbox_id)
            if mailbox is None:
                raise MailboxChannelNotFoundError(mailbox_id)
            if mailbox_id not in currently_selected and mailbox.status != MailboxStatus.CONNECTED:
                raise MailboxChannelNotUsableError(mailbox_id, mailbox.status)
            resolved.append(mailbox)

        await self.channel_store.replace_for_campaign(mail_campaign_id, deduped)
        return resolved

    async def _has_connected_selected_mailbox(self, mail_campaign_id: str) -> bool:
        """True only if at least one of this campaign's SELECTED mailboxes
        is CURRENTLY MailboxStatus.CONNECTED -- see
        _compute_readiness_warnings()'s docstring for why a merely
        historical selection isn't enough."""
        mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id)
        for mailbox_id in mailbox_ids:
            mailbox = await self.mailbox_store.get(mailbox_id)
            if mailbox is not None and mailbox.status == MailboxStatus.CONNECTED:
                return True
        return False

    # --- Schedule (real MailSendWindow rows, legacy-compatible) -----------

    async def _resolve_schedule(
        self, mail_campaign_id: str, campaign: MailCampaign
    ) -> tuple[list[MailSendWindow], MailScheduleSource]:
        """THE one place a campaign's real schedule is ever read from --
        every other schedule-reading code path (get_schedule(),
        _compute_readiness_warnings()'s callers) goes through this, so
        nothing in this service can ever read a different, disagreeing
        representation than anything else.

        Real MailSendWindow rows are authoritative the instant even one
        exists for this campaign -- at that point the legacy
        sending_days/start_time/end_time/all_hours fields on the campaign
        itself are permanently ignored for scheduling purposes (they are
        NOT deleted or synced -- see MailSendWindow's module-level
        docstring in app/models/mail.py). Only when NO window row exists at
        all does this fall back to synthesizing the equivalent windows from
        those legacy fields, purely computed, never persisted by the mere
        act of reading them."""
        windows = await self.window_store.list_for_campaign(mail_campaign_id)
        if windows:
            return windows, "windows"
        synthesized = _synthesize_legacy_windows(campaign)
        if synthesized:
            return synthesized, "legacy"
        return [], "none"

    async def get_schedule(self, mail_campaign_id: str) -> MailCampaignSchedule:
        """Read-only, callable at ANY campaign status (draft/ready/archived)
        -- the Schedule tab must be able to keep displaying a READY or
        ARCHIVED campaign's schedule, just without letting it be edited
        (see set_schedule() for the DRAFT-only write gate)."""
        campaign = await self._require_campaign(mail_campaign_id)
        windows, source = await self._resolve_schedule(mail_campaign_id, campaign)
        return MailCampaignSchedule(
            mail_campaign_id=mail_campaign_id, timezone=campaign.timezone, source=source, windows=windows
        )

    async def set_schedule(
        self,
        mail_campaign_id: str,
        timezone_name: str,
        windows: list[tuple[str | None, int, str, str]],
        actor: str | None = None,
    ) -> MailCampaignSchedule:
        """Atomically replaces the campaign's ENTIRE schedule (timezone +
        every send window) -- not an incremental add/remove, matching the
        Schedule tab's single "Save" action. `windows` is
        (window_id | None, day_of_week, "HH:MM" start, "HH:MM" end) tuples,
        parsed and structurally validated (day range, start<end, no
        same-day overlap; see validate_send_windows()) BEFORE any write
        happens, so a rejected save leaves the previous schedule completely
        untouched.

        Window identity is STABLE across an ordinary edit, not
        regenerated on every save: passing an existing window's real
        `window_id` back (with a changed day/start/end, or unchanged)
        keeps that same row's identity -- only entries with `window_id is
        None` mint a fresh id, and any existing window whose id is simply
        NOT present in this call's `windows` is removed (this is still a
        full replace, just one that preserves identity for whatever
        carries over). Rejects the request entirely, before any write, if:
          - a `window_id` doesn't belong to this campaign at all (could be
            another campaign's id, or simply made up) -- prevents ever
            "adopting" or overwriting a window from elsewhere
          - the same non-null `window_id` appears twice in one request
        Both raise MailScheduleValidationError, same as every other
        structural problem here -- there's no meaningful difference
        between "this row doesn't exist" and "this row's shape is
        invalid" from the caller's perspective, both are a bad request.

        DRAFT-only -- unlike Channels, a Ready campaign's schedule stays
        locked behind the existing Unlock boundary (see
        MailCampaignNotEditableError): schedule changes affect actual
        campaign execution semantics once a sending engine exists, whereas
        replacing a disconnected Channel sender does not. ARCHIVED is
        rejected by the same _require_draft() call -- there is no separate
        "frozen" exception type here (unlike Channels' dedicated
        MailCampaignChannelsFrozenError) since DRAFT-only already covers
        both READY and ARCHIVED identically, matching how every other
        DRAFT-only mutation (update_campaign, add_step, ...) already
        behaves.

        This is the ONE place a campaign's schedule transitions from
        "legacy" to "windows" -- the instant this succeeds, the campaign
        has real MailSendWindow rows and _resolve_schedule() will return
        them (source="windows") for every future read, permanently, even
        though the old sending_days/start_time/end_time/all_hours fields
        are left exactly as they were (never zeroed out, never synced) --
        see MailSendWindow's module docstring in app/models/mail.py. From
        that point on, update_campaign() refuses any patch touching those
        legacy fields (MailCampaignLegacyScheduleLockedError) -- this
        method is the campaign's one remaining authoritative schedule
        write path."""
        campaign = await self._require_campaign(mail_campaign_id)
        self._require_draft(campaign)

        validate_mail_timezone(timezone_name)

        existing_by_id = {w.window_id: w for w in await self.window_store.list_for_campaign(mail_campaign_id)}

        parsed: list[tuple[str | None, int, time, time]] = []
        seen_ids: set[str] = set()
        for window_id, day, start_str, end_str in windows:
            if window_id is not None:
                if window_id not in existing_by_id:
                    raise MailScheduleValidationError(
                        f"'{window_id}' is not an existing send window on this campaign."
                    )
                if window_id in seen_ids:
                    raise MailScheduleValidationError(f"Duplicate window_id '{window_id}' in request.")
                seen_ids.add(window_id)
            parsed.append((window_id, day, _parse_time_of_day(start_str), _parse_time_of_day(end_str)))

        validate_send_windows([(day, start, end) for _window_id, day, start, end in parsed])

        now = datetime.now(timezone.utc)
        new_windows: list[MailSendWindow] = []
        for window_id, day, start, end in parsed:
            if window_id is not None:
                existing = existing_by_id[window_id]
                new_windows.append(
                    existing.model_copy(update={"day_of_week": day, "start_time": start, "end_time": end, "updated_at": now})
                )
            else:
                new_windows.append(
                    MailSendWindow(
                        window_id=str(uuid.uuid4()),
                        mail_campaign_id=mail_campaign_id,
                        day_of_week=day,
                        start_time=start,
                        end_time=end,
                        created_at=now,
                        updated_at=now,
                    )
                )
        await self.window_store.replace_for_campaign(mail_campaign_id, new_windows)

        updated_campaign = campaign.model_copy(update={"timezone": timezone_name, "updated_at": now})
        await self.campaign_store.save(updated_campaign)

        await self.activity_log.record(
            event_type="mail_campaign.schedule_updated",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated_campaign.name}" schedule was updated ({len(new_windows)} send window{"s" if len(new_windows) != 1 else ""}).',
            entity_type="mail_campaign",
            entity_id=mail_campaign_id,
            entity_name=updated_campaign.name,
            actor=actor,
        )

        return MailCampaignSchedule(
            mail_campaign_id=mail_campaign_id, timezone=timezone_name, source="windows", windows=new_windows
        )

    # --- State transitions -------------------------------------------------

    async def mark_ready(
        self, mail_campaign_id: str, suppressed_emails: set[str], actor: str | None = None
    ) -> MailCampaign:
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

        source_list_exists = False
        contacts: list[Any] = []
        if campaign.source_list_id:
            try:
                await self.crm_service.get_contact_list(campaign.source_list_id)
                source_list_exists = True
                contacts = (
                    await self.crm_service.get_list_contacts(campaign.source_list_id, page=1, page_size=_ALL_CONTACTS_PAGE_SIZE)
                ).items
            except CrmContactListNotFound:
                source_list_exists = False

        steps = await self.step_store.list_for_campaign(mail_campaign_id)
        has_connected_mailbox = await self._has_connected_selected_mailbox(mail_campaign_id)
        resolved_windows, _schedule_source = await self._resolve_schedule(mail_campaign_id, campaign)

        reasons = _compute_readiness_warnings(
            campaign, source_list_exists, len(steps), has_connected_mailbox, resolved_windows
        )
        if reasons:
            raise MailCampaignNotReadyError(mail_campaign_id, reasons)

        # Lazy normalization of the Step 1 delay_days invariant (see
        # add_step()/update_step()/_renumber()'s docstrings) for a legacy
        # sequence created before that invariant existed. Deliberately
        # placed AFTER the readiness check above and BEFORE anything else
        # below: a campaign that isn't otherwise ready is never touched by
        # this at all, and this step-store write is a single, ordinary,
        # atomic step save (same primitive add_step/update_step/_renumber
        # already use) -- not a new kind of write. There is no cross-store
        # transaction wrapping this whole method (the enrollment loop below
        # already commits one row at a time, unchanged by this addition),
        # so this is not claiming end-to-end atomicity for mark_ready() as
        # a whole -- only that this specific, always-correct normalization
        # cannot itself be applied "halfway," and can never fire for a
        # campaign whose Ready attempt was going to fail anyway.
        first_step = next((s for s in steps if s.step_number == 1), None)
        if first_step is not None and first_step.delay_days != 0:
            await self.step_store.save(first_step.model_copy(update={"delay_days": 0, "updated_at": datetime.now(timezone.utc)}))

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
            actor=actor,
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
                actor=actor,
            )
        return updated

    async def unlock_campaign(self, mail_campaign_id: str, actor: str | None = None) -> MailCampaign:
        """READY -> DRAFT. Deletes every MailEnrollment AND MailEnrollmentStep
        row for this campaign FIRST (the snapshot -- and any execution rows
        that could only ever have been created from an ACTIVE campaign this
        one never was, since there is no ACTIVE->DRAFT path in this phase --
        are only ever valid while READY; once unlocked, the audience/
        sequence/schedule can change, making the old snapshot meaningless)
        -- a subsequent mark_ready() re-snapshots fresh against whatever the
        list looks like at that point. In practice a READY campaign has
        never been ACTIVE (unlock only exists from READY, and there is no
        way back to READY from ACTIVE/PAUSED/COMPLETED), so the
        MailEnrollmentStep delete is defense-in-depth, not a case expected
        to ever find rows -- deleting nothing is a harmless no-op."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.READY:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "unlock")

        await self.enrollment_step_store.delete_for_campaign(mail_campaign_id)
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
            actor=actor,
        )
        return updated

    async def activate_campaign(self, mail_campaign_id: str, actor: str | None = None) -> MailCampaign:
        """READY -> ACTIVE. The ONLY way execution is ever allowed to begin
        -- nothing else in this codebase sets ACTIVE.

        READY cannot be blindly trusted as still executable: Channels can
        still change while READY (see set_channel_mailboxes()'s docstring),
        so this re-runs the EXACT SAME readiness validation mark_ready()
        used (_compute_readiness_warnings(), called with freshly re-fetched
        data, not whatever was true when this campaign became READY).
        Raises MailCampaignNotReadyError (same exception mark_ready() uses,
        listing every problem found) if anything now fails -- the campaign
        remains READY, no MailEnrollmentStep row is created, no enrollment
        status changes, nothing is written at all.

        FORMAL PREPARE -> COMMIT CONTRACT (a deliberate, adopted design --
        not a workaround standing in for a transaction this codebase
        doesn't have). There is no cross-store transaction anywhere here
        (see sqlite_txn.py's docstring), and a production audit confirmed
        that adding one solely for this operation would be disproportionate.
        The requirement this method actually guarantees instead:

            A campaign must never become ACTIVE until every applicable
            enrollment (every one NOT already SUPPRESSED at snapshot time)
            has been transitioned to ACTIVE with exactly one Step 1
            MailEnrollmentStep row materialized.

        This is safe specifically because partial preparation while the
        campaign remains READY is HARMLESS: nothing can ever act on a
        materialized-but-uncommitted Step 1 row while status != ACTIVE --
        MailSendingService.process_one_due_step()'s first check is exactly
        that. So the method is structured as two explicit phases:

          PREPARE (_prepare_activation()): campaign remains READY
          throughout. Every currently-PENDING enrollment is processed
          idempotently -- Step 1 materialized (create_step1_execution(),
          itself idempotent by the same contract mark_ready()'s own
          snapshot loop relies on), enrollment flipped to ACTIVE. A
          failure partway through (raised exception) leaves some
          enrollments prepared and others not, and simply propagates --
          the campaign is NEVER touched by this phase.

          COMMIT (only reached if PREPARE's loop returns without raising):
          performs an EXPLICIT, freshly-re-queried completeness check
          (_find_incomplete_activation()) -- deliberately NOT inferred
          from "the PREPARE loop above didn't raise." If it finds any
          enrollment that should be ACTIVE-with-a-Step-1-row but isn't,
          this method returns the campaign UNCHANGED (still READY) rather
          than transitioning -- the only way that can legitimately happen
          is a bug elsewhere, since a successful PREPARE loop always
          leaves every applicable enrollment complete, but the check exists
          so that invariant is verified, not merely assumed. Only once the
          check finds nothing incomplete does the campaign transition
          READY -> ACTIVE.

        Resumability, not atomicity: a repeated activate_campaign() call
        (whether after a PREPARE failure, or even on an already-fully-
        activated campaign) simply resumes PREPARE (a no-op loop once
        nothing is PENDING) and re-runs the SAME completeness check before
        COMMIT -- there is deliberately only one code path for "first
        successful activation" and "retry after partial failure," not two.

        Checked FIRST, before even fetching the campaign: settings.
        mail_sending_engine_enabled must be True, or this raises
        MailSendingEngineDisabledError regardless of this campaign's own
        status/readiness -- see that setting's docstring in app/config.py
        for why this deployment-wide gate exists independent of the
        frontend simply not exposing the button.
        """
        if not settings.mail_sending_engine_enabled:
            raise MailSendingEngineDisabledError(mail_campaign_id)

        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.READY:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "activate")

        source_list_exists = False
        if campaign.source_list_id:
            try:
                await self.crm_service.get_contact_list(campaign.source_list_id)
                source_list_exists = True
            except CrmContactListNotFound:
                source_list_exists = False

        steps = await self.step_store.list_for_campaign(mail_campaign_id)
        has_connected_mailbox = await self._has_connected_selected_mailbox(mail_campaign_id)
        resolved_windows, _schedule_source = await self._resolve_schedule(mail_campaign_id, campaign)

        reasons = _compute_readiness_warnings(
            campaign, source_list_exists, len(steps), has_connected_mailbox, resolved_windows
        )
        if reasons:
            raise MailCampaignNotReadyError(mail_campaign_id, reasons)

        assert campaign.timezone is not None  # guaranteed by the readiness check above
        step1 = next(s for s in steps if s.step_number == 1)  # guaranteed to exist: step_count > 0 checked above
        now = datetime.now(timezone.utc)

        # --- PREPARE ---
        activated = await self._prepare_activation(mail_campaign_id, step1, resolved_windows, campaign.timezone, now)

        # --- COMMIT: explicit completeness verification ---
        incomplete = await self._find_incomplete_activation(mail_campaign_id, step1)
        if incomplete:
            # PREPARE is not (yet) complete -- refuse to commit. Returning
            # the still-READY campaign (never raising here) is what makes
            # a repeated activate_campaign() call the normal, expected way
            # to finish, rather than treating this as an error condition.
            return campaign

        updated = campaign.model_copy(
            update={"status": MailCampaignStatus.ACTIVE, "execution_active_since": now, "updated_at": now}
        )
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.activated",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was activated ({activated} lead{"s" if activated != 1 else ""} started).',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
            metadata={"activated": activated},
            actor=actor,
        )
        return updated

    async def _prepare_activation(
        self, mail_campaign_id: str, step1: MailSequenceStep, windows: list[MailSendWindow], timezone_name: str, now: datetime
    ) -> int:
        """PREPARE phase of activate_campaign() -- see that method's own
        docstring for the full PREPARE/COMMIT contract. Every currently-
        PENDING enrollment is processed idempotently; the campaign's own
        status is never touched here. Returns how many enrollments this
        specific call newly activated (for the activity log summary --
        0 on a pure retry/no-op call)."""
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        activated = 0
        for enrollment in enrollments:
            if enrollment.status != MailEnrollmentStatus.PENDING:
                continue
            await self.sending_service.create_step1_execution(
                enrollment=enrollment, step1=step1, windows=windows, timezone_name=timezone_name, now=now
            )
            await self.enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))
            activated += 1
        return activated

    async def _find_incomplete_activation(self, mail_campaign_id: str, step1: MailSequenceStep) -> list[str]:
        """COMMIT-gate of activate_campaign() -- an explicit, freshly-
        re-queried verification of the activation invariant, never
        inferred from "the PREPARE loop above didn't raise." Returns the
        enrollment_ids of every enrollment that is NOT SUPPRESSED (i.e. is
        "applicable") but does not YET satisfy "ACTIVE with exactly one
        Step 1 MailEnrollmentStep row" -- empty means PREPARE is fully
        complete and COMMIT may proceed. Re-reads both stores directly
        rather than reusing anything the PREPARE loop computed, so a
        future change to that loop's logic can never silently violate this
        invariant without this check catching it."""
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        incomplete: list[str] = []
        for enrollment in enrollments:
            if enrollment.status == MailEnrollmentStatus.SUPPRESSED:
                continue  # never applicable -- correctly excluded at mark_ready() snapshot time
            if enrollment.status != MailEnrollmentStatus.ACTIVE:
                incomplete.append(enrollment.enrollment_id)
                continue
            row = await self.enrollment_step_store.get_by_enrollment_and_step(enrollment.enrollment_id, step1.step_id)
            if row is None:
                incomplete.append(enrollment.enrollment_id)
        return incomplete

    async def pause_campaign(self, mail_campaign_id: str, actor: str | None = None) -> MailCampaign:
        """ACTIVE -> PAUSED. Stops NEW claims from this campaign (every
        MailSendingService.process_one_due_step() call checks campaign
        status first) without touching a single MailEnrollment or
        MailEnrollmentStep row -- pausing execution is not the same as
        unlocking configuration (see MailCampaignStatus.PAUSED's docstring):
        Channels/Steps/Schedule remain exactly as locked as they were at
        ACTIVE (set_channel_mailboxes() locks on PAUSED too)."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.ACTIVE:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "pause")

        now = datetime.now(timezone.utc)
        updated = campaign.model_copy(
            update={"status": MailCampaignStatus.PAUSED, "execution_active_since": None, "updated_at": now}
        )
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.paused",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was paused.',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
            actor=actor,
        )
        return updated

    async def resume_campaign(self, mail_campaign_id: str) -> MailCampaign:
        """PAUSED -> ACTIVE. Unlike activate_campaign(), this does NOT
        re-run the full readiness checklist -- audience/steps/schedule
        cannot have changed while PAUSED (everything but Channels is locked
        from READY onward, and Channels themselves are ALSO locked once
        PAUSED -- see set_channel_mailboxes()). The one thing that CAN
        genuinely change independent of any locked edit is a mailbox's own
        live status (e.g. it becomes disconnected at the mailbox level), so
        this re-checks only that: at least one of this campaign's selected
        mailboxes must currently be CONNECTED, or resume is refused
        (MailCampaignNotReadyError, campaign stays PAUSED, nothing written).

        Gated by settings.mail_sending_engine_enabled, same as
        activate_campaign() -- this also produces an ACTIVE campaign, so
        the same deployment-wide safety gate applies here too (see that
        setting's docstring in app/config.py)."""
        if not settings.mail_sending_engine_enabled:
            raise MailSendingEngineDisabledError(mail_campaign_id)

        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status != MailCampaignStatus.PAUSED:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "resume")

        if not await self._has_connected_selected_mailbox(mail_campaign_id):
            raise MailCampaignNotReadyError(
                mail_campaign_id, ["At least one connected sending inbox must be selected."]
            )

        now = datetime.now(timezone.utc)
        updated = campaign.model_copy(
            update={"status": MailCampaignStatus.ACTIVE, "execution_active_since": now, "updated_at": now}
        )
        await self.campaign_store.save(updated)
        await self.activity_log.record(
            event_type="mail_campaign.resumed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'Mail Campaign "{updated.name}" was resumed.',
            entity_type="mail_campaign",
            entity_id=updated.mail_campaign_id,
            entity_name=updated.name,
        )
        return updated

    async def archive_campaign(self, mail_campaign_id: str) -> MailCampaign:
        """Any non-ARCHIVED status -> ARCHIVED (terminal in this phase --
        no un-archive). Enrollment/execution rows, if any, are left as-is
        -- harmless historical record, never read by anything once
        archived (MailSendingService.process_one_due_step()'s first check
        is campaign.status == ACTIVE, which an archived campaign can never
        satisfy again)."""
        campaign = await self._require_campaign(mail_campaign_id)
        if campaign.status == MailCampaignStatus.ARCHIVED:
            raise MailCampaignInvalidTransitionError(mail_campaign_id, campaign.status, "archive")

        now = datetime.now(timezone.utc)
        updated = campaign.model_copy(
            update={
                "status": MailCampaignStatus.ARCHIVED,
                "execution_active_since": None,
                "archived_at": now,
                "updated_at": now,
            }
        )
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

    # --- Workload / prospect batches (Phase 2, 2026-09-03) ----------------

    async def get_workload(self, mail_campaign_id: str) -> MailCampaignWorkload:
        """Enrollment-status counts for this campaign -- WORKLOAD, entirely
        independent of the campaign's own lifecycle `status` (see
        MailCampaignWorkload's own docstring). Pure read, computed fresh
        from every enrollment currently on file; never cached. Callable at
        any campaign status, including DRAFT (always zero, since
        mark_ready() hasn't run yet) and legacy COMPLETED (its historical
        counts, unchanged by this read)."""
        await self._require_campaign(mail_campaign_id)
        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        counts = {status: 0 for status in MailEnrollmentStatus}
        for enrollment in enrollments:
            counts[enrollment.status] += 1
        return MailCampaignWorkload(
            mail_campaign_id=mail_campaign_id,
            total=len(enrollments),
            pending=counts[MailEnrollmentStatus.PENDING],
            active=counts[MailEnrollmentStatus.ACTIVE],
            paused=counts[MailEnrollmentStatus.PAUSED],
            completed=counts[MailEnrollmentStatus.COMPLETED],
            suppressed=counts[MailEnrollmentStatus.SUPPRESSED],
            failed=counts[MailEnrollmentStatus.FAILED],
        )

    async def list_batches(self, mail_campaign_id: str) -> list[MailEnrollmentBatch]:
        """Every prospect batch ever added to this campaign via
        add_prospects(), newest first. Empty for every campaign that
        predates that feature, or that has simply never had a batch
        added -- see MailEnrollmentBatch's own docstring on why that's
        valid, permanent state, not a gap to backfill."""
        await self._require_campaign(mail_campaign_id)
        return await self.batch_store.list_for_campaign(mail_campaign_id)

    async def add_prospects(
        self,
        mail_campaign_id: str,
        source: MailEnrollmentBatchSource,
        idempotency_key: str,
        source_list_id: str | None = None,
        source_import_batch_id: str | None = None,
        actor: str | None = None,
    ) -> MailEnrollmentBatch:
        """The persistent-campaign entry point for growing an already-
        prepared campaign's audience -- unlike mark_ready()'s one-time
        snapshot, this is safe to call any number of times against an
        ACTIVE or PAUSED campaign (new prospects queue behind whatever's
        already there; nothing sends while PAUSED, via the existing
        campaign.status==ACTIVE gate in prepare_and_send_step() -- no new
        gating code needed), and against a legacy COMPLETED campaign,
        where it can REOPEN it to ACTIVE -- but only as a side effect of
        genuinely enrolling someone new (see _reconcile_batch()'s own
        docstring for exactly when/how; a submission that turns out to
        enroll nobody new leaves a COMPLETED campaign COMPLETED). Refuses
        DRAFT/READY (use the audience+Mark Ready path instead) and
        ARCHIVED (terminal) via MailCampaignNotEligibleForProspectsError.

        Both source=CRM_LIST (Stage 3) and source=CSV_UPLOAD (Stage 4B,
        2026-09-03) are implemented. CSV_UPLOAD resolves its candidates via
        `crm_import_reader.list_resolved_contact_ids(source_import_batch_id)`
        -- the narrow, read-only CrmImportResolutionReader Protocol (see
        this file's own module-level docstring for that type) -- which
        itself requires the referenced CrmImportBatch to already be fully
        COMMITTED (raises otherwise). This method NEVER commits a CRM
        import itself, exactly as it never mutates a CrmContactList for
        the CRM_LIST source: resolving an already-committed import is a
        pure read, structurally identical in kind to reading a
        CrmContactList's current membership. Triggering the actual CRM
        commit is the job of MailCampaignCsvProspectService (app/services/
        mail_campaign_csv_prospect_service.py), the one component in this
        codebase allowed to hold a writable CrmImportService -- see that
        service's own docstring for the full orchestration ordering
        (durable link -> eligibility preflight -> commit -> this method).

        OPERATION-LEVEL IDEMPOTENCY: `idempotency_key` is required and is
        the ENTIRE mechanism preventing an HTTP retry (lost response,
        network retry, accidental double-submit) from creating a second,
        duplicate batch for the same logical submission -- see
        MailEnrollmentBatchStore's UNIQUE(mail_campaign_id,
        idempotency_key) constraint. If a batch already exists for this
        exact (campaign, key) pair -- whether READY (returned unchanged,
        nothing re-resolved/re-enrolled) or still PREPARING (reconciled
        and returned, resuming from its already-frozen cohort, never
        re-reading the CRM List) -- this call resolves to THAT batch,
        full stop; the source is never re-resolved once a batch row for
        this key exists.

        THE FREEZE (only for a genuinely new (campaign, key) pair):
        resolves the CRM List's CURRENT membership (a read; safe to
        redo if this exact freeze attempt itself needs to restart -- see
        below), dedupes it, and writes one MailEnrollmentBatchMember row
        per candidate (state=CANDIDATE) BEFORE the owning
        MailEnrollmentBatch row is ever created. This ordering is
        deliberate: the batch row's existence is the ONLY durable signal
        that "this cohort is frozen and committed" -- nothing before that
        point has ever been returned to any caller or become visible via
        GET .../batches, so a crash anywhere before the batch row commits
        (0, some, or all member rows written) is safe to treat as "never
        happened": a retry with the same idempotency_key finds no batch
        row yet, restarts the ENTIRE freeze from scratch under a FRESH
        batch_id, and the old orphaned member rows are cleaned up later by
        cleanup_orphan_batch_members() (never revisited or reconciled --
        nothing ever looks up member rows except through an owning batch
        row). Once the batch row exists, this guarantee flips to strict
        immutability: see _reconcile_batch().

        CONCURRENT RACE (two genuinely simultaneous submissions with the
        same key, not a crash): both may independently freeze their own
        candidate cohorts before either commits its batch row; only one
        `batch_store.create()` can win the UNIQUE(campaign_id,
        idempotency_key) constraint. The loser catches
        DuplicateBatchIdempotencyKeyError, looks up the winner via
        get_by_idempotency_key(), and reconciles/returns THAT one --
        never proceeding to create any enrollment under its own,
        now-abandoned batch_id. Only the winner's cohort ever produces
        real MailEnrollment rows.

        IDEMPOTENCY LOOKUP RUNS BEFORE ELIGIBILITY (2026-09-03 refinement):
        an existing `(campaign_id, idempotency_key)` match is ALWAYS
        looked up and reconciled/returned FIRST, before this method even
        checks the campaign's current status -- otherwise a campaign that
        gets ARCHIVED after a submission was already accepted would make
        every subsequent retry of that exact submission (a lost response,
        a client retry) raise MailCampaignNotEligibleForProspectsError
        instead of returning the batch that already exists, which would
        make operation-level idempotency unreliable across an Archive.
        This is safe specifically because _reconcile_batch() itself
        already refuses to do anything beyond a pure read the moment the
        owning campaign is ARCHIVED (see that method's own docstring) --
        so an existing batch belonging to a since-ARCHIVED campaign is
        simply returned as-is (whatever status it already reached),
        never advanced, never creating new enrollment/step work, never
        touching campaign status. The eligibility check below therefore
        only ever gates a GENUINELY NEW submission (no existing batch for
        this key) -- DRAFT/READY/ARCHIVED are still refused for that case,
        exactly as before."""
        campaign = await self._require_campaign(mail_campaign_id)

        existing = await self.batch_store.get_by_idempotency_key(mail_campaign_id, idempotency_key)
        if existing is not None:
            return await self._reconcile_batch(existing.batch_id)

        if campaign.status not in PROSPECT_ELIGIBLE_CAMPAIGN_STATUSES:
            raise MailCampaignNotEligibleForProspectsError(mail_campaign_id, campaign.status)

        if source == MailEnrollmentBatchSource.CRM_LIST:
            if not source_list_id:
                raise ValueError("source_list_id is required when source=crm_list.")
            await self.crm_service.get_contact_list(source_list_id)  # raises CrmContactListNotFound if dangling
            contacts = (
                await self.crm_service.get_list_contacts(source_list_id, page=1, page_size=_ALL_CONTACTS_PAGE_SIZE)
            ).items
            # Same "usable email only" filter as mark_ready()'s own snapshot,
            # and the same order-preserving in-batch dedupe convention as
            # CrmService.bulk_add_to_list().
            candidate_ids = list(dict.fromkeys(c.crm_contact_id for c in contacts if normalize_email(c.email)))
        else:
            assert source == MailEnrollmentBatchSource.CSV_UPLOAD  # the only two MailEnrollmentBatchSource values
            if not source_import_batch_id:
                raise ValueError("source_import_batch_id is required when source=csv_upload.")
            # Raises CrmImportBatchNotFound / ValueError (not yet fully
            # COMMITTED) via the reader Protocol -- see this method's own
            # docstring. Already deduped by list_resolved_contact_ids()
            # itself; only the blank-email filter is this method's own
            # job, reading each contact's CURRENT, live email (never a
            # transient CSV-row value) -- same principle as the CRM_LIST
            # branch above, and the same reason _reconcile_batch() always
            # re-checks suppression fresh rather than trusting a snapshot.
            resolved_ids = await self.crm_import_reader.list_resolved_contact_ids(source_import_batch_id)
            candidate_ids = []
            for contact_id in resolved_ids:
                contact = await self.crm_service.get_contact(contact_id)
                if normalize_email(contact.email):
                    candidate_ids.append(contact_id)

        batch_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        for contact_id in candidate_ids:
            await self.batch_member_store.create(
                MailEnrollmentBatchMember(
                    batch_id=batch_id,
                    crm_contact_id=contact_id,
                    state=MailEnrollmentBatchMemberState.CANDIDATE,
                    created_at=now,
                    updated_at=now,
                )
            )

        batch = MailEnrollmentBatch(
            batch_id=batch_id,
            mail_campaign_id=mail_campaign_id,
            source=source,
            source_list_id=source_list_id,
            source_import_batch_id=source_import_batch_id,
            idempotency_key=idempotency_key,
            status=MailEnrollmentBatchStatus.PREPARING,
            created_at=now,
            created_by_actor=actor,
            submitted_count=len(candidate_ids),
        )
        try:
            await self.batch_store.create(batch)
        except DuplicateBatchIdempotencyKeyError:
            winner = await self.batch_store.get_by_idempotency_key(mail_campaign_id, idempotency_key)
            assert winner is not None  # the collision itself proves one exists
            return await self._reconcile_batch(winner.batch_id)

        return await self._reconcile_batch(batch_id)

    async def _reconcile_batch(self, batch_id: str) -> MailEnrollmentBatch:
        """Advances one batch as far as it can go RIGHT NOW, entirely from
        durable state -- never from a caller-supplied snapshot of
        anything (suppression, CRM list membership, or otherwise). Safe
        to call any number of times, from any context: synchronously at
        the end of add_prospects() (the common, no-crash case), from a
        retry that found an existing PREPARING/READY batch, from
        reconcile_all_preparing_batches()'s periodic/startup sweep -- all
        four call shapes run this exact same function.

        ARCHIVED campaigns: reconciliation refuses to create any new
        executable work against one -- returns the batch completely
        unchanged (even a still-PREPARING one) the moment the OWNING
        campaign is found to be ARCHIVED. This can only happen if a
        campaign was archived after a batch was submitted but before
        reconciliation caught up; the batch is left exactly where it is
        (a human decision, not something this method resolves on its
        own).

        For an already-READY batch: member processing (below) is skipped
        entirely -- never redone, never re-verified. Only the campaign-
        reopen check (last) is ALWAYS re-run, unconditionally, even for
        an already-READY batch -- this is what correctly finishes a crash
        that happened after READY was durably written but before the
        legacy-COMPLETED-→ACTIVE flip landed (see the ordering note
        below).

        For a PREPARING batch, in order:
          1. Every still-CANDIDATE member is resolved: a fresh suppression
             check (self.suppression_store.get(), NEVER a caller-supplied
             snapshot -- this is what makes reconciliation genuinely
             self-contained across a periodic sweep or app startup, which
             have no request-scoped suppression data to hand it) decides
             SUPPRESSED vs PENDING for a brand-new MailEnrollment; the
             existing PK-idempotent enrollment_store.create() decides
             ALREADY_ENROLLED vs a genuinely new enrollment. A suppressed
             new enrollment becomes ENROLLED_SUPPRESSED (terminal, no
             Step 1 ever); a normal one becomes ENROLLED_PENDING.
          2. Every ENROLLED_PENDING member gets its Step 1 materialized,
             via the campaign's CURRENT schedule (freshly re-resolved,
             exactly as activate_campaign() itself does -- never a stale
             or batch-specific timing rule) and the exact same
             MailSendingService.create_step1_execution() call
             activate_campaign() uses -- then flips PENDING -> ACTIVE and
             the member to PREPARED.
          3. Completeness is verified by re-fetching every member fresh
             (never trusting an in-memory accumulator) and confirming none
             remain CANDIDATE or ENROLLED_PENDING. Incomplete -> return
             the batch exactly as it was (still PREPARING); a later call
             (retry, sweep) picks up wherever this one left off, since
             every member's OWN state -- not "how far this call got" --
             is what determines what's left to do.
          4. Final counts are computed FRESH from durable member states at
             this exact moment (never accumulated across however many
             calls it took to get here) and written to the batch row
             together with status=READY, in ONE save() call.

        Campaign reopening happens ONLY after that READY write lands, and
        ONLY if this batch's enrolled_count is genuinely > 0 -- so a
        submission against a legacy COMPLETED campaign that turns out to
        contain only already-enrolled contacts (enrolled_count == 0)
        leaves that campaign COMPLETED, never flips it. If the process
        crashes between the READY write and this flip, the batch is
        already durably READY with its final counts -- a later
        reconciliation call sees status==READY, skips straight past
        member processing, and finishes only the one remaining, cheap,
        idempotent write. ACTIVE/PAUSED campaigns are never touched by
        this step at all (only campaign.status == COMPLETED triggers
        it)."""
        batch = await self.batch_store.get(batch_id)
        if batch is None:
            raise MailEnrollmentBatchNotFoundError(batch_id)

        campaign = await self._require_campaign(batch.mail_campaign_id)
        if campaign.status == MailCampaignStatus.ARCHIVED:
            return batch

        if batch.status != MailEnrollmentBatchStatus.READY:
            members = await self.batch_member_store.list_for_batch(batch_id)

            for member in members:
                if member.state != MailEnrollmentBatchMemberState.CANDIDATE:
                    continue
                contact = await self.crm_service.get_contact(member.crm_contact_id)
                normalized = normalize_email(contact.email)
                suppression = await self.suppression_store.get(normalized) if normalized else None
                is_suppressed = suppression is not None and suppression.active

                now = datetime.now(timezone.utc)
                enrollment = MailEnrollment(
                    enrollment_id=str(uuid.uuid4()),
                    mail_campaign_id=batch.mail_campaign_id,
                    crm_contact_id=member.crm_contact_id,
                    email_at_enrollment=contact.email,
                    status=MailEnrollmentStatus.SUPPRESSED if is_suppressed else MailEnrollmentStatus.PENDING,
                    enrolled_at=now,
                    created_at=now,
                    batch_id=batch_id,
                )
                is_new = await self.enrollment_store.create(enrollment)
                if is_new:
                    new_state = (
                        MailEnrollmentBatchMemberState.ENROLLED_SUPPRESSED
                        if is_suppressed
                        else MailEnrollmentBatchMemberState.ENROLLED_PENDING
                    )
                    await self.batch_member_store.save(
                        member.model_copy(update={"state": new_state, "enrollment_id": enrollment.enrollment_id, "updated_at": now})
                    )
                else:
                    await self.batch_member_store.save(
                        member.model_copy(update={"state": MailEnrollmentBatchMemberState.ALREADY_ENROLLED, "updated_at": now})
                    )

            members = await self.batch_member_store.list_for_batch(batch_id)
            pending_members = [m for m in members if m.state == MailEnrollmentBatchMemberState.ENROLLED_PENDING]
            if pending_members:
                steps = await self.step_store.list_for_campaign(batch.mail_campaign_id)
                step1 = next(s for s in steps if s.step_number == 1)  # guaranteed: this campaign was READY once
                resolved_windows, _source = await self._resolve_schedule(batch.mail_campaign_id, campaign)
                assert campaign.timezone is not None  # guaranteed by the same readiness check activation required
                now = datetime.now(timezone.utc)
                for member in pending_members:
                    enrollment = await self.enrollment_store.get(member.enrollment_id)
                    if enrollment is None:
                        continue  # defensive only -- this member's own create() call above guarantees it exists
                    await self.sending_service.create_step1_execution(
                        enrollment=enrollment, step1=step1, windows=resolved_windows, timezone_name=campaign.timezone, now=now
                    )
                    await self.enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))
                    await self.batch_member_store.save(
                        member.model_copy(update={"state": MailEnrollmentBatchMemberState.PREPARED, "updated_at": now})
                    )

            members = await self.batch_member_store.list_for_batch(batch_id)
            incomplete = [
                m
                for m in members
                if m.state in (MailEnrollmentBatchMemberState.CANDIDATE, MailEnrollmentBatchMemberState.ENROLLED_PENDING)
            ]
            if incomplete:
                return batch  # still PREPARING -- a later call finishes it

            enrolled_count = sum(
                1
                for m in members
                if m.state in (MailEnrollmentBatchMemberState.ENROLLED_SUPPRESSED, MailEnrollmentBatchMemberState.PREPARED)
            )
            already_enrolled_count = sum(1 for m in members if m.state == MailEnrollmentBatchMemberState.ALREADY_ENROLLED)
            suppressed_count = sum(1 for m in members if m.state == MailEnrollmentBatchMemberState.ENROLLED_SUPPRESSED)

            batch = batch.model_copy(
                update={
                    "status": MailEnrollmentBatchStatus.READY,
                    "enrolled_count": enrolled_count,
                    "already_enrolled_count": already_enrolled_count,
                    "suppressed_count": suppressed_count,
                }
            )
            await self.batch_store.save(batch)

        if batch.enrolled_count and batch.enrolled_count > 0 and campaign.status == MailCampaignStatus.COMPLETED:
            now = datetime.now(timezone.utc)
            reopened = campaign.model_copy(
                update={"status": MailCampaignStatus.ACTIVE, "execution_active_since": now, "updated_at": now}
            )
            await self.campaign_store.save(reopened)
            await self.activity_log.record(
                event_type="mail_campaign.activated",
                category=ActivityCategory.MAIL,
                source=ActivitySource.MAIL_SYSTEM,
                summary=f'Mail Campaign "{reopened.name}" was reactivated (a prospect batch added {batch.enrolled_count} new lead{"s" if batch.enrolled_count != 1 else ""}).',
                entity_type="mail_campaign",
                entity_id=reopened.mail_campaign_id,
                entity_name=reopened.name,
                metadata={"activated": batch.enrolled_count},
                actor=batch.created_by_actor,
            )

        return batch

    async def reconcile_all_preparing_batches(self) -> int:
        """The discovery half of the startup/periodic reconciliation
        sweep (see app/services/mail_batch_reconciliation_worker.py) --
        finds every batch, across ALL campaigns, currently PREPARING and
        reconciles each one. This is what guarantees a PREPARING batch
        can never be silently stranded forever even if no client ever
        retries the original submission (e.g. a user closed their tab
        after a lost response). Deliberately independent of
        settings.mail_sending_engine_enabled -- this is pure campaign/
        enrollment bookkeeping, never a Gmail/provider call. Returns how
        many batches this call newly advanced to READY."""
        preparing = await self.batch_store.list_by_status(MailEnrollmentBatchStatus.PREPARING)
        newly_ready = 0
        for batch in preparing:
            result = await self._reconcile_batch(batch.batch_id)
            if result.status == MailEnrollmentBatchStatus.READY:
                newly_ready += 1
        return newly_ready

    async def cleanup_orphan_batch_members(
        self, now: datetime, older_than: timedelta = timedelta(hours=1)
    ) -> int:
        """Deletes MailEnrollmentBatchMember rows whose batch_id has NO
        corresponding MailEnrollmentBatch row at all -- the orphans left
        behind by add_prospects() restarting a freeze from scratch under
        a fresh batch_id after a crash (see that method's own docstring).
        Deliberately narrow, not a general-purpose garbage collector:
        only ever looks at this one specific orphan shape.

        `older_than` (default 1 hour) is the conservative age threshold
        that keeps this from ever racing an in-progress freeze -- a
        member row younger than this is never even considered, no matter
        how many of its sibling rows already exist, because a real
        freeze in progress (writing member rows for a large CRM List) is
        never expected to take anywhere close to this long. A batch row
        for a truly in-progress or already-committed freeze is NEVER
        deleted by this method, at any age -- only member rows with
        NO owning batch row at all are ever candidates.

        Idempotent: a batch_id already cleaned up simply won't appear in
        the next call's candidate list (its member rows are gone), and
        deleting an already-empty batch_id's members is a harmless no-op
        (delete_for_batch() returns 0). Safe at startup and periodically,
        for the same reason reconcile_all_preparing_batches() is: no
        Gmail/provider call anywhere in this path. Returns the total
        number of member rows deleted."""
        cutoff = now - older_than
        candidate_batch_ids = await self.batch_member_store.list_distinct_batch_ids_created_before(cutoff)
        deleted = 0
        for batch_id in candidate_batch_ids:
            if await self.batch_store.get(batch_id) is None:
                deleted += await self.batch_member_store.delete_for_batch(batch_id)
        return deleted

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
        has_connected_mailbox = await self._has_connected_selected_mailbox(mail_campaign_id)
        resolved_windows, _schedule_source = await self._resolve_schedule(mail_campaign_id, campaign)

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
                "Daily sending capacity isn't available yet -- Astronomic Mail has no per-mailbox "
                "send-volume limits or a sending engine yet, regardless of how many inboxes are connected."
            ),
            readiness_warnings=_compute_readiness_warnings(
                campaign, source_list_exists, step_count, has_connected_mailbox, resolved_windows
            ),
        )
