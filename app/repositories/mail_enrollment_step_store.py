"""
Storage abstraction for MailEnrollmentStep -- see that model's own docstring
(app/models/mail.py) for the lazy-materialization strategy and the full
per-step state machine. `create()` is idempotent by contract (same shape as
MailEnrollmentStore.create()) -- UNIQUE(enrollment_id, step_id) is enforced
at the DB layer (see sqlite_mail_enrollment_step_store.py), and this ABC's
`create()` mirrors that: it returns whether a NEW row was actually
inserted, never raises for a repeat, so activate_campaign()'s Step 1
materialization loop is safely re-callable (e.g. after a partial failure)
without ever duplicating a row -- the exact same resilience property
mark_ready()'s own enrollment-snapshot loop already relies on.

`try_transition()` is the one atomic primitive every status-gated mutation
in MailSendingService goes through -- a compare-and-swap: apply the given
FULL new row iff the row's CURRENT status is still exactly
`expected_status`. This is what makes the QUEUED->CLAIMED transition safe
even if more than one caller ever raced to claim the same row (SQLite's own
single-writer-at-a-time locking makes the underlying conditional UPDATE
statement itself atomic -- see sqlite_mail_enrollment_step_store.py). It is
deliberately general (not a bespoke "claim()" method) so the SAME primitive
also protects CLAIMED->SENDING, SENDING->SENT/UNKNOWN, and every other
status-gated step transition, rather than each inventing its own
compare-and-swap shape.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus


class MailEnrollmentStepNotFoundError(Exception):
    def __init__(self, enrollment_step_id: str):
        self.enrollment_step_id = enrollment_step_id
        super().__init__(f"MailEnrollmentStep not found: {enrollment_step_id}")


class MailEnrollmentStepStore(ABC):
    @abstractmethod
    async def create(self, step: MailEnrollmentStep) -> bool:
        """Idempotent. Returns True if this was a new row, False if
        (enrollment_id, step_id) already existed (no-op)."""

    @abstractmethod
    async def get(self, enrollment_step_id: str) -> MailEnrollmentStep | None: ...

    @abstractmethod
    async def get_by_enrollment_and_step(self, enrollment_id: str, step_id: str) -> MailEnrollmentStep | None:
        """Used to check "does this enrollment already have a row for this
        step" before creating one -- the read half of create()'s
        idempotency (create() itself is also safe to call blind, but
        callers that need the EXISTING row back, not just a bool, use this)."""

    @abstractmethod
    async def save(self, step: MailEnrollmentStep) -> None:
        """Unconditional overwrite. Raises MailEnrollmentStepNotFoundError
        if the row doesn't exist. Callers that need "only if the row is
        still in the state I expect" MUST use try_transition() instead --
        save() offers no such guarantee and must never be used for a
        status-gated mutation two callers could race on."""

    @abstractmethod
    async def try_transition(
        self, enrollment_step_id: str, expected_status: MailEnrollmentStepStatus, updated: MailEnrollmentStep
    ) -> bool:
        """Atomically applies `updated` (the full new row) iff the row's
        CURRENT status is still exactly `expected_status`. Returns True iff
        this call's write actually applied; False means the row's status
        had already changed (a lost race, or it was already reaped/
        resolved by something else) -- the caller must not assume its own
        prior read of the row is still current when this returns False."""

    @abstractmethod
    async def persist_prepared_fields(self, enrollment_step_id: str, updated: MailEnrollmentStep) -> bool:
        """Phase C: atomically applies `updated` (the full row) iff the
        row's CURRENT status is still exactly CLAIMED -- a semantically
        named, SAME-STATUS use of the exact try_transition() primitive
        (`expected_status=CLAIMED`, `updated.status` also CLAIMED). Exists
        so execution code can durably commit prepared, pre-provider fields
        (most importantly `rfc_message_id` -- see MailSendRequest's
        MANDATORY PHASE C INVARIANT in app/services/mail_sending_service.py)
        WITHOUT yet crossing the CLAIMED->SENDING boundary, and without
        scattering confusing same-status try_transition() calls through
        execution code. Returns True iff the write applied; False means
        the row moved out from under this attempt (e.g. a concurrent
        reap_orphans() reset it to QUEUED for a stale claim) -- the caller
        must abort this attempt cleanly, never assume its prepared fields
        landed."""

    @abstractmethod
    async def list_for_enrollment(self, enrollment_id: str) -> list[MailEnrollmentStep]:
        """Every step execution row for one enrollment, ordered by
        step_number ascending."""

    @abstractmethod
    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentStep]:
        """Every step execution row for one campaign -- used by admin/
        Leads-tab-style views and by tests, not by the hot poll path."""

    @abstractmethod
    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        """Clears every row for this campaign -- used by unlock_campaign()
        when a READY campaign's stale snapshot (both MailEnrollment AND
        MailEnrollmentStep rows) must be discarded before it can be edited
        again. See MailCampaignService.unlock_campaign()'s docstring."""

    @abstractmethod
    async def list_due(self, now: datetime, limit: int = 100) -> list[MailEnrollmentStep]:
        """Every QUEUED row whose next_send_at is at or before `now`,
        across every campaign, oldest-due-first, capped at `limit` -- the
        worker's core poll query in a future phase. Callable and directly
        testable in Phase A even with no real worker."""

    @abstractmethod
    async def count_sent_step_for_campaign_since(
        self, mail_campaign_id: str, step_number: int, since: datetime
    ) -> int:
        """How many rows for this campaign, at this exact step_number, have
        reached SENT with `sent_at >= since` -- the query behind
        `daily_lead_start_limit` enforcement (called with step_number=1 and
        `since` = the start of the current campaign-local calendar day;
        see MailSendingService's runtime safety checks). Deliberately
        step_number-scoped so follow-up sends never count against this
        limit, per MailCampaign.daily_lead_start_limit's own docstring."""

    @abstractmethod
    async def count_sent_for_mailbox_since(self, mailbox_id: str, since: datetime) -> int:
        """How many rows sent BY this mailbox (any campaign, any step
        number) have `sent_at >= since` -- the query behind
        MailboxSendPolicy.daily_send_limit enforcement. `since` is always
        the start of the current UTC calendar day -- see that field's own
        docstring for why the boundary is UTC-fixed, not campaign-local."""

    @abstractmethod
    async def get_most_recent_sent_for_mailbox(self, mailbox_id: str) -> MailEnrollmentStep | None:
        """The single most-recently-SENT row for this mailbox (any
        campaign) -- the query behind MailboxSendPolicy.
        min_seconds_between_sends pacing enforcement. None if this mailbox
        has never sent anything."""

    @abstractmethod
    async def list_stale_claimed(self, older_than: datetime) -> list[MailEnrollmentStep]:
        """Every row still CLAIMED with claimed_at at or before
        `older_than` -- safe to auto-reset to QUEUED (see
        MailEnrollmentStepStatus.CLAIMED's docstring: no provider call has
        happened yet in this state, so this is never ambiguous)."""

    @abstractmethod
    async def list_stale_sending(self, older_than: datetime) -> list[MailEnrollmentStep]:
        """Every row still SENDING with claimed_at at or before
        `older_than` -- NEVER auto-resolved; the caller's only correct
        action is moving these to UNKNOWN for manual reconciliation (see
        MailEnrollmentStepStatus.SENDING/UNKNOWN's docstrings)."""


class MemoryMailEnrollmentStepStore(MailEnrollmentStepStore):
    """Dict-backed, keyed by enrollment_step_id -- not persistent, for
    tests/local dev. Single-process/single-coroutine-at-a-time by
    construction (no real concurrency exists in a test run using this
    store), so try_transition() here is a plain check-then-set -- the
    REAL atomicity guarantee this class exists to model is provided by
    SQLiteMailEnrollmentStepStore's conditional UPDATE, not this one; see
    that store's own docstring."""

    def __init__(self):
        self._rows: dict[str, MailEnrollmentStep] = {}

    def _collides(self, step: MailEnrollmentStep) -> bool:
        return any(
            r.enrollment_id == step.enrollment_id and r.step_id == step.step_id and r.enrollment_step_id != step.enrollment_step_id
            for r in self._rows.values()
        )

    async def create(self, step: MailEnrollmentStep) -> bool:
        if step.enrollment_step_id in self._rows:
            return False
        if self._collides(step):
            return False
        self._rows[step.enrollment_step_id] = step
        return True

    async def get(self, enrollment_step_id: str) -> MailEnrollmentStep | None:
        return self._rows.get(enrollment_step_id)

    async def get_by_enrollment_and_step(self, enrollment_id: str, step_id: str) -> MailEnrollmentStep | None:
        for row in self._rows.values():
            if row.enrollment_id == enrollment_id and row.step_id == step_id:
                return row
        return None

    async def save(self, step: MailEnrollmentStep) -> None:
        if step.enrollment_step_id not in self._rows:
            raise MailEnrollmentStepNotFoundError(step.enrollment_step_id)
        self._rows[step.enrollment_step_id] = step

    async def try_transition(
        self, enrollment_step_id: str, expected_status: MailEnrollmentStepStatus, updated: MailEnrollmentStep
    ) -> bool:
        current = self._rows.get(enrollment_step_id)
        if current is None or current.status != expected_status:
            return False
        self._rows[enrollment_step_id] = updated
        return True

    async def persist_prepared_fields(self, enrollment_step_id: str, updated: MailEnrollmentStep) -> bool:
        return await self.try_transition(enrollment_step_id, MailEnrollmentStepStatus.CLAIMED, updated)

    async def list_for_enrollment(self, enrollment_id: str) -> list[MailEnrollmentStep]:
        matching = [r for r in self._rows.values() if r.enrollment_id == enrollment_id]
        return sorted(matching, key=lambda r: r.step_number)

    async def list_for_campaign(self, mail_campaign_id: str) -> list[MailEnrollmentStep]:
        matching = [r for r in self._rows.values() if r.mail_campaign_id == mail_campaign_id]
        return sorted(matching, key=lambda r: (r.enrollment_id, r.step_number))

    async def delete_for_campaign(self, mail_campaign_id: str) -> None:
        for key in [k for k, r in self._rows.items() if r.mail_campaign_id == mail_campaign_id]:
            del self._rows[key]

    async def list_due(self, now: datetime, limit: int = 100) -> list[MailEnrollmentStep]:
        due = [
            r
            for r in self._rows.values()
            if r.status == MailEnrollmentStepStatus.QUEUED and r.next_send_at is not None and r.next_send_at <= now
        ]
        due.sort(key=lambda r: r.next_send_at)
        return due[:limit]

    async def count_sent_step_for_campaign_since(
        self, mail_campaign_id: str, step_number: int, since: datetime
    ) -> int:
        return sum(
            1
            for r in self._rows.values()
            if r.mail_campaign_id == mail_campaign_id
            and r.step_number == step_number
            and r.status == MailEnrollmentStepStatus.SENT
            and r.sent_at is not None
            and r.sent_at >= since
        )

    async def count_sent_for_mailbox_since(self, mailbox_id: str, since: datetime) -> int:
        return sum(
            1
            for r in self._rows.values()
            if r.mailbox_id == mailbox_id
            and r.status == MailEnrollmentStepStatus.SENT
            and r.sent_at is not None
            and r.sent_at >= since
        )

    async def get_most_recent_sent_for_mailbox(self, mailbox_id: str) -> MailEnrollmentStep | None:
        sent = [
            r
            for r in self._rows.values()
            if r.mailbox_id == mailbox_id and r.status == MailEnrollmentStepStatus.SENT and r.sent_at is not None
        ]
        if not sent:
            return None
        return max(sent, key=lambda r: r.sent_at)

    async def list_stale_claimed(self, older_than: datetime) -> list[MailEnrollmentStep]:
        return [
            r
            for r in self._rows.values()
            if r.status == MailEnrollmentStepStatus.CLAIMED and r.claimed_at is not None and r.claimed_at <= older_than
        ]

    async def list_stale_sending(self, older_than: datetime) -> list[MailEnrollmentStep]:
        return [
            r
            for r in self._rows.values()
            if r.status == MailEnrollmentStepStatus.SENDING and r.claimed_at is not None and r.claimed_at <= older_than
        ]
