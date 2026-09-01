"""
WorkerLeaseService -- Astronomic Mail Phase C. Thin orchestration over
WorkerLeaseStore's atomic primitives (see that module's own docstring for
why the database, not Railway configuration alone, is the authoritative
leadership guard).

The lease name is fixed to MAIL_EXECUTION_LEASE_NAME -- this app only ever
runs one kind of execution worker today. `holder_id` is generated fresh per
process (see generate_holder_id()) -- there is deliberately no attempt to
reuse a stable identity across restarts; a fresh restart is, correctly, a
brand-new contender for leadership, not an automatic continuation of
whatever it held before crashing.
"""

import secrets
import socket
from datetime import datetime

from app.repositories.worker_lease_store import WorkerLeaseStore

MAIL_EXECUTION_LEASE_NAME = "mail_execution_worker"


def generate_holder_id() -> str:
    """`hostname-random` -- the hostname is purely for human-readable
    logs/debugging (Railway sets a container-specific hostname); the
    random suffix is what actually guarantees uniqueness even if two
    processes somehow shared a hostname (e.g. local dev)."""
    return f"{socket.gethostname()}-{secrets.token_hex(8)}"


class WorkerLeaseService:
    def __init__(self, store: WorkerLeaseStore, holder_id: str | None = None):
        self.store = store
        self.holder_id = holder_id or generate_holder_id()
        # In-memory only -- this process's own belief about whether it
        # currently holds the lease. NEVER trusted on its own for a
        # provider-invocation decision without a fresh store check (see
        # is_leader()'s docstring) -- exists purely so
        # MailExecutionWorker doesn't need to re-derive it from scratch
        # every tick for logging/liveness purposes.
        self._currently_leader = False

    async def try_acquire_or_renew(self, now: datetime, lease_duration_seconds: int) -> bool:
        """Call once per worker tick, BEFORE claiming any due row. Tries
        try_renew() first (the common case once this process already
        holds the lease); falls back to try_acquire() (first acquisition,
        or a takeover after a previous holder's lease expired) only if
        renewal fails. Updates self._currently_leader to match the real,
        just-observed result -- never assumed."""
        renewed = await self.store.try_renew(MAIL_EXECUTION_LEASE_NAME, self.holder_id, now, lease_duration_seconds)
        if renewed:
            self._currently_leader = True
            return True
        acquired = await self.store.try_acquire(MAIL_EXECUTION_LEASE_NAME, self.holder_id, now, lease_duration_seconds)
        self._currently_leader = acquired
        return acquired

    async def is_leader(self, now: datetime) -> bool:
        """A FRESH check against the store -- confirms this process's
        holder_id is the CURRENT holder AND the lease has not expired.
        This is the check MailSendingService.prepare_and_send_step()'s
        `confirm_leadership` callback should be bound to -- never the
        cached self._currently_leader flag alone, which only reflects
        the last tick's try_acquire_or_renew() result and could be stale
        by the time a slow prepare() call reaches the point of actually
        needing to know."""
        lease = await self.store.get(MAIL_EXECUTION_LEASE_NAME)
        is_leader = lease is not None and lease.holder_id == self.holder_id and lease.expires_at > now
        self._currently_leader = is_leader
        return is_leader

    async def release(self) -> bool:
        """Best-effort graceful release (clean shutdown) -- see
        WorkerLeaseStore.release()'s own docstring on why skipping this
        is always safe (the lease simply expires on its own)."""
        released = await self.store.release(MAIL_EXECUTION_LEASE_NAME, self.holder_id)
        self._currently_leader = False
        return released
