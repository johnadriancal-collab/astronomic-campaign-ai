"""
MailInboxService -- Inbox V1 (2026-09-17) + V2 reply-body reading
(2026-09-17). A single, minimal read path that unifies MailReply rows
across every Astronomic Mail campaign into MailInboxReplyView rows, plus
an on-demand Gmail body fetch for one reply at a time. Deliberately its
own small service rather than another method bolted onto
MailCampaignService's already-large constructor: this needs exactly six
existing stores plus MailboxService (for token refresh only), none of
which are mutated by list_replies(), and nothing about it depends on (or
should touch) MailCampaignService's own state-machine/DRAFT-READY-ACTIVE
logic.

No new reply state, no second reply model -- MailReply (written only by
MailSendingService.mark_enrollment_replied()) remains the single source
of truth. list_replies() only joins it against MailEnrollment,
MailCampaign, CrmContact, Mailbox, and the replied enrollment's own
MailEnrollmentStep rows for display. get_reply_body() never persists
anything -- every call re-fetches from Gmail; there is no body cache
anywhere in this codebase to go stale or leak."""

from app.google.gmail_message_body_client import GmailMessageBodyClient, extract_best_body
from app.google.gmail_thread_reader_client import GmailReadError, GmailReadNotFoundError
from app.google.oauth_client import GMAIL_READONLY_SCOPE, GoogleRefreshTokenInvalidError
from app.models.mail import MailEnrollmentStepStatus, MailInboxReplyBody, MailInboxReplyView
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_reply_store import MailReplyStore
from app.repositories.mailbox_store import MailboxStore
from app.services.mailbox_service import MailboxCredentialMissingError, MailboxNotFound, MailboxService


class MailInboxService:
    def __init__(
        self,
        reply_store: MailReplyStore,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        contact_store: CrmContactStore,
        mailbox_store: MailboxStore,
        mailbox_service: MailboxService,
        body_client: GmailMessageBodyClient | None = None,
    ):
        self.reply_store = reply_store
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.contact_store = contact_store
        self.mailbox_store = mailbox_store
        self.mailbox_service = mailbox_service
        self.body_client = body_client or GmailMessageBodyClient()

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

    async def get_reply_body(self, enrollment_id: str) -> MailInboxReplyBody:
        """On-demand only -- never called from list_replies(), never
        persisted. Anchored ENTIRELY to this one enrollment's existing
        MailReply row: fetches exactly `reply.gmail_message_id` and
        nothing else, so this method cannot be used to read any Gmail
        message Campaign Manager doesn't already know is a reply to one
        of its own campaigns (see GmailMessageBodyClient's own docstring
        -- it has no listing/search capability at all).

        Every return is `status="ok"` or one of the expected,
        handleable non-ok statuses (see MailInboxReplyBody's own
        docstring) -- this method never raises for an ordinary "can't
        show the body right now" outcome; the Inbox list itself must
        stay usable regardless of what happens here."""
        reply = await self.reply_store.get(enrollment_id)
        if reply is None:
            return MailInboxReplyBody(status="not_found", message="No reply is recorded for this enrollment.")

        mailbox = await self.mailbox_store.get(reply.mailbox_id)
        if mailbox is None or GMAIL_READONLY_SCOPE not in mailbox.granted_scopes:
            return MailInboxReplyBody(
                status="scope_missing",
                message="This mailbox hasn't granted reply-content viewing yet. Reconnect it to enable this.",
            )

        try:
            access_token = await self.mailbox_service.refresh_mailbox_access_token(reply.mailbox_id)
        except GoogleRefreshTokenInvalidError:
            return MailInboxReplyBody(
                status="needs_reauth",
                message="Reconnect this mailbox to enable reply-content viewing.",
            )
        except (MailboxNotFound, MailboxCredentialMissingError):
            return MailInboxReplyBody(status="scope_missing", message="This mailbox is not fully connected.")

        try:
            message = await self.body_client.get_message_full(access_token=access_token, message_id=reply.gmail_message_id)
        except GmailReadNotFoundError:
            return MailInboxReplyBody(status="not_found", message="Gmail no longer has this specific message.")
        except GmailReadError as e:
            return MailInboxReplyBody(status="provider_error", message=f"Couldn't reach Gmail: {type(e).__name__}.")

        extracted = extract_best_body(message)
        if extracted is None:
            return MailInboxReplyBody(status="ok", body_text="", body_source="plain")
        body_text, body_source = extracted
        return MailInboxReplyBody(status="ok", body_text=body_text, body_source=body_source)
