"""
MailCampaignStatsService -- campaign detail stats strip (2026-09-17).

Investigation this is built on (Open rate / Reply rate / Unsub rate /
Bounce rate, requested for the campaign Dashboard tab):

- Opens: NO tracking exists anywhere for Astronomic Mail (a separate,
  disabled Apollo integration has its own open/click sync -- see
  app/services/email_message_sync_service.py -- but nothing here reads
  it, and this codebase's own AI-assistant guardrails explicitly say to
  never claim open/click/bounce tracking for Astronomic Mail; see
  app/services/astro_ai_service.py). No field for it exists on this
  model at all -- the frontend renders "Not tracked" as static copy.
- Bounces: no provider bounce webhook exists anywhere. MailSuppression
  Reason.HARD_BOUNCE is a real enum value but nothing ever sets it
  automatically -- it would only ever be a manual entry. Conflating
  MailEnrollmentStepStatus.FAILED (OUR OWN send attempt failing, before
  or during the provider call) with an actual bounce would be wrong --
  those are different events. No field for it exists on this model
  either -- also "Not tracked", static copy, frontend-side.
- Replies: real and reliable -- MailReply/MailEnrollment.status ==
  REPLIED, set exclusively by MailSendingService.mark_enrollment_
  replied(). Numerator/denominator convention matches
  MailCampaignListItem.progress_percent exactly (replied / total * 100,
  1 decimal, 0.0 when total == 0).
- Unsubscribes: real, but requires a join no existing store method
  provides -- MailEnrollment carries no suppression-reason field, and
  MailCampaignWorkload.suppressed lumps every MailSuppressionReason
  together. This service computes the UNSUBSCRIBED-only count itself,
  by cross-referencing each enrollment's frozen email_at_enrollment
  (normalized the same way mark_ready() does) against active
  MailSuppression rows filtered to reason == UNSUBSCRIBED specifically.

V1 pilot scale, same stance as every other read service this session:
loops MailCampaignStore.get() + MailEnrollmentStore.list_for_campaign()
and MailSuppressionStore.list() (already-existing methods) rather than
adding new store methods for this one read.
"""

from app.models.crm import normalize_email
from app.models.mail import MailCampaignStats, MailEnrollmentStatus, MailSuppressionReason
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_suppression_store import MailSuppressionStore

# Reuses MailCampaignService's own MailCampaignNotFound -- see
# MailCampaignMailboxNextSendService's identical precedent/comment for why
# this is a lightweight exception-class import only.
from app.services.mail_campaign_service import MailCampaignNotFound


class MailCampaignStatsService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        suppression_store: MailSuppressionStore,
    ):
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.suppression_store = suppression_store

    async def get_stats(self, mail_campaign_id: str) -> MailCampaignStats:
        campaign = await self.campaign_store.get(mail_campaign_id)
        if campaign is None:
            raise MailCampaignNotFound(mail_campaign_id)

        enrollments = await self.enrollment_store.list_for_campaign(mail_campaign_id)
        total = len(enrollments)
        replied = sum(1 for e in enrollments if e.status == MailEnrollmentStatus.REPLIED)
        reply_rate_percent = round((replied / total) * 100, 1) if total > 0 else 0.0

        suppressions = await self.suppression_store.list()
        unsubscribed_emails = {
            s.email_normalized for s in suppressions if s.active and s.reason == MailSuppressionReason.UNSUBSCRIBED
        }
        unsubscribed = sum(
            1 for e in enrollments if normalize_email(e.email_at_enrollment) in unsubscribed_emails
        )
        unsub_rate_percent = round((unsubscribed / total) * 100, 1) if total > 0 else 0.0

        return MailCampaignStats(
            mail_campaign_id=mail_campaign_id,
            total=total,
            replied=replied,
            reply_rate_percent=reply_rate_percent,
            unsubscribed=unsubscribed,
            unsub_rate_percent=unsub_rate_percent,
        )
