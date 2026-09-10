"""
Client CRM Stage 1H-C -- historical, dry-run-capable driver for the
Luma-registration -> EngagementParticipant sync (see
app/services/luma_engagement_participant_sync_service.py for the shared
eligibility/create/update/RSVP rules this reuses verbatim -- no separate
backfill algorithm exists here, only orchestration + counting, exactly
mirroring luma_contact_enrichment_backfill.py's own "orchestration only"
precedent).

Scope, deliberately narrow (Stage 1H-C's own approved design): targets
EXACTLY ONE explicitly-supplied engagement_id per call. There is no
"process every linked Engagement" mode, no automatic discovery of newly-
linked Engagements, and nothing here runs at startup or on a schedule --
see scripts/run_luma_engagement_participant_backfill.py's own docstring
for the two-gate (--write AND --confirm-production-writes) operator
safety this module is driven by. Rejects (raises
ParticipantBackfillInvalidTarget) BEFORE reading or writing anything else
when the given engagement_id doesn't exist or has no linked Luma event --
an archived/cancelled-but-LINKED Engagement is NOT rejected here; each of
its registrations instead reports the exact same ENGAGEMENT_ARCHIVED/
ENGAGEMENT_CANCELLED outcome LumaEngagementParticipantSyncService already
defines, requiring no special-casing in this module at all.

DRY RUN (the default): computes the exact same per-registration outcome a
live write would, by running the REAL, UNMODIFIED
LumaEngagementParticipantSyncService against a throwaway in-memory
REPLICA -- seeded from this one Engagement's own current record, its
CURRENT EngagementParticipants, and only the CrmContacts these
registrations actually reference (never the whole Contact table). This is
"explicit simulation," not "call the write path and roll back": the real
engagement_store/engagement_participant_store/crm_contact_store passed
into this function are touched ONLY via read-only get()/list_for_*() calls
in dry-run mode -- their own create()/save() are never invoked. A fresh,
also-thrown-away ActivityLogService/MemoryActivityEventStore backs the
replica too, so a dry run is structurally incapable of writing a real
Activity Log entry.

WRITE mode (dry_run=False): the SAME iteration, SAME per-registration call
to sync_luma_registration_to_engagement_participant(), against the REAL
stores/activity log passed in instead of a replica. This is the only
difference between the two modes -- proving (and testing) "dry-run
predicts write" is exactly a matter of running both against identically-
seeded stores and comparing results.

Deterministic order: registrations for one Luma event are always
processed oldest-`registered_at`-first (a registration with no
registered_at at all sorts last, never assumed to be "first") --
identical in both modes, and independent of whatever order the underlying
store happens to return them in. This is what makes "two LumaRegistration
rows for the same Contact + Engagement" resolve deterministically: the
earlier one is always evaluated before the later one, exactly as if they
had arrived live in that order.

One bad registration never aborts the run: any unexpected exception raised
while processing a single registration is caught, counted in
`counts.errors`, and recorded as one short, PII-free diagnostic string
(luma_guest_id + exception type/message only -- never a raw payload) in
`report.errors`; every other registration in the cohort still gets its own
outcome. A report with `errors > 0` is never silently indistinguishable
from a clean run -- callers must check `counts.errors` themselves (the CLI
script surfaces it plainly, see that script's own _print_report()).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models.luma import LumaMatchStatus, LumaRegistration
from app.repositories.crm_contact_store import CrmContactStore, MemoryCrmContactStore
from app.repositories.engagement_participant_store import EngagementParticipantStore, MemoryEngagementParticipantStore
from app.repositories.engagement_store import EngagementStore, MemoryEngagementStore
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.luma_registration_store import LumaRegistrationStore
from app.services.activity_log_service import ActivityLogService
from app.services.luma_engagement_participant_sync_service import (
    LumaEngagementParticipantSyncService,
    LumaParticipantSyncOutcome,
)

_OUTCOME_TO_COUNTS_FIELD: dict[LumaParticipantSyncOutcome, str] = {
    LumaParticipantSyncOutcome.CREATED: "created",
    LumaParticipantSyncOutcome.UPDATED: "updated",
    LumaParticipantSyncOutcome.UNCHANGED: "unchanged",
    LumaParticipantSyncOutcome.NO_ENGAGEMENT_LINKED: "skipped_no_engagement_linked",
    LumaParticipantSyncOutcome.ENGAGEMENT_ARCHIVED: "skipped_engagement_archived",
    LumaParticipantSyncOutcome.ENGAGEMENT_CANCELLED: "skipped_engagement_cancelled",
    LumaParticipantSyncOutcome.NOT_MATCHED: "skipped_not_matched",
    LumaParticipantSyncOutcome.NO_CONTACT: "skipped_no_contact",
    LumaParticipantSyncOutcome.ARCHIVED_PARTICIPANT_SKIPPED: "skipped_archived_participant",
}

# Never assumed to be "first" -- a registration with no registered_at at
# all always sorts LAST, tz-aware so it compares against real (aware)
# timestamps without raising.
_NEVER_REGISTERED = datetime.max.replace(tzinfo=timezone.utc)


class ParticipantBackfillInvalidTarget(Exception):
    """Raised when the given engagement_id doesn't exist, or exists but has
    no linked Luma event (luma_event_id is null) -- Stage 1H-C's own
    "reject invalid/unlinked targets safely" requirement. Nothing else is
    read or written when this is raised."""

    def __init__(self, engagement_id: str, reason: str):
        self.engagement_id = engagement_id
        self.reason = reason
        super().__init__(f"engagement_id {engagement_id!r} is not a valid backfill target: {reason}")


@dataclass
class ParticipantBackfillCounts:
    registrations_examined: int = 0
    unique_matched_contacts_referenced: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_no_engagement_linked: int = 0
    skipped_engagement_archived: int = 0
    skipped_engagement_cancelled: int = 0
    skipped_not_matched: int = 0
    skipped_no_contact: int = 0
    skipped_archived_participant: int = 0
    errors: int = 0
    participants_before: int = 0
    participants_after: int = 0


@dataclass
class ParticipantBackfillReport:
    engagement_id: str
    luma_event_id: str
    dry_run: bool
    counts: ParticipantBackfillCounts = field(default_factory=ParticipantBackfillCounts)
    # Sizes only (e.g. [3, 2]) for every crm_contact_id with MORE THAN ONE
    # matched registration for this event -- never the contact id itself.
    # See module docstring's "never expose unnecessary PII" convention,
    # already established across every prior Client CRM/Luma stage.
    duplicate_contact_group_sizes: list[int] = field(default_factory=list)
    # One short, PII-free diagnostic string per registration whose own
    # sync call raised -- luma_guest_id + exception type/message only.
    errors: list[str] = field(default_factory=list)


def _registration_sort_key(registration: LumaRegistration) -> datetime:
    return registration.registered_at or _NEVER_REGISTERED


async def _build_dry_run_replica(
    engagement_id: str,
    luma_event_id: str,
    engagement_store: EngagementStore,
    engagement_participant_store: EngagementParticipantStore,
    crm_contact_store: CrmContactStore,
    registrations: list[LumaRegistration],
) -> tuple[LumaEngagementParticipantSyncService, EngagementParticipantStore]:
    """Seeds fresh, in-process Memory stores from exactly the real data this
    one backfill run needs -- the Engagement itself, its CURRENT
    participants, and only the Contacts these registrations reference --
    then wires the real, unmodified LumaEngagementParticipantSyncService to
    that replica. Every read below is read-only against the REAL stores;
    every write from here on lands only in the replica."""
    real_engagement = await engagement_store.get(engagement_id)
    replica_engagement_store = MemoryEngagementStore()
    await replica_engagement_store.create(real_engagement)

    replica_participant_store = MemoryEngagementParticipantStore()
    for participant in await engagement_participant_store.list_for_engagement(engagement_id):
        await replica_participant_store.create(participant)

    replica_contact_store = MemoryCrmContactStore()
    seen_contact_ids: set[str] = set()
    for registration in registrations:
        if registration.crm_contact_id and registration.crm_contact_id not in seen_contact_ids:
            seen_contact_ids.add(registration.crm_contact_id)
            contact = await crm_contact_store.get(registration.crm_contact_id)
            if contact is not None:
                await replica_contact_store.create(contact)

    # Deliberately thrown away -- proves a dry run cannot produce a real
    # Activity Log entry, structurally, not just by convention.
    replica_activity_log = ActivityLogService(MemoryActivityEventStore())

    sync_service = LumaEngagementParticipantSyncService(
        engagement_store=replica_engagement_store,
        engagement_participant_store=replica_participant_store,
        crm_contact_store=replica_contact_store,
        activity_log=replica_activity_log,
    )
    return sync_service, replica_participant_store


async def run_luma_engagement_participant_backfill(
    engagement_store: EngagementStore,
    engagement_participant_store: EngagementParticipantStore,
    crm_contact_store: CrmContactStore,
    luma_registration_store: LumaRegistrationStore,
    activity_log: ActivityLogService,
    *,
    engagement_id: str,
    dry_run: bool = True,
) -> ParticipantBackfillReport:
    """The one entry point, for both the CLI script above and its own
    tests. `activity_log` is used ONLY when dry_run=False -- a dry run
    builds and discards its own isolated ActivityLogService instead (see
    _build_dry_run_replica), so passing a real one here has zero effect
    while dry_run=True."""
    engagement = await engagement_store.get(engagement_id)
    if engagement is None:
        raise ParticipantBackfillInvalidTarget(engagement_id, "no Engagement exists with this id.")
    if not engagement.luma_event_id:
        raise ParticipantBackfillInvalidTarget(engagement_id, "this Engagement has no linked Luma event (luma_event_id is null).")
    luma_event_id = engagement.luma_event_id

    registrations = sorted(await luma_registration_store.list_for_event(luma_event_id), key=_registration_sort_key)

    counts = ParticipantBackfillCounts(registrations_examined=len(registrations))
    report = ParticipantBackfillReport(engagement_id=engagement_id, luma_event_id=luma_event_id, dry_run=dry_run, counts=counts)

    contact_registration_counts: dict[str, int] = defaultdict(int)
    for registration in registrations:
        if registration.match_status == LumaMatchStatus.MATCHED and registration.crm_contact_id:
            contact_registration_counts[registration.crm_contact_id] += 1
    counts.unique_matched_contacts_referenced = len(contact_registration_counts)
    report.duplicate_contact_group_sizes = sorted(n for n in contact_registration_counts.values() if n > 1)

    if dry_run:
        sync_service, counting_participant_store = await _build_dry_run_replica(
            engagement_id, luma_event_id, engagement_store, engagement_participant_store, crm_contact_store, registrations
        )
    else:
        sync_service = LumaEngagementParticipantSyncService(
            engagement_store=engagement_store,
            engagement_participant_store=engagement_participant_store,
            crm_contact_store=crm_contact_store,
            activity_log=activity_log,
        )
        counting_participant_store = engagement_participant_store

    counts.participants_before = len(await counting_participant_store.list_for_engagement(engagement_id))

    for registration in registrations:
        try:
            result = await sync_service.sync_luma_registration_to_engagement_participant(registration)
        except Exception as e:  # noqa: BLE001 -- one bad registration must never abort the whole cohort
            counts.errors += 1
            report.errors.append(f"luma_guest_id={registration.luma_guest_id}: {type(e).__name__}: {e}")
            continue
        counts_field = _OUTCOME_TO_COUNTS_FIELD[result.outcome]
        setattr(counts, counts_field, getattr(counts, counts_field) + 1)

    counts.participants_after = len(await counting_participant_store.list_for_engagement(engagement_id))
    return report
