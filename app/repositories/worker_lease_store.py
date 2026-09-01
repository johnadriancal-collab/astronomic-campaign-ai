"""
Storage abstraction for WorkerLease -- see app/models/worker_lease.py's
module docstring for why this exists at the database level rather than
relying on Railway configuration alone.

`try_acquire()` and `try_renew()` are the two real compare-and-swap
primitives here, matching this codebase's established idiom
(MailEnrollmentStepStore.try_transition(), MailEnrollmentStore.
try_assign_mailbox()) -- both are single, atomic, conditional writes; no
read-then-write round trip that could race.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from app.models.worker_lease import WorkerLease


class WorkerLeaseStore(ABC):
    @abstractmethod
    async def try_acquire(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        """Atomically claims `lease_name` for `holder_id` IFF no row exists
        for it yet, OR the existing row's `expires_at` is at or before
        `now` (the previous holder's lease has expired -- a crash/timeout
        takeover, not a graceful handoff). Returns True iff this call's
        write actually applied. A currently-valid lease held by ANY
        holder_id (including this same one calling try_acquire() again
        instead of try_renew()) blocks this -- try_acquire() is for
        FIRST acquisition or takeover, never for a holder renewing its
        own still-valid lease (use try_renew() for that)."""

    @abstractmethod
    async def try_renew(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        """Atomically extends `lease_name`'s `expires_at` to
        `now + lease_duration_seconds` IFF the row's CURRENT `holder_id`
        is still exactly `holder_id`. Returns False if this holder no
        longer owns the lease (someone else already took it over after
        this holder's previous lease expired) -- the caller MUST treat
        False as "leadership lost," never retry blindly, and must stop
        claiming new work / invoking any provider immediately (see
        app/services/worker_lease_service.py)."""

    @abstractmethod
    async def get(self, lease_name: str) -> WorkerLease | None: ...

    @abstractmethod
    async def release(self, lease_name: str, holder_id: str) -> bool:
        """Best-effort clean release (graceful shutdown) -- deletes the row
        IFF `holder_id` still matches, so a holder that already lost the
        lease can never accidentally delete a DIFFERENT holder's valid
        one. Returns True iff a row was actually deleted. Not calling
        this on shutdown is always safe -- the lease simply expires on
        its own schedule; this only makes a faster, cleaner handoff
        possible on a graceful stop."""


class MemoryWorkerLeaseStore(WorkerLeaseStore):
    """Dict-backed, keyed by lease_name -- not persistent, for tests/local
    dev. Single-process/single-coroutine-at-a-time by construction, same
    caveat as every other Memory*Store in this codebase: the REAL
    atomicity guarantee this class exists to model is provided by
    SQLiteWorkerLeaseStore's conditional UPDATE/UPSERT, not this one."""

    def __init__(self):
        self._leases: dict[str, WorkerLease] = {}

    async def try_acquire(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        current = self._leases.get(lease_name)
        if current is not None and current.expires_at > now:
            return False
        self._leases[lease_name] = WorkerLease(
            lease_name=lease_name,
            holder_id=holder_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=lease_duration_seconds),
            updated_at=now,
        )
        return True

    async def try_renew(self, lease_name: str, holder_id: str, now: datetime, lease_duration_seconds: int) -> bool:
        current = self._leases.get(lease_name)
        if current is None or current.holder_id != holder_id:
            return False
        self._leases[lease_name] = current.model_copy(
            update={"expires_at": now + timedelta(seconds=lease_duration_seconds), "updated_at": now}
        )
        return True

    async def get(self, lease_name: str) -> WorkerLease | None:
        return self._leases.get(lease_name)

    async def release(self, lease_name: str, holder_id: str) -> bool:
        current = self._leases.get(lease_name)
        if current is None or current.holder_id != holder_id:
            return False
        del self._leases[lease_name]
        return True
