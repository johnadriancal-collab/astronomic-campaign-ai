"""
Contacts CRM Stage 3B (2026-09-11) -- the ONE canonical reconciliation
function that advances a CrmContact's custom_fields["engagement_stage"]
to "Interested" based on genuine positive interest demonstrated through
an EngagementParticipant. Deliberately the only place this logic exists
-- wired into ClientCrmService's manual create/update paths AND
LumaEngagementParticipantSyncService's create/update paths, never
duplicated in either. See reconcile_from_participant()'s own docstring
for the full contract.

Forward-only, advance-only: this NEVER downgrades and NEVER reverts on a
later negative signal (declined/cancelled/no-show/archived) -- once a
Contact demonstrated positive interest, that historical signal is
preserved regardless of what happens afterward. It also NEVER scans or
reconciles historical participants on its own; it only ever evaluates the
ONE EngagementParticipant a caller hands it, at the moment that caller's
own material write already succeeded. Stage 3C's own explicit, separate,
approved scope is the historical backfill for the existing production
cohort -- nothing here iterates any store, and nothing here runs at app
startup.

Positive-interest signal is derived ENTIRELY from EngagementParticipant's
own fields (rsvp_status/attendance_status/role) -- never from
ParticipantSource, and never by reaching back into raw Luma/registration
state. A manually-created participant and a Luma-created one in the same
state produce byte-identical outcomes here.

Provenance note (investigated before writing this file): neither
crm_migration.py's historical funding_stage-corruption repair nor
crm_classification_rules.py's CSV-import classify_engagement_stage() has
ever written a custom_fields["field_provenance"]["engagement_stage"]
entry -- both set only the bare engagement_stage value itself. So there
is, in practice, no pre-existing provenance for this field to preserve or
destroy; the entry _advance() writes below is the first one ever written
for engagement_stage. It follows the exact same
"copy the existing field_provenance dict, mutate just the one key,
merge back into custom_fields" pattern already established by
luma_contact_enrichment.py's own Luma-self-report merge function -- see
FIELD_PROVENANCE_KEY there.

Stage 3C addendum (2026-09-11): is_qualifying_participant() and
positive_signal_type() are exposed as public, stateless staticmethods
specifically so the historical reconciliation driver
(app/services/contact_engagement_stage_backfill.py) can select WHICH of a
Contact's several qualifying participants should be the provenance
trigger, and can filter a large historical participant set down to
"would reconcile_from_participant() even consider this one," WITHOUT
reimplementing the archived/role/signal-type rules a second time.
reconcile_from_participant() itself is refactored to call these same two
methods internally (not a parallel, differently-behaved copy) -- see each
method's own docstring. _ADVANCEABLE_CURRENT_STAGES and
INTERESTED_STAGE_VALUE are likewise imported directly by that driver
rather than redefined there.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.models.activity import ActivityCategory, ActivitySource
from app.models.client_crm import EngagementParticipant, ParticipantAttendanceStatus, ParticipantRole, ParticipantRsvpStatus
from app.models.crm import CrmContact
from app.repositories.crm_contact_store import CrmContactStore
from app.services.activity_log_service import ActivityLogService

FIELD_PROVENANCE_KEY = "field_provenance"
ENGAGEMENT_STAGE_KEY = "engagement_stage"
INTERESTED_STAGE_VALUE = "Interested"

# Locked Stage 3B product decision -- a role outside this set is NEVER a
# positive-interest trigger, regardless of RSVP/attendance, even if OTHER
# is later explicitly approved for inclusion. CLIENT/HOST/ASTRONOMIC_TEAM
# are excluded deliberately: a Client's own staff or Astronomic's own team
# attending their own dinner is operational attendance, not prospect
# interest, and writing engagement_stage for them would be a category
# error regardless of their RSVP state.
_ELIGIBLE_ROLES = frozenset({ParticipantRole.GUEST, ParticipantRole.SPEAKER_PANELIST})

# Only a Contact currently at one of these (or unset/blank) values is
# eligible to advance -- every other value, INCLUDING any value this app
# doesn't even recognize, is left completely untouched. This is the
# smallest-safe rule the Stage 3B investigation itself recommended: it
# satisfies "advance from earlier, no-op if already Interested, never
# downgrade if later" WITHOUT inventing a fragile total ordering over
# stage names whose relative "advancement" (e.g. Replied vs Unresponsive)
# isn't documented or agreed anywhere.
_ADVANCEABLE_CURRENT_STAGES = frozenset({None, "", "Cold"})


class ContactEngagementSignalOutcome(str, Enum):
    """Every possible result of one reconcile_from_participant() call --
    deliberately granular so callers/tests can assert exactly which rule
    fired, and so Activity Log noise stays limited to exactly ADVANCED
    (see ContactEngagementSignalService._advance's own docstring)."""

    ADVANCED = "advanced"
    NO_OP_ALREADY_INTERESTED = "no_op_already_interested"
    NO_OP_STAGE_NOT_ADVANCEABLE = "no_op_stage_not_advanceable"
    SKIPPED_ARCHIVED_PARTICIPANT = "skipped_archived_participant"
    SKIPPED_NO_CONTACT_LINK = "skipped_no_contact_link"
    SKIPPED_INELIGIBLE_ROLE = "skipped_ineligible_role"
    SKIPPED_NO_POSITIVE_SIGNAL = "skipped_no_positive_signal"
    SKIPPED_CONTACT_NOT_FOUND = "skipped_contact_not_found"


@dataclass
class ContactEngagementSignalResult:
    outcome: ContactEngagementSignalOutcome
    reason: str


class ContactEngagementSignalService:
    def __init__(self, crm_contact_store: CrmContactStore, activity_log: ActivityLogService):
        self.crm_contact_store = crm_contact_store
        self.activity_log = activity_log

    async def reconcile_from_participant(self, participant: EngagementParticipant) -> ContactEngagementSignalResult:
        """The one entry point. Every ineligibility/no-op case below is a
        plain result, never an exception -- an archived participant, an
        unresolved (no crm_contact_id) participant, an ineligible role, no
        positive signal, a missing Contact, or a Contact whose current
        stage isn't in the advanceable set are all normal, expected,
        non-error outcomes, exactly mirroring the "plain NO-OP, never an
        exception" convention LumaEngagementParticipantSyncService's own
        eligibility chain already established. Callers invoke this AFTER
        their own material EngagementParticipant write already succeeded,
        and are expected to wrap this call in their own try/except so a
        genuinely unexpected failure here (e.g. a store error) can never
        undo or block that already-committed write -- see each caller's
        own docstring for its specific error-boundary choice."""
        if participant.archived:
            return ContactEngagementSignalResult(ContactEngagementSignalOutcome.SKIPPED_ARCHIVED_PARTICIPANT, "participant is archived")
        if participant.crm_contact_id is None:
            return ContactEngagementSignalResult(ContactEngagementSignalOutcome.SKIPPED_NO_CONTACT_LINK, "participant has no crm_contact_id")
        if not self._is_eligible_role(participant.role):
            return ContactEngagementSignalResult(
                ContactEngagementSignalOutcome.SKIPPED_INELIGIBLE_ROLE, f"role {participant.role.value} is not eligible"
            )

        signal_type = self.positive_signal_type(participant)
        if signal_type is None:
            return ContactEngagementSignalResult(ContactEngagementSignalOutcome.SKIPPED_NO_POSITIVE_SIGNAL, "no positive RSVP/attendance signal")

        contact = await self.crm_contact_store.get(participant.crm_contact_id)
        if contact is None:
            return ContactEngagementSignalResult(ContactEngagementSignalOutcome.SKIPPED_CONTACT_NOT_FOUND, "linked Contact no longer exists")

        current_stage = (contact.custom_fields or {}).get(ENGAGEMENT_STAGE_KEY)
        if current_stage == INTERESTED_STAGE_VALUE:
            return ContactEngagementSignalResult(ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED, "already Interested")
        if current_stage not in _ADVANCEABLE_CURRENT_STAGES:
            return ContactEngagementSignalResult(
                ContactEngagementSignalOutcome.NO_OP_STAGE_NOT_ADVANCEABLE, f"current stage {current_stage!r} is not advanceable"
            )

        await self._advance(contact, participant, signal_type, current_stage)
        return ContactEngagementSignalResult(ContactEngagementSignalOutcome.ADVANCED, f"advanced via {signal_type}")

    @staticmethod
    def _is_eligible_role(role: ParticipantRole) -> bool:
        return role in _ELIGIBLE_ROLES

    @staticmethod
    def positive_signal_type(participant: EngagementParticipant) -> str | None:
        """Public, stateless -- pure function of the participant's own
        rsvp_status/attendance_status, reused directly by
        contact_engagement_stage_backfill.py for its own deterministic
        trigger-participant precedence (ATTENDED-signal participants
        outrank CONFIRMED-only ones there too, using this exact same
        string values). Deterministic when both are true --
        attendance_attended wins over rsvp_confirmed (locked product
        decision: someone who actually showed up is a stronger signal
        than someone who merely confirmed). Neither present -- invited,
        declined, no_show, cancelled, or entirely null -- returns None
        (not a positive signal)."""
        if participant.attendance_status == ParticipantAttendanceStatus.ATTENDED:
            return "attendance_attended"
        if participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED:
            return "rsvp_confirmed"
        return None

    @staticmethod
    def is_qualifying_participant(participant: EngagementParticipant) -> bool:
        """Public, stateless -- "would reconcile_from_participant()
        consider this participant at all" (archived + crm_contact_id +
        role + positive-signal, i.e. everything EXCEPT the Contact's own
        current stage, which this can't know without a store read).
        Exists so contact_engagement_stage_backfill.py can filter a whole
        historical participant set down to qualifying candidates without
        re-deriving any of these rules itself -- see this module's own
        Stage 3C addendum above."""
        if participant.archived:
            return False
        if participant.crm_contact_id is None:
            return False
        if not ContactEngagementSignalService._is_eligible_role(participant.role):
            return False
        return ContactEngagementSignalService.positive_signal_type(participant) is not None

    async def _advance(
        self, contact: CrmContact, participant: EngagementParticipant, signal_type: str, from_stage: Any
    ) -> None:
        """Writes directly via crm_contact_store.save() -- same
        "compute the merged Contact, persist directly, record ONE specific
        Activity event" pattern already used by the caller of
        luma_contact_enrichment.py's own Luma-self-report merge function,
        deliberately NOT routed through
        CrmService.update_contact() (which would additionally fire its own
        generic "contact.updated" Activity event -- exactly one Activity
        entry per advancement is the locked requirement, not two).
        custom_fields is shallow-copied and only its two relevant keys
        (engagement_stage, field_provenance) are touched -- every sibling
        custom field, and every sibling field_provenance entry, is
        preserved untouched."""
        now = datetime.now(timezone.utc)
        provenance = dict((contact.custom_fields or {}).get(FIELD_PROVENANCE_KEY) or {})
        provenance[ENGAGEMENT_STAGE_KEY] = {
            "source": "engagement_participation",
            "engagement_id": participant.engagement_id,
            "participant_id": participant.participant_id,
            "signal_type": signal_type,
            "advanced_at": now.isoformat(),
        }
        updated_contact = contact.model_copy(
            update={
                "custom_fields": {
                    **(contact.custom_fields or {}),
                    ENGAGEMENT_STAGE_KEY: INTERESTED_STAGE_VALUE,
                    FIELD_PROVENANCE_KEY: provenance,
                },
                "updated_at": now,
            }
        )
        await self.crm_contact_store.save(updated_contact)
        await self.activity_log.record(
            event_type="contact.engagement_stage.advanced",
            category=ActivityCategory.CONTACTS,
            source=ActivitySource.ENGAGEMENT_SIGNAL,
            summary="A Contact's Engagement Stage automatically advanced to Interested.",
            entity_type="contact",
            entity_id=contact.crm_contact_id,
            entity_name=None,
            metadata={
                "crm_contact_id": contact.crm_contact_id,
                "from_stage": from_stage,
                "to_stage": INTERESTED_STAGE_VALUE,
                "engagement_id": participant.engagement_id,
                "participant_id": participant.participant_id,
                "signal_type": signal_type,
            },
        )
