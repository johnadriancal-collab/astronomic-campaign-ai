"""
MailCampaignMailboxNextSendService -- proactive OAuth expiration warnings
(2026-09-17, mailbox-attribution fix 2026-09-18). Answers exactly one
question per campaign: for each mailbox currently assigned to send it,
what is the earliest future send that can be CONFIDENTLY attributed to
it? That's the one piece a campaign detail page needs to compare against
a mailbox's own `estimated_expires_at` (from GET /mailboxes -- see
MailboxListItem) to decide whether to show the stronger "reconnect
required before next scheduled send" warning.

2026-09-18 fix: the original implementation filtered waiting steps by
`step.mailbox_id == mailbox_id` directly. That field is null until a
step is actually CLAIMED (see MailSendingService.persist_prepared_
fields()) -- so for any enrollment whose Step 1 hasn't been claimed yet,
that filter could never match, meaning this warning could silently fail
to appear before the exact send it's meant to warn about. Attribution
now goes through the SAME safe logic MailboxMetricsService uses for its
own Queue count -- see app/services/mail_step_mailbox_attribution.py's
attribute_step_to_mailbox() docstring for exactly which not-yet-assigned
enrollments get attributed to which mailbox and why (never guessed).

A PAUSED/DRAFT/READY/COMPLETED/ARCHIVED campaign returns next_send_at =
None for every one of its mailboxes, regardless of what steps exist --
nothing sends while a campaign isn't ACTIVE, so surfacing a "next send"
time for one would misleadingly imply an imminent send that won't
actually happen.

Read-only, no new persistence -- reuses MailEnrollmentStore.
list_for_campaign() and MailEnrollmentStepStore.list_for_campaign()
(both already used elsewhere this session) rather than adding a new
store method, same V1 pilot-scale stance as every other read service.
"""

from datetime import datetime

from app.models.mail import MailCampaignMailboxNextSend, MailCampaignStatus
from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.services.mail_step_mailbox_attribution import WAITING_STEP_STATUSES, attribute_step_to_mailbox

# Reuses MailCampaignService's own MailCampaignNotFound -- a lightweight
# exception-class import only (this service never constructs a
# MailCampaignService instance), so callers/routes that already catch it
# for other campaign routes need no second except-branch for this one.
from app.services.mail_campaign_service import MailCampaignNotFound


class MailCampaignMailboxNextSendService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        channel_store: MailCampaignMailboxStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
    ):
        self.campaign_store = campaign_store
        self.channel_store = channel_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store

    async def get_next_send_by_mailbox(self, mail_campaign_id: str) -> list[MailCampaignMailboxNextSend]:
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)

        mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id)
        if not mailbox_ids:
            return []

        if campaign.status != MailCampaignStatus.ACTIVE:
            return [
                MailCampaignMailboxNextSend(mail_campaign_id=mail_campaign_id, mailbox_id=mailbox_id, next_send_at=None)
                for mailbox_id in mailbox_ids
            ]

        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        assigned_mailbox_by_enrollment = {e.enrollment_id: e.assigned_mailbox_id for e in enrollments}

        steps = await self.enrollment_step_store.list_for_campaign(mail_campaign_id)
        earliest_by_mailbox: dict[str, datetime] = {}
        for step in steps:
            if step.status not in WAITING_STEP_STATUSES:
                continue
            # next_send_at is the real scheduled/resolved time for QUEUED
            # and CLAIMED steps (CLAIMED preserves the next_send_at it
            # had while QUEUED -- see MailSendingService's QUEUED->CLAIMED
            # transition). eligible_at is a defensive fallback for a
            # PENDING step that hasn't had a legal send window resolved
            # yet -- a real lower bound, never fabricated.
            candidate_time = step.next_send_at if step.next_send_at is not None else step.eligible_at
            if candidate_time is None:
                continue
            mailbox_id = attribute_step_to_mailbox(assigned_mailbox_by_enrollment.get(step.enrollment_id), mailbox_ids)
            if mailbox_id is None:
                continue
            current = earliest_by_mailbox.get(mailbox_id)
            if current is None or candidate_time < current:
                earliest_by_mailbox[mailbox_id] = candidate_time

        return [
            MailCampaignMailboxNextSend(
                mail_campaign_id=mail_campaign_id,
                mailbox_id=mailbox_id,
                next_send_at=earliest_by_mailbox.get(mailbox_id),
            )
            for mailbox_id in mailbox_ids
        ]
