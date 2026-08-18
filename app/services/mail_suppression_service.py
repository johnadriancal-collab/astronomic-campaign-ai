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


class MailSuppressionService:
    def __init__(self, store: MailSuppressionStore, activity_log: ActivityLogService):
        self.store = store
        self.activity_log = activity_log

    async def suppress(
        self, email: str, reason: MailSuppressionReason = MailSuppressionReason.MANUAL, notes: str | None = None
    ) -> MailSuppression:
        """
        Idempotent: suppressing an email that's already active is a pure
        no-op returning the existing row unchanged (no duplicate row is
        even possible -- email_normalized is the primary key). Re-
        suppressing a previously-unsuppressed (inactive) row reactivates it
        in place, updating reason/notes, rather than erroring.
        """
        normalized = normalize_email(email)
        if not normalized:
            raise InvalidMailSuppressionEmailError(email)

        now = datetime.now(timezone.utc)
        existing = await self.store.get(normalized)

        if existing is not None and existing.active:
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
        it's already inactive -- unsuppressing twice is not an error."""
        normalized = normalize_email(email)
        if not normalized:
            raise InvalidMailSuppressionEmailError(email)

        existing = await self.store.get(normalized)
        if existing is None:
            raise MailSuppressionNotFoundError(normalized)
        if not existing.active:
            return existing

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
