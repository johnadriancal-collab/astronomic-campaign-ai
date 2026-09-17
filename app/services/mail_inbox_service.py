"""
MailInboxService -- Inbox V1 (2026-09-17). A single, minimal read path
that unifies MailReply rows across every Astronomic Mail campaign into
MailInboxReplyView rows. Deliberately its own small service rather than
another method bolted onto MailCampaignService's already-large
constructor: this needs exactly six existing stores, none of which are
mutated here, and nothing about it depends on (or should touch)
MailCampaignService's own state-machine/DRAFT-READY-ACTIVE logic.

No new reply state, no second reply model -- MailReply (written only by
MailSendingService.mark_enrollment_replied()) remains the single source
of truth. This service only joins it against MailEnrollment, MailCampaign,
CrmContact, Mailbox, and the replied enrollment's own MailEnrollmentStep
rows for display.
"""

from app.models.mail import MailEnrollmentStepStatus, MailInboxReplyView
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_reply_store import MailReplyStore
from app.repositories.mailbox_store import MailboxStore


class MailInboxService:
    def __init__(
        self,
        reply_store: MailReplyStore,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        contact_store: CrmContactStore,
        mailbox_store: MailboxStore,
    ):
        self.reply_store = reply_store
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.contact_store = contact_store
        self.mailbox_store = mailbox_store

    async def list_replies(self) -> list[MailInboxReplyView]:
        """Pure read, computed fresh on every call -- never cached. A
        MailReply whose enrollment or campaign has since vanished is
        skipped (same defensive stance as
        MailCampaignService.list_execution_steps(): enrollments are
        never hard-deleted and campaigns are only ever archived, never
        deleted, so this should be unreachable in practice -- there is
        no honest fallback value for a MailCampaignStatus that doesn't
        exist, so this row is dropped rather than shown with a
        fabricated status). A missing contact/mailbox never drops the
        row -- only the corresponding display field goes None, so a
        reply is never hidden just because a Contact or mailbox it
        points at was removed."""
        replies = await self.reply_store.list_all()

        views: list[MailInboxReplyView] = []
        for reply in replies:
            enrollment = await self.enrollment_store.get(reply.enrollment_id)
            if enrollment is None:
                continue

            campaign = await self.campaign_store.get(reply.mail_campaign_id)
            if campaign is None:
                continue

            contact = await self.contact_store.get(reply.crm_contact_id)
            mailbox = await self.mailbox_store.get(reply.mailbox_id)
            steps = await self.enrollment_step_store.list_for_enrollment(reply.enrollment_id)

            step1 = next((s for s in steps if s.step_number == 1), None)
            subject = None
            if step1 is not None:
                subject = step1.rendered_subject or step1.subject

            skipped_step_numbers = sorted(
                s.step_number for s in steps if s.status == MailEnrollmentStepStatus.SKIPPED_REPLIED
            )

            contact_name = None
            if contact is not None:
                contact_name = " ".join(part for part in [contact.first_name, contact.last_name] if part) or None

            views.append(
                MailInboxReplyView(
                    enrollment_id=reply.enrollment_id,
                    mail_campaign_id=reply.mail_campaign_id,
                    campaign_name=campaign.name,
                    campaign_status=campaign.status,
                    crm_contact_id=reply.crm_contact_id,
                    contact_name=contact_name,
                    email=reply.reply_email_normalized,
                    mailbox_id=reply.mailbox_id,
                    mailbox_email=mailbox.email if mailbox is not None else None,
                    subject=subject,
                    replied_at=reply.detected_at,
                    gmail_thread_id=reply.gmail_thread_id,
                    gmail_message_id=reply.gmail_message_id,
                    enrollment_status=enrollment.status,
                    skipped_step_numbers=skipped_step_numbers,
                )
            )

        return views
