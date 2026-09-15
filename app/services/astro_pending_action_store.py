"""
AstroPendingActionStore -- in-memory, time-limited holding area for a
proposed-but-not-yet-executed Astro AI write action (Astro AI Phase 3,
2026-09-15).

Mirrors AstroExportStore's own in-memory dict + TTL + prune-on-access
shape (which itself mirrors MailboxService's `_pending_states` OAuth-state
pattern) rather than inventing a new one. Differs from AstroExportStore in
the direction MailboxService's `_consume_state()` already established:
executing a pending action is meant to happen AT MOST ONCE. It differs
from `_consume_state()` too, though: that pops (deletes) on first use, so
a second attempt can't be told apart from "never existed". Here, a
CONFIRMED pending action is deliberately kept around (marked EXECUTED, not
deleted) so a repeated confirm_astro_action call on the same
pending_action_id can return a clean "already_executed" no-op instead of
either re-running the mutation or returning a misleading "not found".

Not a generic workflow engine on purpose -- this holds ONLY the concrete,
already-resolved parameters for exactly the four narrow Astro write tools
(remove_crm_contact_from_list, update_crm_contact_investor_field's
confirmation-gated field/operation combinations, and
mark_crm_contact_engagement_attendance), each represented as one
self-contained async `execute` closure captured by the proposing tool
handler at PROPOSE time. This store never knows what a given action
actually does -- it only tracks whether it's still pending, has expired,
or has already run.

Single-instance constraint (documented, not solved here, same as
AstroExportStore): process-local memory only. Fine at today's
single-instance Railway deployment; would need shared storage if ever
scaled to multiple replicas.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

PENDING_ACTION_TTL = timedelta(minutes=20)


class PendingActionStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"


@dataclass
class PendingAstroAction:
    tool_name: str
    description: str  # human-readable, shown to the user via Astro's own reply text
    before: Any
    after: Any
    execute: Callable[[], Awaitable[dict]]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: PendingActionStatus = PendingActionStatus.PENDING
    executed_at: datetime | None = None


class AstroPendingActionStore:
    """Write path: `put()`, called by a write tool's "propose" handler
    once Contact/List/Engagement resolution is exact and unambiguous --
    never for an ambiguous resolution (see astro_crm_tools.py /
    astro_client_crm_tools.py's own propose-handler docstrings for that
    rule). Read/execute path: `get()` (peek, does not consume) and
    `mark_executed()` (the only state transition this store performs)."""

    def __init__(self):
        self._pending: dict[str, PendingAstroAction] = {}

    def put(self, tool_name: str, description: str, before: Any, after: Any, execute: Callable[[], Awaitable[dict]]) -> str:
        self._prune_expired()
        pending_action_id = str(uuid.uuid4())
        self._pending[pending_action_id] = PendingAstroAction(
            tool_name=tool_name, description=description, before=before, after=after, execute=execute
        )
        return pending_action_id

    def get(self, pending_action_id: str) -> PendingAstroAction | None:
        """None means "not found, expired, or never existed" -- these are
        deliberately indistinguishable from the outside (same "don't leak
        which case it was" precedent as AstroExportStore's own `get()`),
        EXCEPT an already-EXECUTED action, which is never pruned by TTL
        and is always still returned here so the caller can detect
        "already_executed" and return a safe no-op rather than either
        re-running the mutation or a confusing not-found."""
        self._prune_expired()
        return self._pending.get(pending_action_id)

    def mark_executed(self, pending_action_id: str) -> None:
        pending = self._pending.get(pending_action_id)
        if pending is not None:
            pending.status = PendingActionStatus.EXECUTED
            pending.executed_at = datetime.now(timezone.utc)

    def _prune_expired(self) -> None:
        # Only PENDING (never-confirmed) actions expire and are removed --
        # an EXECUTED action is kept indefinitely (bounded by process
        # lifetime; this store is in-memory and empties on every restart)
        # specifically so a repeated confirm attempt still finds it and
        # gets "already_executed", not "not found".
        now = datetime.now(timezone.utc)
        expired = [
            pid
            for pid, p in self._pending.items()
            if p.status == PendingActionStatus.PENDING and now - p.created_at > PENDING_ACTION_TTL
        ]
        for pid in expired:
            del self._pending[pid]
