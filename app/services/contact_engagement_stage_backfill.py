"""
Contacts CRM Stage 3C (2026-09-11) -- controlled, dry-run-first historical
reconciliation of EXISTING EngagementParticipants against
ContactEngagementSignalService's own canonical Stage 3B rules. Reuses that
service's is_qualifying_participant()/positive_signal_type() staticmethods
and its own _ADVANCEABLE_CURRENT_STAGES/INTERESTED_STAGE_VALUE constants
directly -- no separate role/signal/stage-eligibility rule is redefined
here, by construction, preventing semantic drift between Stage 3B's live
forward path and this historical one.

Two independent operations:

compute_historical_reconciliation_report() -- READ-ONLY. Scans every
current EngagementParticipant across every Engagement/Client (a small,
bounded scan at this app's actual scale -- see its own docstring),
classifies every Contact with at least one qualifying participant into
one of six buckets (already Interested / eligible unset / eligible Cold /
protected Replied / protected Unresponsive / protected unexpected), and
picks ONE deterministic "trigger" participant per Contact using the
precedence documented on select_trigger_participant(). Writes NOTHING --
no Contact/EngagementParticipant/Activity mutation, ever, structurally
(it never even touches crm_contact_store.save() or
engagement_participant_store.save()/create()).

apply_historical_reconciliation() -- the WRITE path. Takes an EXPLICIT,
already-deduplicated list of crm_contact_ids -- never "everyone the dry
run found" (see scripts/run_contact_engagement_stage_backfill.py's own
two-gate CLI, which additionally requires an explicit target list on top
of --write/--confirm-production-writes). For EACH id, independently:
re-reads that Contact's CURRENT EngagementParticipants fresh (never
trusting a frozen dry-run manifest), re-selects the current qualifying/
trigger participant using the SAME precedence, and calls
ContactEngagementSignalService.reconcile_from_participant() on it -- so a
Contact that already advanced via a live Stage 3B event, or was manually
moved to a protected stage, or whose only qualifying participant became
non-positive, since the dry run was generated, is correctly a no-op here,
never a stale replay of frozen data. One Contact's failure is caught and
reported; it never stops the rest of the batch.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from app.models.client_crm import Client, Engagement, EngagementParticipant
from app.models.crm import CrmContact
from app.repositories.client_store import ClientStore
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.engagement_participant_store import EngagementParticipantStore
from app.repositories.engagement_store import EngagementStore
from app.services.contact_engagement_signal_service import (
    _ADVANCEABLE_CURRENT_STAGES,  # the SAME set Stage 3B itself uses -- imported, never redefined
    INTERESTED_STAGE_VALUE,
    ContactEngagementSignalOutcome,
    ContactEngagementSignalService,
)


class ReconciliationBucket:
    ALREADY_INTERESTED = "already_interested"
    ELIGIBLE_UNSET = "eligible_unset"
    ELIGIBLE_COLD = "eligible_cold"
    PROTECTED_REPLIED = "protected_replied"
    PROTECTED_UNRESPONSIVE = "protected_unresponsive"
    PROTECTED_UNEXPECTED = "protected_unexpected"


_WRITE_CANDIDATE_BUCKETS = frozenset({ReconciliationBucket.ELIGIBLE_UNSET, ReconciliationBucket.ELIGIBLE_COLD})


def _classify_current_stage(current_stage: str | None) -> str:
    """The ADVANCE-vs-PROTECT decision itself flows entirely from the
    imported _ADVANCEABLE_CURRENT_STAGES/INTERESTED_STAGE_VALUE (Stage
    3B's own constants, not a local copy) -- the finer Replied/
    Unresponsive/"something else" split below is purely for Stage 3C's
    own human-readable reporting, and can never disagree with Stage 3B
    about WHETHER a stage is advanceable, only about which protected
    label to print."""
    if current_stage == INTERESTED_STAGE_VALUE:
        return ReconciliationBucket.ALREADY_INTERESTED
    if current_stage in _ADVANCEABLE_CURRENT_STAGES:
        return ReconciliationBucket.ELIGIBLE_COLD if current_stage == "Cold" else ReconciliationBucket.ELIGIBLE_UNSET
    if current_stage == "Replied":
        return ReconciliationBucket.PROTECTED_REPLIED
    if current_stage == "Unresponsive":
        return ReconciliationBucket.PROTECTED_UNRESPONSIVE
    return ReconciliationBucket.PROTECTED_UNEXPECTED


def select_trigger_participant(
    qualifying_participants: list[EngagementParticipant], engagement_dates: dict[str, date | None]
) -> EngagementParticipant:
    """Deterministic precedence, locked: (1) ATTENDED-signal participants
    outrank CONFIRMED-only ones; (2) among equal signal strength, the
    MOST RECENT Engagement date wins; (3) then the most recently updated
    participant row; (4) then participant_id ascending as a final, stable
    tie-break. A qualifying participant whose own Engagement has no
    engagement_date set (a PLANNED Engagement with no date locked in yet)
    is treated as LOWEST recency priority, not highest or an error --
    this is the one judgment call beyond your explicit rule, called out
    here and in the accompanying report rather than assumed silently."""
    def sort_key(participant: EngagementParticipant) -> tuple:
        signal_type = ContactEngagementSignalService.positive_signal_type(participant)
        signal_rank = 0 if signal_type == "attendance_attended" else 1
        event_date = engagement_dates.get(participant.engagement_id)
        date_component = -(event_date.toordinal()) if event_date is not None else 0
        return (signal_rank, date_component, -participant.updated_at.timestamp(), participant.participant_id)

    return min(qualifying_participants, key=sort_key)


@dataclass
class HistoricalReconciliationRow:
    crm_contact_id: str
    contact_name: str
    current_stage: str | None
    proposed_stage: str | None  # "Interested" only for write-candidate buckets, else None
    bucket: str
    trigger_participant_id: str
    trigger_engagement_id: str
    client_name: str
    event_name: str
    event_date: date | None
    role: str
    rsvp_status: str | None
    attendance_status: str | None
    source: str
    signal_type: str
    participant_updated_at: datetime
    contact_updated_at: datetime
    qualifying_participant_count: int


@dataclass
class HistoricalReconciliationCounts:
    total_active_participants: int = 0
    total_positive_signal_contacts: int = 0
    already_interested: int = 0
    eligible_unset: int = 0
    eligible_cold: int = 0
    protected_replied: int = 0
    protected_unresponsive: int = 0
    protected_unexpected: int = 0
    proposed_write_count: int = 0
    signal_rsvp_confirmed: int = 0
    signal_attendance_attended: int = 0
    source_manual: int = 0
    source_luma: int = 0
    multi_signal_contacts: int = 0


@dataclass
class HistoricalReconciliationReport:
    counts: HistoricalReconciliationCounts
    rows: list[HistoricalReconciliationRow] = field(default_factory=list)

    @property
    def write_candidates(self) -> list[HistoricalReconciliationRow]:
        return [r for r in self.rows if r.bucket in _WRITE_CANDIDATE_BUCKETS]

    @property
    def protected(self) -> list[HistoricalReconciliationRow]:
        return [r for r in self.rows if r.bucket.startswith("protected_")]


async def _list_all_engagements(client_store: ClientStore, engagement_store: EngagementStore) -> list[Engagement]:
    """Every Engagement in the system -- composed entirely from EXISTING
    bulk methods (ClientStore.list() + EngagementStore.list_for_clients(),
    both already used elsewhere, e.g. ClientCrmService.list_clients()'s
    own Next Dinner derivation), so no new store method was needed for
    this. Fine at this app's scale (a handful of Clients/Engagements
    total, the same "Python-side full scan" convention already
    established across this codebase)."""
    clients = await client_store.list()
    return await engagement_store.list_for_clients([c.client_id for c in clients])


async def compute_historical_reconciliation_report(
    client_store: ClientStore,
    engagement_store: EngagementStore,
    engagement_participant_store: EngagementParticipantStore,
    crm_contact_store: CrmContactStore,
) -> HistoricalReconciliationReport:
    """READ-ONLY. See this module's own docstring for the full contract."""
    engagements = await _list_all_engagements(client_store, engagement_store)
    engagement_by_id: dict[str, Engagement] = {e.engagement_id: e for e in engagements}
    engagement_dates: dict[str, date | None] = {e.engagement_id: e.engagement_date for e in engagements}

    clients = await client_store.list()
    client_by_id: dict[str, Client] = {c.client_id: c for c in clients}

    all_participants: list[EngagementParticipant] = []
    for engagement in engagements:
        all_participants.extend(await engagement_participant_store.list_for_engagement(engagement.engagement_id))
    active_participants = [p for p in all_participants if not p.archived]

    qualifying_by_contact: dict[str, list[EngagementParticipant]] = {}
    for participant in active_participants:
        if ContactEngagementSignalService.is_qualifying_participant(participant):
            qualifying_by_contact.setdefault(participant.crm_contact_id, []).append(participant)

    counts = HistoricalReconciliationCounts(
        total_active_participants=len(active_participants),
        total_positive_signal_contacts=len(qualifying_by_contact),
    )
    rows: list[HistoricalReconciliationRow] = []

    for crm_contact_id, participants in qualifying_by_contact.items():
        contact = await crm_contact_store.get(crm_contact_id)
        if contact is None:
            # A qualifying participant whose linked Contact no longer
            # exists -- same "fail safe, skip, never crash the whole
            # scan" convention as ContactEngagementSignalService's own
            # SKIPPED_CONTACT_NOT_FOUND. Not counted into any bucket
            # (there is no Contact to classify).
            continue

        current_stage = (contact.custom_fields or {}).get("engagement_stage")
        bucket = _classify_current_stage(current_stage)

        trigger = select_trigger_participant(participants, engagement_dates)
        signal_type = ContactEngagementSignalService.positive_signal_type(trigger)
        engagement = engagement_by_id.get(trigger.engagement_id)
        client = client_by_id.get(engagement.client_id) if engagement else None

        rows.append(
            HistoricalReconciliationRow(
                crm_contact_id=crm_contact_id,
                contact_name=" ".join(part for part in (contact.first_name, contact.last_name) if part) or "(unnamed)",
                current_stage=current_stage,
                proposed_stage=INTERESTED_STAGE_VALUE if bucket in _WRITE_CANDIDATE_BUCKETS else None,
                bucket=bucket,
                trigger_participant_id=trigger.participant_id,
                trigger_engagement_id=trigger.engagement_id,
                client_name=client.name if client else "(unknown Client)",
                event_name=engagement.title if engagement else "(unknown Engagement)",
                event_date=engagement.engagement_date if engagement else None,
                role=trigger.role.value,
                rsvp_status=trigger.rsvp_status.value if trigger.rsvp_status else None,
                attendance_status=trigger.attendance_status.value if trigger.attendance_status else None,
                source=trigger.source.value,
                signal_type=signal_type,
                participant_updated_at=trigger.updated_at,
                contact_updated_at=contact.updated_at,
                qualifying_participant_count=len(participants),
            )
        )

        if bucket == ReconciliationBucket.ALREADY_INTERESTED:
            counts.already_interested += 1
        elif bucket == ReconciliationBucket.ELIGIBLE_UNSET:
            counts.eligible_unset += 1
            counts.proposed_write_count += 1
        elif bucket == ReconciliationBucket.ELIGIBLE_COLD:
            counts.eligible_cold += 1
            counts.proposed_write_count += 1
        elif bucket == ReconciliationBucket.PROTECTED_REPLIED:
            counts.protected_replied += 1
        elif bucket == ReconciliationBucket.PROTECTED_UNRESPONSIVE:
            counts.protected_unresponsive += 1
        else:
            counts.protected_unexpected += 1

        if signal_type == "attendance_attended":
            counts.signal_attendance_attended += 1
        else:
            counts.signal_rsvp_confirmed += 1
        if trigger.source.value == "manual":
            counts.source_manual += 1
        else:
            counts.source_luma += 1
        if len(participants) > 1:
            counts.multi_signal_contacts += 1

    rows.sort(key=lambda r: (r.bucket, r.contact_name, r.crm_contact_id))
    return HistoricalReconciliationReport(counts=counts, rows=rows)


class HistoricalWriteOutcome:
    ADVANCED = "advanced"
    ALREADY_INTERESTED = "already_interested"
    STAGE_NOT_ADVANCEABLE = "stage_not_advanceable"
    NO_LONGER_POSITIVE = "no_longer_positive"
    MISSING_CONTACT = "missing_contact"
    MISSING_PARTICIPANT = "missing_participant"
    FAILED = "failed"


@dataclass
class HistoricalWriteResult:
    crm_contact_id: str
    outcome: str
    detail: str


@dataclass
class HistoricalWriteReport:
    results: list[HistoricalWriteResult] = field(default_factory=list)
    duplicate_ids_deduped: int = 0
    unknown_ids: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts


_OUTCOME_MAP = {
    ContactEngagementSignalOutcome.ADVANCED: HistoricalWriteOutcome.ADVANCED,
    ContactEngagementSignalOutcome.NO_OP_ALREADY_INTERESTED: HistoricalWriteOutcome.ALREADY_INTERESTED,
    ContactEngagementSignalOutcome.NO_OP_STAGE_NOT_ADVANCEABLE: HistoricalWriteOutcome.STAGE_NOT_ADVANCEABLE,
}


async def apply_historical_reconciliation(
    crm_contact_ids: list[str],
    engagement_store: EngagementStore,
    engagement_participant_store: EngagementParticipantStore,
    crm_contact_store: CrmContactStore,
    contact_engagement_signal_service: ContactEngagementSignalService,
) -> HistoricalWriteReport:
    """The WRITE path -- see this module's own docstring for the full
    re-evaluate-live contract. `crm_contact_ids` must already be the
    explicit, human-approved target set (deduping/unknown-id handling
    happens here defensively too, but the CALLER -- see
    scripts/run_contact_engagement_stage_backfill.py -- is what refuses
    to run at all without an explicit target)."""
    report = HistoricalWriteReport()
    seen: set[str] = set()
    ordered_unique_ids: list[str] = []
    for crm_contact_id in crm_contact_ids:
        if crm_contact_id in seen:
            report.duplicate_ids_deduped += 1
            continue
        seen.add(crm_contact_id)
        ordered_unique_ids.append(crm_contact_id)

    for crm_contact_id in ordered_unique_ids:
        try:
            contact = await crm_contact_store.get(crm_contact_id)
            if contact is None:
                report.unknown_ids.append(crm_contact_id)
                report.results.append(
                    HistoricalWriteResult(crm_contact_id, HistoricalWriteOutcome.MISSING_CONTACT, "no such Contact exists")
                )
                continue

            participants = await engagement_participant_store.list_for_contact(crm_contact_id)
            if not participants:
                report.results.append(
                    HistoricalWriteResult(crm_contact_id, HistoricalWriteOutcome.MISSING_PARTICIPANT, "Contact has no EngagementParticipant rows at all")
                )
                continue

            active = [p for p in participants if not p.archived]
            qualifying = [p for p in active if ContactEngagementSignalService.is_qualifying_participant(p)]
            if not qualifying:
                report.results.append(
                    HistoricalWriteResult(
                        crm_contact_id, HistoricalWriteOutcome.NO_LONGER_POSITIVE,
                        "no currently-qualifying participant remains (re-evaluated live, not from the frozen dry-run manifest)",
                    )
                )
                continue

            engagement_ids = {p.engagement_id for p in qualifying}
            engagements = await engagement_store.list_by_ids(list(engagement_ids))
            engagement_dates = {e.engagement_id: e.engagement_date for e in engagements}
            trigger = select_trigger_participant(qualifying, engagement_dates)

            result = await contact_engagement_signal_service.reconcile_from_participant(trigger)
            outcome = _OUTCOME_MAP.get(result.outcome)
            if outcome is None:
                # SKIPPED_ARCHIVED_PARTICIPANT/SKIPPED_NO_CONTACT_LINK/SKIPPED_INELIGIBLE_ROLE/
                # SKIPPED_NO_POSITIVE_SIGNAL/SKIPPED_CONTACT_NOT_FOUND should be structurally
                # unreachable here (the trigger was JUST selected as qualifying, against a
                # Contact JUST confirmed to exist) -- treated defensively as a no-op-shaped
                # report line rather than crashing the batch if it ever somehow occurs.
                outcome = HistoricalWriteOutcome.NO_LONGER_POSITIVE
            report.results.append(HistoricalWriteResult(crm_contact_id, outcome, result.reason))
        except Exception as exc:  # noqa: BLE001 -- one Contact's failure must never stop the batch
            report.results.append(HistoricalWriteResult(crm_contact_id, HistoricalWriteOutcome.FAILED, f"{type(exc).__name__}: {exc}"))

    return report
