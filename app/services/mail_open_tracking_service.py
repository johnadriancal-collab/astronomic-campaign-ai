"""
MailOpenTrackingService -- open tracking (2026-09-18). The ONE write
path for MailOpenEvent: record_open() looks up the MailEnrollmentStep
that owns a given opaque token and, if found, upserts its open-event
row. Read-only against MailEnrollmentStepStore (never mutates the step
itself) -- opening an email is not a step-execution-state change.

Deliberately silent/no-op for an unknown token (never raises, never
distinguishes "wrong token" from "valid token, already recorded") --
see app/api/mail_open_tracking.py's own docstring for why the pixel
response must be identical either way.
"""

from datetime import datetime

from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_open_event_store import MailOpenEventStore


class MailOpenTrackingService:
    def __init__(self, enrollment_step_store: MailEnrollmentStepStore, open_event_store: MailOpenEventStore):
        self.enrollment_step_store = enrollment_step_store
        self.open_event_store = open_event_store

    async def record_open(self, token: str, now: datetime) -> None:
        step = await self.enrollment_step_store.get_by_open_tracking_token(token)
        if step is None:
            return
        await self.open_event_store.record_open(
            enrollment_step_id=step.enrollment_step_id,
            mail_campaign_id=step.mail_campaign_id,
            enrollment_id=step.enrollment_id,
            at=now,
        )
