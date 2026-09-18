"""
MailboxMetricsService -- real per-mailbox metrics for the Emails page
(2026-09-18). Answers, for every mailbox, three questions that page's
table previously hardcoded to 0:

  - campaigns_count: how many distinct campaigns use this mailbox as a
    sending channel, across every lifecycle state (draft/ready/active/
    paused/completed/archived)?
  - emails_sent_today: how many SENT steps went out from this mailbox
    since UTC midnight?
  - queue_count: how many not-yet-sent steps are currently waiting to
    send from this mailbox?

Read-only, no new persistence -- reuses MailCampaignStore.list(),
MailCampaignMailboxStore.list_mailbox_ids_for_campaign(),
MailEnrollmentStore.list_for_campaign(), MailEnrollmentStepStore.
list_for_campaign()/count_sent_for_mailbox_since(), the same store
methods MailCampaignListService/MailCampaignMailboxNextSendService
already use -- same V1 pilot-scale, loop-and-aggregate-in-Python stance
as every other read service this session. One pass over all campaigns
(three store calls per campaign), not a per-mailbox loop over campaigns.

Queue counting is deliberately conservative -- see
app/services/mail_step_mailbox_attribution.py's attribute_step_to_mailbox()
for exactly which steps get attributed to which mailbox and why (the
SAME logic MailCampaignMailboxNextSendService uses for its own proactive
OAuth expiration warnings, so the two features never drift apart on
this). Deliverability Index has no field here: zero real signal exists
anywhere in this codebase for Astronomic Mail (see
frontend/lib/mailboxes.ts's own DELIVERABILITY_TOOLTIP), so the frontend
keeps showing "Not available" rather than this service fabricating a
score.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.services.mail_step_mailbox_attribution import WAITING_STEP_STATUSES, attribute_step_to_mailbox


def _utc_day_start(at: datetime) -> datetime:
    """Same UTC-midnight convention as mail_sending_service._utc_day_start
    -- kept as its own one-line copy rather than importing a private
    helper across services."""
    return at.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class MailboxMetrics:
    campaigns_count: int
    emails_sent_today: int
    queue_count: int


class MailboxMetricsService:
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

    async def compute_for_mailboxes(self, mailbox_ids: list[str], now: datetime) -> dict[str, MailboxMetrics]:
        campaigns_by_mailbox: dict[str, set[str]] = {}
        queue_by_mailbox: dict[str, int] = {}

        campaigns = await self.campaign_store.list()
        for campaign in campaigns:
            channel_mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(campaign.mail_campaign_id)
            if not channel_mailbox_ids:
                continue

            for mailbox_id in channel_mailbox_ids:
                campaigns_by_mailbox.setdefault(mailbox_id, set()).add(campaign.mail_campaign_id)

            enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
            assigned_mailbox_by_enrollment = {e.enrollment_id: e.assigned_mailbox_id for e in enrollments}

            steps = await self.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
            for step in steps:
                if step.status not in WAITING_STEP_STATUSES:
                    continue
                mailbox_id = attribute_step_to_mailbox(
                    assigned_mailbox_by_enrollment.get(step.enrollment_id), channel_mailbox_ids
                )
                if mailbox_id is not None:
                    queue_by_mailbox[mailbox_id] = queue_by_mailbox.get(mailbox_id, 0) + 1

        day_start = _utc_day_start(now)
        result: dict[str, MailboxMetrics] = {}
        for mailbox_id in mailbox_ids:
            emails_sent_today = await self.enrollment_step_store.count_sent_for_mailbox_since(mailbox_id, day_start)
            result[mailbox_id] = MailboxMetrics(
                campaigns_count=len(campaigns_by_mailbox.get(mailbox_id, set())),
                emails_sent_today=emails_sent_today,
                queue_count=queue_by_mailbox.get(mailbox_id, 0),
            )
        return result
