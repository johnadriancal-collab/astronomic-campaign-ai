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

Stage 5A exposes exactly the primitives Stage 5D's freeze/reconcile logic
will need -- create_occurrence() (the idempotent claim) and
freeze_members() (the one atomic multi-row write) -- WITHOUT implementing
any of the surrounding business logic (which enrollments are eligible,
when an occurrence is due, what a member's outcome should be). Nothing in
this codebase calls any of this yet.
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
        if occurrence.frozen_at is not None:
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
