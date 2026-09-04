"""
Storage abstraction for MailTriggerOccurrence + MailTriggerOccurrenceMember
(Trigger feature, Stage 5A, 2026-09-04).

ONE store/interface owns BOTH entities deliberately -- not two separate
stores -- because the future cohort-freeze operation (Stage 5D) needs to
insert every frozen member row AND stamp the occurrence's own `frozen_at`
in one atomic SQLite transaction. This codebase's stores each hold their
own independent aiosqlite connection for the process's whole life (see
sqlite_connection.py's own docstring); two different store objects, even
against the same underlying .db file, cannot share one transaction. So the
only way an atomic freeze is actually achievable is for one store/one
connection to own both tables -- see sqlite_mail_trigger_occurrence_store.py.

Stage 5A exposed the freeze-side primitives -- create_occurrence() (the
idempotent claim) and freeze_members() (the one atomic multi-row write).
Stage 5D (2026-09-04) adds the two reconciliation-side primitives this
store was always going to need but Stage 5A deliberately didn't build yet
(no reconciliation logic existed to call them) -- mark_member_reconciled()
(a CAS-guarded per-member outcome write) and complete_occurrence() (a
CAS-guarded occurrence-level status transition). Both operate on columns
that already existed in Stage 5A's own schema (`outcome`/`reconciled_at`,
`status`/`started_count`/`completed_at`) -- no new table, no new column,
purely additive store methods for state Stage 5A's models always defined.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.mail import MailTriggerOccurrence, MailTriggerOccurrenceMember


class MailTriggerOccurrenceStore(ABC):
    @abstractmethod
    async def create_occurrence(self, occurrence: MailTriggerOccurrence) -> bool:
        """Idempotent claim: inserts a new PREPARING row for
        (trigger_id, scheduled_for) if none exists yet. Returns True if
        this call created it, False if a row already existed (in which
        case the existing row -- not this argument -- is authoritative;
        callers should re-fetch via get_occurrence())."""

    @abstractmethod
    async def get_occurrence(self, trigger_id: str, scheduled_for: datetime) -> MailTriggerOccurrence | None:
        """Returns the occurrence, or None if it doesn't exist yet."""

    @abstractmethod
    async def freeze_members(
        self, trigger_id: str, scheduled_for: datetime, enrollment_ids: list[str], now: datetime
    ) -> bool:
        """Atomically, in ONE transaction: stamps this occurrence's
        `frozen_at` (only if it is currently None) and inserts one
        PENDING_RECONCILE member row per id in `enrollment_ids`. A
        candidate that already appears as a member of ANY OTHER occurrence
        (the global UNIQUE(enrollment_id) constraint) is silently excluded
        from THIS insert rather than failing the whole call -- see
        MailTriggerOccurrenceMember's own docstring for why that's the
        correct, accepted behavior, not an error.

        Returns True if this call performed the freeze (occurrence's
        `frozen_at` was None and is now set), False if the occurrence was
        already frozen by an earlier attempt -- in which case this call
        changed NOTHING (not even a partial member insert), so the
        cohort's existing member rows (see list_members()) remain the
        single source of truth for what was actually frozen.

        Raises ValueError if no occurrence row exists yet for
        (trigger_id, scheduled_for) -- create_occurrence() must run first.
        """

    @abstractmethod
    async def list_members(self, trigger_id: str, scheduled_for: datetime) -> list[MailTriggerOccurrenceMember]:
        """Every member frozen into this occurrence's cohort, in the order
        they were inserted. Empty list means either not yet frozen, or
        frozen with zero eligible candidates -- see MailTriggerOccurrence's
        own `frozen_at` field for how callers tell those two apart."""

    @abstractmethod
    async def mark_member_reconciled(
        self, trigger_id: str, scheduled_for: datetime, enrollment_id: str, outcome: str, reconciled_at: datetime
    ) -> None:
        """CAS-guarded: only applies if this member's CURRENT outcome is
        still PENDING_RECONCILE -- a retry after a crash (or a second,
        redundant reconciliation attempt) safely no-ops for a member
        that's already reconciled, rather than overwriting a terminal
        outcome. `outcome` is the caller's own already-decided value
        (STARTED or SKIPPED_INELIGIBLE) -- this method performs no
        eligibility logic itself, purely a durable write."""

    @abstractmethod
    async def complete_occurrence(
        self, trigger_id: str, scheduled_for: datetime, started_count: int, completed_at: datetime
    ) -> bool:
        """CAS-guarded: only transitions status PREPARING -> COMPLETED,
        storing the given `started_count` (the caller's own already-
        computed value, derived from durable member outcomes -- this
        method does not recompute it). Returns True if this call performed
        the transition, False if the occurrence was already COMPLETED (a
        safe no-op -- e.g. a retry after the transition already landed but
        before the caller observed success)."""

    @abstractmethod
    async def get_latest_occurrence_for_campaign_between(
        self, mail_campaign_id: str, start_utc: datetime, end_utc: datetime
    ) -> MailTriggerOccurrence | None:
        """The single occurrence (any status -- PREPARING never reaches
        this call in practice, see below; COMPLETED and SUPERSEDED both
        count) with the LARGEST `scheduled_for` in `[start_utc, end_utc)`
        for this campaign, across ALL of its triggers, or None if no
        occurrence exists in that range yet. Added Stage 5E (2026-09-04)
        as the durable-history read behind "latest-selected-today":
        `start_utc`/`end_utc` are the caller's own campaign-local calendar
        day boundaries, already converted to UTC (this store stays
        timezone-naive by design -- see MailTriggerService's own
        docstring for why that conversion belongs in the service layer).

        Deliberately NOT filtered by status: a SUPERSEDED row's own
        scheduled_for is, by construction, never later than the
        occurrence that actually preempted it (see
        MailTriggerOccurrence's own docstring), so including it can never
        cause an eligible later candidate to be wrongly excluded -- and
        excluding it would let discovery briefly disagree with itself
        about "the latest row for today" depending on ordering. Callers
        should not expect this to return a PREPARING row in ordinary
        operation: fresh discovery only ever runs once
        process_due_occurrences() has already resolved any outstanding
        PREPARING occurrence for the campaign (see that method's own
        docstring) -- so by the time this is called, every existing row
        for today is terminal (COMPLETED or SUPERSEDED)."""

    @abstractmethod
    async def supersede_occurrence(self, trigger_id: str, scheduled_for: datetime, now: datetime) -> bool:
        """CAS-guarded, terminal, one-way: PREPARING -> SUPERSEDED, and
        ONLY while `frozen_at IS NULL` -- once a cohort has been frozen
        (committed), even a deliberately empty one (Stage 5A's own
        legitimate "frozen with zero eligible members" shape), this
        occurrence has crossed the commitment boundary and can never be
        superseded, matching `frozen_at`'s own existing meaning
        everywhere else in this store. This predicate is enforced BY THE
        STORE itself, in the same atomic statement as the status
        transition -- not merely pre-checked by the caller -- specifically
        so that a freeze_members() call and a supersede_occurrence() call
        racing for the same row can never both succeed: whichever of the
        two atomic writes actually lands first durably changes either
        `frozen_at` or `status`, which makes the other's own WHERE
        predicate stop matching. See this store's own module docstring
        for why one connection per store (no separate process-local lock)
        is what makes this a genuine mutual-exclusion guarantee, not just
        a best-effort ordering.

        Returns True if this call performed the transition, False if it
        was already SUPERSEDED (safe no-op retry), already COMPLETED, or
        already frozen (the race case above) -- in every False case, the
        occurrence's own durable state, not this return value, is what
        the caller must re-read to decide what happened."""

    @abstractmethod
    async def list_preparing_occurrences_for_campaign(self, mail_campaign_id: str) -> list[MailTriggerOccurrence]:
        """Every occurrence for this campaign (across ALL of its triggers)
        still in status PREPARING -- i.e. discovered/created but not yet
        completed. Added Stage 5E (2026-09-04) to let the worker find and
        resume an in-flight occurrence BEFORE evaluating whether a new one
        should be discovered, without needing to know which trigger or
        which scheduled_for it belongs to in advance. Pure additive read
        method against Stage 5A's existing `status` column -- no schema
        change. Under normal operation there is at most one such row per
        campaign at a time (see MailTriggerService.process_due_occurrences
        for the invariant that keeps it that way); callers should not
        assume the list is empty or singleton, only handle it generically."""


class MemoryMailTriggerOccurrenceStore(MailTriggerOccurrenceStore):
    """Dict-backed -- not persistent, for tests/local dev. Enforces the
    SAME uniqueness/atomicity contract as the SQLite implementation (global
    enrollment_id uniqueness, all-or-nothing... actually all-or-EXCLUDE
    freeze) so tests written against this store exercise real behavior,
    not a simplified stand-in."""

    def __init__(self):
        self._occurrences: dict[tuple[str, datetime], MailTriggerOccurrence] = {}
        self._members: dict[tuple[str, datetime, str], MailTriggerOccurrenceMember] = {}
        self._claimed_enrollment_ids: set[str] = set()

    async def create_occurrence(self, occurrence: MailTriggerOccurrence) -> bool:
        key = (occurrence.trigger_id, occurrence.scheduled_for)
        if key in self._occurrences:
            return False
        self._occurrences[key] = occurrence
        return True

    async def get_occurrence(self, trigger_id: str, scheduled_for: datetime) -> MailTriggerOccurrence | None:
        return self._occurrences.get((trigger_id, scheduled_for))

    async def freeze_members(
        self, trigger_id: str, scheduled_for: datetime, enrollment_ids: list[str], now: datetime
    ) -> bool:
        key = (trigger_id, scheduled_for)
        occurrence = self._occurrences.get(key)
        if occurrence is None:
            raise ValueError(f"No occurrence exists for ({trigger_id!r}, {scheduled_for!r}) -- create it first.")
        # Stage 5E: status='PREPARING' is now also required, matching the
        # SQLite store's own predicate -- see its freeze_members() comment
        # for why frozen_at IS NULL alone is no longer a sufficient CAS
        # guard once SUPERSEDED exists.
        if occurrence.frozen_at is not None or occurrence.status != "PREPARING":
            return False
        self._occurrences[key] = occurrence.model_copy(update={"frozen_at": now})
        for enrollment_id in enrollment_ids:
            if enrollment_id in self._claimed_enrollment_ids:
                continue  # matches SQLite's global UNIQUE(enrollment_id): excluded, not an error
            self._claimed_enrollment_ids.add(enrollment_id)
            member_key = (trigger_id, scheduled_for, enrollment_id)
            self._members[member_key] = MailTriggerOccurrenceMember(
                trigger_id=trigger_id, scheduled_for=scheduled_for, enrollment_id=enrollment_id
            )
        return True

    async def list_members(self, trigger_id: str, scheduled_for: datetime) -> list[MailTriggerOccurrenceMember]:
        return [m for (t, s, _e), m in self._members.items() if t == trigger_id and s == scheduled_for]

    async def mark_member_reconciled(
        self, trigger_id: str, scheduled_for: datetime, enrollment_id: str, outcome: str, reconciled_at: datetime
    ) -> None:
        key = (trigger_id, scheduled_for, enrollment_id)
        member = self._members.get(key)
        if member is None or member.outcome != "PENDING_RECONCILE":
            return  # CAS miss -- already reconciled (or never froze), safe no-op
        self._members[key] = member.model_copy(update={"outcome": outcome, "reconciled_at": reconciled_at})

    async def complete_occurrence(
        self, trigger_id: str, scheduled_for: datetime, started_count: int, completed_at: datetime
    ) -> bool:
        key = (trigger_id, scheduled_for)
        occurrence = self._occurrences.get(key)
        if occurrence is None or occurrence.status != "PREPARING":
            return False
        self._occurrences[key] = occurrence.model_copy(
            update={"status": "COMPLETED", "started_count": started_count, "completed_at": completed_at}
        )
        return True

    async def list_preparing_occurrences_for_campaign(self, mail_campaign_id: str) -> list[MailTriggerOccurrence]:
        return [
            occurrence
            for occurrence in self._occurrences.values()
            if occurrence.mail_campaign_id == mail_campaign_id and occurrence.status == "PREPARING"
        ]

    async def get_latest_occurrence_for_campaign_between(
        self, mail_campaign_id: str, start_utc: datetime, end_utc: datetime
    ) -> MailTriggerOccurrence | None:
        candidates = [
            occurrence
            for occurrence in self._occurrences.values()
            if occurrence.mail_campaign_id == mail_campaign_id and start_utc <= occurrence.scheduled_for < end_utc
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda occurrence: occurrence.scheduled_for)

    async def supersede_occurrence(self, trigger_id: str, scheduled_for: datetime, now: datetime) -> bool:
        key = (trigger_id, scheduled_for)
        occurrence = self._occurrences.get(key)
        if occurrence is None or occurrence.status != "PREPARING" or occurrence.frozen_at is not None:
            return False
        self._occurrences[key] = occurrence.model_copy(update={"status": "SUPERSEDED", "completed_at": now})
        return True
