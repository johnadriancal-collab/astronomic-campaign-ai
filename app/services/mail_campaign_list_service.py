"""
MailCampaignListService -- Campaigns list V1 (2026-09-17). A wide,
table-shaped read model over every existing campaign, built purely from
data that already exists (MailCampaign + MailEnrollment +
MailEnrollmentStep + MailSequenceStep + the campaign's channel
mailboxes). No new persistence, no duplicated analytics state -- the
per-enrollment-status counting here is the EXACT same computation
MailCampaignService.get_workload() already does (this file does not
call that method directly, to avoid depending on MailCampaignService's
much larger constructor for a handful of enrollment-status counts, but
performs the identical bucketing over the same MailEnrollmentStore rows).

V1 pilot scale, same stance as MailInboxService/MailLeadsService: every
read here loops MailCampaignStore.list() and calls the EXISTING
per-campaign list_for_campaign() on MailEnrollmentStore/
MailEnrollmentStepStore/MailSequenceStepStore -- no new store methods.
"""

from app.models.mail import (
    MailCampaign,
    MailCampaignListItem,
    MailCampaignListPage,
    MailEnrollmentStatus,
    MailEnrollmentStepStatus,
)
from app.repositories.mail_campaign_mailbox_store import MailCampaignMailboxStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_sequence_step_store import MailSequenceStepStore
from app.repositories.mailbox_store import MailboxStore

MailCampaignSortBy = str  # "name" | "status" | "total_leads" | "replied" | "progress" | "updated_at"


class MailCampaignListService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        sequence_step_store: MailSequenceStepStore,
        channel_store: MailCampaignMailboxStore,
        mailbox_store: MailboxStore,
    ):
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.sequence_step_store = sequence_step_store
        self.channel_store = channel_store
        self.mailbox_store = mailbox_store

    async def _build_item(self, campaign: MailCampaign) -> MailCampaignListItem:
        enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
        counts = {status: 0 for status in MailEnrollmentStatus}
        for enrollment in enrollments:
            counts[enrollment.status] += 1
        total = len(enrollments)

        terminal = (
            counts[MailEnrollmentStatus.COMPLETED]
            + counts[MailEnrollmentStatus.REPLIED]
            + counts[MailEnrollmentStatus.SUPPRESSED]
            + counts[MailEnrollmentStatus.FAILED]
        )
        progress_percent = round((terminal / total) * 100, 1) if total > 0 else 0.0

        steps = await self.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
        sent_count = sum(1 for step in steps if step.status == MailEnrollmentStepStatus.SENT)

        sequence_steps = await self.sequence_step_store.list_for_campaign(campaign.mail_campaign_id)

        mailbox_ids = await self.channel_store.list_mailbox_ids_for_campaign(campaign.mail_campaign_id)
        mailbox_id = mailbox_ids[0] if mailbox_ids else None
        mailbox_email = None
        if mailbox_id:
            mailbox = await self.mailbox_store.get(mailbox_id)
            mailbox_email = mailbox.email if mailbox is not None else None

        return MailCampaignListItem(
            mail_campaign_id=campaign.mail_campaign_id,
            name=campaign.name,
            status=campaign.status,
            mailbox_id=mailbox_id,
            mailbox_email=mailbox_email,
            mailbox_count=len(mailbox_ids),
            total_leads=total,
            sent=sent_count,
            replied=counts[MailEnrollmentStatus.REPLIED],
            suppressed=counts[MailEnrollmentStatus.SUPPRESSED],
            failed=counts[MailEnrollmentStatus.FAILED],
            completed=counts[MailEnrollmentStatus.COMPLETED],
            progress_percent=progress_percent,
            step_count=len(sequence_steps),
            created_at=campaign.created_at,
            updated_at=campaign.updated_at,
        )

    async def list_campaigns(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        mailbox_email: str | None = None,
        sort_by: MailCampaignSortBy = "updated_at",
        sort_dir: str = "desc",
        page: int = 1,
        page_size: int = 25,
    ) -> MailCampaignListPage:
        """Pure read, computed fresh on every call -- never cached.
        Filtering/sorting/pagination happen in Python over the full set
        (V1 pilot scale, same stance as CrmService.list_contacts's plain
        scan). Historical/archived campaigns are never excluded -- this
        endpoint deliberately has no "hide archived" default; that's a
        later, separate decision (see this module's own module
        docstring)."""
        campaigns = await self.campaign_store.list()
        items = [await self._build_item(campaign) for campaign in campaigns]

        if q:
            needle = q.strip().lower()
            items = [item for item in items if needle in item.name.lower()]

        if status:
            items = [item for item in items if item.status.value == status]

        if mailbox_email:
            items = [item for item in items if item.mailbox_email == mailbox_email]

        reverse = sort_dir != "asc"
        if sort_by == "name":
            items.sort(key=lambda item: item.name.lower(), reverse=reverse)
        elif sort_by == "status":
            items.sort(key=lambda item: item.status.value, reverse=reverse)
        elif sort_by == "total_leads":
            items.sort(key=lambda item: item.total_leads, reverse=reverse)
        elif sort_by == "replied":
            items.sort(key=lambda item: item.replied, reverse=reverse)
        elif sort_by == "progress":
            items.sort(key=lambda item: item.progress_percent, reverse=reverse)
        else:
            items.sort(key=lambda item: item.updated_at, reverse=reverse)

        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]

        return MailCampaignListPage(items=page_items, total=total, page=page, page_size=page_size)
