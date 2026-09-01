"""
MailSuppressionService -- the single, first-class enforcement mechanism for
"never contact this address again," independent of CrmContact.email_status
(a free-text, ITF-only convention -- see app/models/crm.py; this service
never reads or writes it). Keyed entirely by normalized email, never by
crm_contact_id, per this phase's explicit design (see MailSuppression's
docstring in app/models/mail.py).

Suppressing/unsuppressing NEVER triggers a send, a queue action, or any
CRM contact mutation -- both are pure record-keeping in this phase, since
there is nothing downstream yet that could act on them.

Phase B3: unsuppress() now refuses to reverse an active UNSUBSCRIBED
suppression (see UnsubscribeReversalNotAllowedError below) -- an
affirmative recipient opt-out is not the same kind of thing as a staff
member's manual suppression. RECOMMENDATION, NOT YET IMPLEMENTED: COMPLAINT
(a spam complaint) arguably deserves the same protection, or stronger --
reversing it risks real sender-reputation/deliverability harm, not just a
compliance question. HARD_BOUNCE is murkier: a bounce can be a genuinely
temporary/fixable condition (e.g. a contact's email was mistyped and later
corrected), so blocking its reversal outright could be actively wrong. Both
were deliberately left unchanged in B3 pending an explicit decision --
their semantics must not be silently altered alongside this unsubscribe
work.
"""

import uuid
from datetime import datetime, timezone

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import normalize_email
from app.models.mail import MailContactSuppressionStatus, MailSuppression, MailSuppressionReason
from app.repositories.mail_suppression_store import MailSuppressionStore
from app.services.activity_log_service import ActivityLogService


class InvalidMailSuppressionEmailError(ValueError):
    def __init__(self, email: str):
        self.email = email
        super().__init__(f"'{email}' is not a usable email address to suppress.")


class MailSuppressionNotFoundError(Exception):
    def __init__(self, email_normalized: str):
        self.email_normalized = email_normalized
        super().__init__(f"'{email_normalized}' has never been suppressed.")


class UnsubscribeReversalNotAllowedError(Exception):
    """Raised by unsuppress() when the target row's CURRENT reason is
    UNSUBSCRIBED and it's still active -- Phase B3's explicit decision:
    an affirmative recipient opt-out is materially different from an
    operational/manual suppression and must not be reversible through
    the same generic action a staff member uses for everything else.
    This is a SERVICE-LEVEL guard (not merely a frontend warning) --
    calling unsuppress() directly, from anywhere, on such a row always
    raises this; there is no way around it at this layer.

    Deliberately NO administrative override exists yet -- build one only
    if actually needed (per the B3 approval). If a genuine business need
    arises (e.g. a recipient re-subscribes by replying to say so), the
    fix belongs in a new, explicit, audited method -- never by quietly
    special-casing this one back open.

    HARD_BOUNCE and COMPLAINT are NOT covered by this guard in B3 -- see
    this module's own docstring for the explicit recommendation (not yet
    implemented) that COMPLAINT likely deserves the same protection."""

    def __init__(self, email_normalized: str):
        self.email_normalized = email_normalized
        super().__init__(
            f'"{email_normalized}" was suppressed by an explicit recipient unsubscribe and cannot be '
            "reversed through ordinary unsuppress."
        )


class MailSuppressionService:
    def __init__(self, store: MailSuppressionStore, activity_log: ActivityLogService):
        self.store = store
        self.activity_log = activity_log

    async def suppress(
        self, email: str, reason: MailSuppressionReason = MailSuppressionReason.MANUAL, notes: str | None = None
    ) -> MailSuppression:
        """
        Idempotent: suppressing an email that's already active WITH THE
        SAME reason is a pure no-op returning the existing row unchanged
        (no duplicate row is even possible -- email_normalized is the
        primary key, and no activity-log event fires for a true no-op).
        Re-suppressing a previously-unsuppressed (inactive) row
        reactivates it in place, updating reason/notes, rather than
        erroring.

        Phase B3 correction: suppressing an ALREADY-ACTIVE row with a
        DIFFERENT reason is NOT a no-op -- the reason (and notes) are
        updated in place, and this DOES log a fresh activity event. This
        is what makes decision #6 ("MANUAL -> recipient unsubscribe
        should result in active UNSUBSCRIBED") actually true: without
        this reason-equality check, a contact already MANUAL-suppressed
        who later unsubscribed for real would have kept showing as
        MANUAL forever, since the prior code's early return fired for
        ANY already-active row regardless of reason. Found and fixed
        during B3 implementation, not present before it.
        """
        normalized = normalize_email(email)
        if not normalized:
            raise InvalidMailSuppressionEmailError(email)

        now = datetime.now(timezone.utc)
        existing = await self.store.get(normalized)

        if existing is not None and existing.active and existing.reason == reason:
            return existing

        if existing is not None:
            updated = existing.model_copy(
                update={"active": True, "reason": reason, "notes": notes, "updated_at": now, "unsuppressed_at": None}
            )
        else:
            updated = MailSuppression(
                email_normalized=normalized, reason=reason, notes=notes, created_at=now, updated_at=now, active=True
            )

        await self.store.upsert(updated)
        await self.activity_log.record(
            event_type="mail.contact_suppressed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'"{normalized}" was suppressed ({reason.value}).',
            entity_type="email",
            entity_name=normalized,
            metadata={"reason": reason.value},
        )
        return updated

    async def unsuppress(self, email: str) -> MailSuppression:
        """Raises MailSuppressionNotFoundError if this email has never been
        suppressed at all. A no-op (returns the existing row unchanged) if
        it's already inactive -- unsuppressing twice is not an error.

        Phase B3: raises UnsubscribeReversalNotAllowedError if the row is
        ACTIVE and its reason is UNSUBSCRIBED -- see that exception's own
        docstring. Checked AFTER the not-found/already-inactive checks
        above (an inactive UNSUBSCRIBED row is already a no-op via the
        `not existing.active` branch, so the guard only ever needs to
        fire for the one case that would actually change anything)."""
        normalized = normalize_email(email)
        if not normalized:
            raise InvalidMailSuppressionEmailError(email)

        existing = await self.store.get(normalized)
        if existing is None:
            raise MailSuppressionNotFoundError(normalized)
        if not existing.active:
            return existing
        if existing.reason == MailSuppressionReason.UNSUBSCRIBED:
            raise UnsubscribeReversalNotAllowedError(normalized)

        now = datetime.now(timezone.utc)
        updated = existing.model_copy(update={"active": False, "unsuppressed_at": now, "updated_at": now})
        await self.store.upsert(updated)
        await self.activity_log.record(
            event_type="mail.contact_unsuppressed",
            category=ActivityCategory.MAIL,
            source=ActivitySource.MAIL_SYSTEM,
            summary=f'"{normalized}" was unsuppressed.',
            entity_type="email",
            entity_name=normalized,
        )
        return updated

    async def is_suppressed(self, email: str | None) -> bool:
        normalized = normalize_email(email)
        if not normalized:
            return False
        existing = await self.store.get(normalized)
        return existing is not None and existing.active

    async def get_status(self, email: str) -> MailContactSuppressionStatus:
        normalized = normalize_email(email)
        if not normalized:
            return MailContactSuppressionStatus(
                email_normalized=email, suppressed=False, reason=None, notes=None, created_at=None, unsuppressed_at=None
            )
        existing = await self.store.get(normalized)
        if existing is None:
            return MailContactSuppressionStatus(
                email_normalized=normalized, suppressed=False, reason=None, notes=None, created_at=None, unsuppressed_at=None
            )
        return MailContactSuppressionStatus(
            email_normalized=normalized,
            suppressed=existing.active,
            reason=existing.reason,
            notes=existing.notes,
            created_at=existing.created_at,
            unsuppressed_at=existing.unsuppressed_at,
        )

    async def list_all(self) -> list[MailSuppression]:
        return await self.store.list()

    async def list_active_suppressed_emails(self) -> set[str]:
        """Bulk read used by MailCampaignService's Review calculation and
        mark_ready() enrollment snapshot -- one query, checked in-memory
        against however many contacts a list has, rather than one lookup
        per contact."""
        return {row.email_normalized for row in await self.store.list() if row.active}
