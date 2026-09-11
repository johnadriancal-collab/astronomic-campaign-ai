"""
Client CRM Stage 1H-B (2026-09-10) -- syncs ONE already-persisted
LumaRegistration to its linked Engagement's EngagementParticipant, if and
only if that Engagement is explicitly linked (Stage 1H-A's
Engagement.luma_event_id) and the registration is confidently matched to a
canonical Contact. This is the one reusable function Stage 1H-C's future
historical backfill is meant to call too -- everything here operates on a
single already-saved LumaRegistration and has no awareness of "live
webhook" vs. "backfill" as a concept, exactly mirroring how
LumaSyncService.process_guest_event() itself is shared by both paths.

Link-only sync, NOT the reverse: this module never creates, links, or
unlinks an Engagement<->Luma event relationship (Stage 1H-A owns that
entirely) -- it only ever reads Engagement.luma_event_id via
EngagementStore.get_by_luma_event_id().

Never invoked automatically over history: nothing in this module iterates
LumaRegistrationStore, runs at app startup, or runs when an Engagement is
newly linked. The ONLY caller (see luma_sync_service.py's own
_process_guest_event_locked()) is the live webhook/backfill guest-processing
path, invoked once per already-processed registration, exactly once per
call. Historical catch-up for registrations that already existed before an
Engagement was linked is Stage 1H-C's own explicit, separate, approved
scope -- not automatic, not implemented here.

MUST NEVER create an EngagementParticipant without a resolved canonical
Contact (registration.crm_contact_id), and MUST NEVER treat a manually-
created participant's own role/source/is_walk_in/attendance_status/archived
state as something Luma is allowed to touch -- see
sync_luma_registration_to_engagement_participant()'s own docstring for the
complete eligibility/create/update contract.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from loguru import logger

from app.models.activity import ActivityCategory, ActivitySource
from app.models.client_crm import (
    DeclineOrigin,
    Engagement,
    EngagementParticipant,
    EngagementStatus,
    ParticipantRole,
    ParticipantRsvpStatus,
    ParticipantSource,
)
from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaRegistration
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.engagement_participant_store import (
    EngagementParticipantDuplicateError,
    EngagementParticipantStore,
)
from app.repositories.engagement_store import EngagementStore
from app.services.activity_log_service import ActivityLogService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService

# Client CRM Stage 1H-A's own model docstring already tags LUMA as "a
# reserved value for a future, dedicated Luma-linkage stage" -- this IS
# that stage. Only these three Luma approval_status values map to a
# defined ParticipantRsvpStatus; every other value (pending_approval,
# waitlist, session, or anything unrecognized) is a LOCKED product
# decision to leave rsvp_status alone -- null on a new participant, or
# whatever it already was on an existing one. Never invents a new
# ParticipantRsvpStatus value to cover them.
_RSVP_STATUS_BY_APPROVAL_STATUS: dict[LumaApprovalStatus, ParticipantRsvpStatus] = {
    LumaApprovalStatus.APPROVED: ParticipantRsvpStatus.CONFIRMED,
    LumaApprovalStatus.INVITED: ParticipantRsvpStatus.INVITED,
    LumaApprovalStatus.DECLINED: ParticipantRsvpStatus.DECLINED,
}

# The exact snapshot fields Luma may fill on an EXISTING participant, and
# ONLY when that field is currently blank -- never overwrites a nonblank
# human-entered value. Same field set (and same "fill only from the
# canonical Contact" idea) as ClientCrmService.create_engagement_participant's
# own resolved-participant snapshot population.
_SNAPSHOT_FIELDS: tuple[str, ...] = ("first_name", "last_name", "email", "title", "company")


class LumaParticipantSyncOutcome(str, Enum):
    """Every possible result of one sync_luma_registration_to_engagement_participant()
    call -- deliberately granular so callers/tests can assert exactly which
    eligibility rule fired, and so Activity Log noise can be limited to
    exactly CREATED/UPDATED (see _record_activity's own docstring)."""

    NO_ENGAGEMENT_LINKED = "no_engagement_linked"
    ENGAGEMENT_ARCHIVED = "engagement_archived"
    ENGAGEMENT_CANCELLED = "engagement_cancelled"
    NOT_MATCHED = "not_matched"
    NO_CONTACT = "no_contact"
    ARCHIVED_PARTICIPANT_SKIPPED = "archived_participant_skipped"
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass
class LumaParticipantSyncResult:
    outcome: LumaParticipantSyncOutcome
    participant: EngagementParticipant | None = None


def _map_rsvp_status(approval_status: LumaApprovalStatus) -> ParticipantRsvpStatus | None:
    return _RSVP_STATUS_BY_APPROVAL_STATUS.get(approval_status)


def _participant_display_name(participant: EngagementParticipant) -> str:
    """Never falls back to email -- same Activity Log privacy convention
    as ClientCrmService._participant_display_name(); kept as a small local
    copy rather than importing that (private, leading-underscore) method
    across modules."""
    name = " ".join(part for part in (participant.first_name, participant.last_name) if part)
    return name or "Unnamed participant"


class LumaEngagementParticipantSyncService:
    def __init__(
        self,
        engagement_store: EngagementStore,
        engagement_participant_store: EngagementParticipantStore,
        crm_contact_store: CrmContactStore,
        activity_log: ActivityLogService,
        contact_engagement_signal_service: ContactEngagementSignalService,
    ):
        self.engagement_store = engagement_store
        self.engagement_participant_store = engagement_participant_store
        self.crm_contact_store = crm_contact_store
        self.activity_log = activity_log
        self.contact_engagement_signal_service = contact_engagement_signal_service

    async def sync_luma_registration_to_engagement_participant(
        self, registration: LumaRegistration, *, derived_decline_origin: DeclineOrigin | None = None
    ) -> LumaParticipantSyncResult:
        """The one entry point. Every eligibility rule below is a plain
        NO-OP (never an exception) -- an unlinked event, an archived/
        cancelled Engagement, or an unresolved registration are all normal,
        expected, non-error states. Raises only on a genuinely unexpected
        failure (e.g. a store error); the caller (see luma_sync_service.py)
        wraps this call in its own try/except so such a failure can never
        break Luma registration/Contact processing or the webhook's HTTP
        success response.

        Eligibility, in order:
        1. Engagement.luma_event_id must resolve to a stored Engagement
           (via the Stage 1H-A indexed lookup) -- else NO_ENGAGEMENT_LINKED.
        2. That Engagement must not be archived -- else ENGAGEMENT_ARCHIVED.
        3. That Engagement's status must not be CANCELLED (planned/
           confirmed/completed all allow) -- else ENGAGEMENT_CANCELLED.
        4. registration.match_status must be MATCHED -- else NOT_MATCHED.
        5. registration.crm_contact_id must be set AND resolve to a real,
           still-existing CrmContact -- else NO_CONTACT. Never creates an
           EngagementParticipant without a resolved canonical Contact.

        `derived_decline_origin` (Client CRM Stage 5A) -- already computed
        by the ONE caller (LumaSyncService._derive_decline_origin(), which
        alone still holds the pre-overwrite LumaRegistration state this
        needs); None means "no new decline fact from this delivery" (the
        new approval_status isn't declined, or nothing about it actually
        changed). This module never re-derives it itself -- see
        app/services/luma_decline_origin.py's own docstring for why that
        logic lives there and only there."""
        engagement = await self.engagement_store.get_by_luma_event_id(registration.luma_event_id)
        if engagement is None:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.NO_ENGAGEMENT_LINKED)
        if engagement.archived:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.ENGAGEMENT_ARCHIVED)
        if engagement.status == EngagementStatus.CANCELLED:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.ENGAGEMENT_CANCELLED)
        if registration.match_status != LumaMatchStatus.MATCHED or registration.crm_contact_id is None:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.NOT_MATCHED)

        contact = await self.crm_contact_store.get(registration.crm_contact_id)
        if contact is None:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.NO_CONTACT)

        existing = await self._find_existing_participant(engagement.engagement_id, contact.crm_contact_id)
        if existing is not None:
            return await self._sync_against_existing(engagement, existing, contact, registration, derived_decline_origin)
        return await self._create_participant(engagement, contact, registration, derived_decline_origin)

    async def _find_existing_participant(self, engagement_id: str, crm_contact_id: str) -> EngagementParticipant | None:
        """(engagement_id, crm_contact_id) is the canonical participant
        identity (same pair the Stage 1G partial unique index enforces).
        No dedicated store lookup exists for this pair -- list_for_engagement()
        already returns every participant (archived or not) for this
        Engagement, and this Engagement's participant list is expected to
        stay small (one dinner's guest list), so a Python-side scan is the
        right-sized approach here, same convention as e.g.
        ClientCrmService.list_clients()'s own in-memory filtering."""
        participants = await self.engagement_participant_store.list_for_engagement(engagement_id)
        return next((p for p in participants if p.crm_contact_id == crm_contact_id), None)

    async def _create_participant(
        self,
        engagement: Engagement,
        contact: CrmContact,
        registration: LumaRegistration,
        derived_decline_origin: DeclineOrigin | None,
    ) -> LumaParticipantSyncResult:
        now = datetime.now(timezone.utc)
        rsvp_status = _map_rsvp_status(registration.approval_status)
        participant = EngagementParticipant(
            participant_id=str(uuid.uuid4()),
            engagement_id=engagement.engagement_id,
            client_id=engagement.client_id,
            crm_contact_id=contact.crm_contact_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            email=contact.email,
            title=contact.title,
            company=contact.company,
            role=ParticipantRole.GUEST,
            rsvp_status=rsvp_status,
            # Stage 5A: decline_origin is meaningful ONLY alongside DECLINED
            # -- a brand-new participant has no prior human assertion to
            # protect, so this is always the fresh Luma-derived value (or
            # None if rsvp_status isn't DECLINED at all).
            decline_origin=derived_decline_origin if rsvp_status == ParticipantRsvpStatus.DECLINED else None,
            decline_origin_is_manual=False,
            is_walk_in=False,
            source=ParticipantSource.LUMA,
            created_at=now,
            updated_at=now,
        )
        try:
            await self.engagement_participant_store.create(participant)
        except EngagementParticipantDuplicateError:
            # Race: a concurrent execution (a second webhook delivery for a
            # DIFFERENT LumaRegistration row for the same Contact+event, or
            # a concurrent backfill call) created a participant for this
            # (engagement, contact) between our lookup and this create --
            # fall back to the update path against whatever now exists,
            # exactly like the normal "already exists" path. Never a second
            # participant is created here.
            refreshed = await self._find_existing_participant(engagement.engagement_id, contact.crm_contact_id)
            if refreshed is None:
                raise  # genuinely unexpected -- the row the constraint just rejected against should be findable
            return await self._sync_against_existing(engagement, refreshed, contact, registration, derived_decline_origin)

        await self._record_activity(
            event_type="luma.engagement_participant.created",
            summary=f'"{_participant_display_name(participant)}" was added as a Participant from a Luma registration.',
            participant=participant,
            metadata={"engagement_id": engagement.engagement_id, "client_id": engagement.client_id},
        )
        await self._reconcile_engagement_signal(participant)
        return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.CREATED, participant=participant)

    async def _sync_against_existing(
        self,
        engagement: Engagement,
        existing: EngagementParticipant,
        contact: CrmContact,
        registration: LumaRegistration,
        derived_decline_origin: DeclineOrigin | None,
    ) -> LumaParticipantSyncResult:
        """Preserves EVERYTHING about an existing participant except the
        narrow set of Luma-owned fields below -- role, source, is_walk_in,
        attendance_status, archived, and every nonblank snapshot value are
        never touched, regardless of whether this participant was created
        MANUALLY or by an earlier Luma sync. A MANUAL participant is never
        converted to LUMA; a human-selected role (Host/Client/Speaker/
        Panelist/etc.) is never overwritten to Guest. Stage 5A:
        decline_origin joins this same "narrow set of Luma-owned fields"
        list, EXCEPT it has its own additional guard -- see
        _build_update_patch()'s own docstring -- a human-asserted
        decline_origin (existing.decline_origin_is_manual) is never
        overwritten by an automatic Luma derivation, on a manually-created
        participant or a Luma-created one alike."""
        if existing.archived:
            logger.info(
                f"Luma participant sync: participant {existing.participant_id} for Engagement "
                f"{engagement.engagement_id} is archived -- leaving it archived, not creating a duplicate."
            )
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.ARCHIVED_PARTICIPANT_SKIPPED, participant=existing)

        patch = self._build_update_patch(existing, contact, registration, derived_decline_origin)
        if not patch:
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.UNCHANGED, participant=existing)

        updated = existing.model_copy(update={**patch, "updated_at": datetime.now(timezone.utc)})
        try:
            await self.engagement_participant_store.save(updated)
        except EngagementParticipantDuplicateError as exc:
            # crm_contact_id never changes on this path, so this store-level
            # conflict should not be reachable here -- treated defensively,
            # never crashing the sync over it.
            logger.error(
                f"Luma participant sync: unexpected duplicate-constraint error updating participant "
                f"{existing.participant_id}: {exc}"
            )
            return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.UNCHANGED, participant=existing)

        await self._record_activity(
            event_type="luma.engagement_participant.updated",
            summary=f'"{_participant_display_name(updated)}" was updated from a Luma registration.',
            participant=updated,
            metadata={
                "engagement_id": engagement.engagement_id,
                "client_id": engagement.client_id,
                "fields_updated": sorted(patch.keys()),
            },
        )
        await self._reconcile_engagement_signal(updated)
        return LumaParticipantSyncResult(outcome=LumaParticipantSyncOutcome.UPDATED, participant=updated)

    @staticmethod
    def _build_update_patch(
        existing: EngagementParticipant,
        contact: CrmContact,
        registration: LumaRegistration,
        derived_decline_origin: DeclineOrigin | None,
    ) -> dict[str, Any]:
        """Snapshot fields: fill-only-if-blank, never overwrite a nonblank
        human-entered value. rsvp_status: only ever set when Luma's
        approval_status maps to a defined value AND that differs from the
        current value -- pending_approval/waitlist/session/unknown never
        appear in this patch at all, leaving rsvp_status completely
        untouched for those (Section G's own locked mapping). Never
        includes role/source/is_walk_in/attendance_status/archived --
        those are simply never candidates here.

        Stage 5A decline_origin/decline_origin_is_manual:
        - rsvp_status is CHANGING to something other than DECLINED (or was
          never DECLINED) -- both are cleared (None, False), per the
          model's own core invariant.
        - rsvp_status is CHANGING to DECLINED -- both are set from
          `derived_decline_origin` (always a real value here, never None,
          since the caller only computes a decline fact when the incoming
          approval_status actually IS declined) and decline_origin_is_manual
          becomes False -- an automatic Luma derivation is never "manual."
        - rsvp_status is NOT changing (already DECLINED, still DECLINED) --
          decline_origin is left alone UNLESS `derived_decline_origin` is
          non-None (a genuinely fresh transition was observed even though
          the mapped rsvp_status happens to be unchanged -- see
          LumaSyncService._derive_decline_origin()'s own docstring for when
          that can happen) AND the existing value isn't human-asserted
          (`existing.decline_origin_is_manual`) AND it would actually
          change something -- never touches it otherwise, so a routine
          duplicate/no-op delivery never manufactures a patch."""
        patch: dict[str, Any] = {}
        for field_name in _SNAPSHOT_FIELDS:
            if not getattr(existing, field_name) and getattr(contact, field_name):
                patch[field_name] = getattr(contact, field_name)

        mapped_rsvp = _map_rsvp_status(registration.approval_status)
        if mapped_rsvp is not None and existing.rsvp_status != mapped_rsvp:
            patch["rsvp_status"] = mapped_rsvp
            if mapped_rsvp == ParticipantRsvpStatus.DECLINED:
                patch["decline_origin"] = derived_decline_origin
                patch["decline_origin_is_manual"] = False
            else:
                patch["decline_origin"] = None
                patch["decline_origin_is_manual"] = False
        elif (
            mapped_rsvp == ParticipantRsvpStatus.DECLINED
            and existing.rsvp_status == ParticipantRsvpStatus.DECLINED
            and derived_decline_origin is not None
            and not existing.decline_origin_is_manual
            and existing.decline_origin != derived_decline_origin
        ):
            patch["decline_origin"] = derived_decline_origin

        return patch

    async def _reconcile_engagement_signal(self, participant: EngagementParticipant) -> None:
        """Contacts CRM Stage 3B. Invoked only after CREATED/UPDATED --
        the exact same two outcomes that get an Activity Log entry above,
        i.e. only after a genuinely material participant write. Never
        invoked merely because a Luma webhook was received or a
        registration was processed -- see this module's own "invoke on
        the resulting normalized EngagementParticipant state, not on
        webhook receipt" contract. A failure here is logged and
        swallowed, never re-raised -- it must never break Luma
        registration/participant processing or the webhook's HTTP success
        response, same boundary this whole sync path already draws around
        every one of its own operations (see
        sync_luma_registration_to_engagement_participant's own docstring)."""
        try:
            await self.contact_engagement_signal_service.reconcile_from_participant(participant)
        except Exception:
            logger.exception(
                f"ContactEngagementSignalService.reconcile_from_participant failed for participant "
                f"{participant.participant_id} -- the Luma participant sync itself already succeeded and is unaffected."
            )

    async def _record_activity(self, *, event_type: str, summary: str, participant: EngagementParticipant, metadata: dict[str, Any]) -> None:
        """Only ever called for CREATED/UPDATED -- every NO-OP outcome
        (unlinked event, archived/cancelled Engagement, unresolved
        registration, an unchanged/idempotent repeat sync, or an archived
        existing participant) deliberately records nothing here, matching
        the explicit "no activity noise for a no-op" requirement. Metadata
        is structural only (ids, field KEYS) -- never a raw Luma payload,
        never a snapshot field VALUE, same convention as every other
        Activity Log call in this Luma pipeline."""
        await self.activity_log.record(
            event_type=event_type,
            category=ActivityCategory.LUMA,
            source=ActivitySource.LUMA_SYNC,
            summary=summary,
            entity_type="engagement_participant",
            entity_id=participant.participant_id,
            entity_name=_participant_display_name(participant),
            metadata=metadata,
        )
