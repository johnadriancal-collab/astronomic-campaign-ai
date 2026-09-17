"""
MailCampaignMailboxNextSendService -- proactive OAuth expiration warnings
(2026-09-17). Answers exactly one question per campaign: for each mailbox
currently assigned to send it, what is the earliest still-QUEUED send time?
That's the one piece a campaign detail page needs to compare against a
mailbox's own `estimated_expires_at` (from GET /mailboxes -- see
MailboxListItem) to decide whether to show the stronger "reconnect required
before next scheduled send" warning.

Read-only, no new persistence -- reuses MailEnrollmentStepStore.
list_for_campaign() (already used by MailCampaignListService) rather than
adding a new store method, same V1 pilot-scale stance as every other read
service this session.
"""

from app.models.mail import MailCampaignMailboxNextSend, MailEnrollmentStepStatus
from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore

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
        enrollment_step_store: MailEnrollmentStepStore,
    ):
        self.campaign_store = campaign_store
        self.channel_store = channel_store
        self.enrollment_step_store = enrollment_step_store

    async def get_next_send_by_mailbox(self, mail_campaign_id: str) -> list[MailCampaignMailboxNextSend]:
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)

        mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(mail_campaign_id)
        if not mailbox_ids:
            return []

        steps = await self.enrollment_step_store.list_for_campaign(mail_campaign_id)
        queued = [
            step for step in steps if step.status == MailEnrollmentStepStatus.QUEUED and step.next_send_at is not None
        ]

        results = []
        for mailbox_id in mailbox_ids:
            earliest = min(
                (step.next_send_at for step in queued if step.mailbox_id == mailbox_id),
                default=None,
            )
            results.append(
                MailCampaignMailboxNextSend(
                    mail_campaign_id=mail_campaign_id,
                    mailbox_id=mailbox_id,
                    next_send_at=earliest,
                )
            )
        return results
