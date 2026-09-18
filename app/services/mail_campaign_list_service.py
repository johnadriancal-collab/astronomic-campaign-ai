"""
MailCampaignListService -- Campaigns list V1 (2026-09-17), sequence-
completion progress redefinition (2026-09-18b), Available redefinition
(2026-09-18c). A wide, table-shaped read model over every existing
campaign, built purely from data that already exists (MailCampaign +
MailEnrollment + MailSequenceStep). No new persistence, no duplicated
analytics state.

2026-09-18c: `available_leads` now means the SAME thing as
`in_progress_leads` -- "has NOT finished the sequence" (total_leads -
finished_leads), replacing the earlier "Step 1 hasn't been sent yet"
definition. Available and Progress are intentionally the SAME
finished-sequence semantics now (colored bar = finished, Available
count = not finished) -- kept as two separate response fields
(`available_leads` for the Available column/sort, `in_progress_leads`
for the Progress tooltip) purely so each column's own name stays
self-explanatory, not because they're computed differently. Because
`finished_leads` is strictly MailEnrollmentStatus.COMPLETED (see
MailCampaignListItem's own docstring), a REPLIED/SUPPRESSED/FAILED
enrollment that stopped before finishing every step counts as
Available/in-progress -- intentional under the current Progress
definition, not a gap to silently patch here.

Open rate (2026-09-18, open tracking) re-adds a per-campaign read of
MailEnrollmentStepStore -- ONLY to compute "leads with >=1 SENT email,"
Open rate's denominator; `available_leads`/`in_progress_leads`/
`finished_leads` above never touch step data at all. `open_rate_percent`
is None whenever `campaign.open_tracking_enabled` is False (never a
fabricated 0%) OR when the denominator is zero (no send yet) -- the
frontend distinguishes those two None cases using `open_tracking_enabled`
itself, which is passed through unchanged. Numerator is UNIQUE opened
leads (grouped by `enrollment_id`, never raw pixel-hit count) that also
appear in the SENT-denominator set.

Bounce rate (2026-09-18, bounce detection) is computed with the exact
SAME shape as Open rate -- unique bounced leads (grouped by
`enrollment_id`, from MailBounceStore.list_for_campaign()) intersected
with the SAME sent-denominator set already built for Open rate above --
but with only ONE None case (no sent denominator yet), since bounce
tracking has no per-campaign toggle at all. This MUST stay identical to
MailCampaignStatsService.get_stats()'s own bounce-rate formula so the
Campaigns list and the Campaign Dashboard can never disagree.

`reply_rate_percent` matches MailCampaignStatsService.get_stats()'s own
formula exactly. The Mailbox column (mailbox_id/mailbox_email/
mailbox_count) and the raw `sent`/`completed` counts were dropped from
this read model (2026-09-18) -- Mailbox is a Channels-tab concept now.

V1 pilot scale, same stance as MailInboxService/MailLeadsService: every
read here loops MailCampaignStore.list() and calls the EXISTING
per-campaign list_for_campaign() on MailEnrollmentStore/
MailEnrollmentStepStore/MailSequenceStepStore/MailOpenEventStore -- no
new store methods.
"""

from app.models.mail import (
    MailCampaign,
    MailCampaignListItem,
    MailCampaignListPage,
    MailEnrollmentStatus,
    MailEnrollmentStepStatus,
)
from app.repositories.mail_bounce_store import MailBounceStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_open_event_store import MailOpenEventStore
from app.repositories.mail_sequence_step_store import MailSequenceStepStore

MailCampaignSortBy = str  # "name" | "status" | "available" | "total_leads" | "progress" | "reply_rate" | "open_rate" | "bounce_rate" | "replied" | "created_at" | "updated_at"


class MailCampaignListService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        open_event_store: MailOpenEventStore,
        sequence_step_store: MailSequenceStepStore,
        bounce_store: MailBounceStore,
    ):
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.open_event_store = open_event_store
        self.sequence_step_store = sequence_step_store
        self.bounce_store = bounce_store

    async def _build_item(self, campaign: MailCampaign) -> MailCampaignListItem:
        enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
        counts = {status: 0 for status in MailEnrollmentStatus}
        for enrollment in enrollments:
            counts[enrollment.status] += 1
        total = len(enrollments)

        replied = counts[MailEnrollmentStatus.REPLIED]
        reply_rate_percent = round((replied / total) * 100, 1) if total > 0 else 0.0

        # FINISHED = the sequence ran to actual completion -- exactly
        # MailEnrollmentStatus.COMPLETED, never REPLIED/SUPPRESSED/FAILED
        # (each of those is also terminal, but represents the sequence
        # being cut short before every applicable step ran; see
        # MailCampaignListItem's own docstring for the full reasoning).
        # available_leads and in_progress_leads are now the SAME value
        # -- both "not finished" -- see this module's own docstring.
        finished = counts[MailEnrollmentStatus.COMPLETED]
        not_finished = total - finished
        progress_percent = round((finished / total) * 100, 1) if total > 0 else 0.0

        # Sent-denominator set: shared, identically, by Open rate (only
        # when tracking is on) and Bounce rate (always) -- see this
        # module's own docstring for why they MUST share it.
        steps = await self.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
        sent_enrollment_ids = {step.enrollment_id for step in steps if step.status == MailEnrollmentStepStatus.SENT}

        open_rate_percent = None
        if campaign.open_tracking_enabled and sent_enrollment_ids:
            open_events = await self.open_event_store.list_for_campaign(campaign.mail_campaign_id)
            opened_enrollment_ids = {event.enrollment_id for event in open_events}
            unique_opens = len(opened_enrollment_ids & sent_enrollment_ids)
            open_rate_percent = round((unique_opens / len(sent_enrollment_ids)) * 100, 1)

        bounce_rate_percent = None
        if sent_enrollment_ids:
            bounces = await self.bounce_store.list_for_campaign(campaign.mail_campaign_id)
            bounced_enrollment_ids = {bounce.enrollment_id for bounce in bounces}
            unique_bounces = len(bounced_enrollment_ids & sent_enrollment_ids)
            bounce_rate_percent = round((unique_bounces / len(sent_enrollment_ids)) * 100, 1)

        sequence_steps = await self.sequence_step_store.list_for_campaign(campaign.mail_campaign_id)

        return MailCampaignListItem(
            mail_campaign_id=campaign.mail_campaign_id,
            name=campaign.name,
            status=campaign.status,
            total_leads=total,
            available_leads=not_finished,
            open_tracking_enabled=campaign.open_tracking_enabled,
            open_rate_percent=open_rate_percent,
            replied=replied,
            reply_rate_percent=reply_rate_percent,
            suppressed=counts[MailEnrollmentStatus.SUPPRESSED],
            failed=counts[MailEnrollmentStatus.FAILED],
            bounce_rate_percent=bounce_rate_percent,
            finished_leads=finished,
            in_progress_leads=not_finished,
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

        reverse = sort_dir != "asc"
        if sort_by == "name":
            items.sort(key=lambda item: item.name.lower(), reverse=reverse)
        elif sort_by == "status":
            items.sort(key=lambda item: item.status.value, reverse=reverse)
        elif sort_by == "available":
            items.sort(key=lambda item: item.available_leads, reverse=reverse)
        elif sort_by == "total_leads":
            items.sort(key=lambda item: item.total_leads, reverse=reverse)
        elif sort_by == "reply_rate":
            items.sort(key=lambda item: item.reply_rate_percent, reverse=reverse)
        elif sort_by == "open_rate":
            # None (tracking disabled or no sent denominator yet) sorts
            # as -1 -- always last on a descending sort, never mixed in
            # among real percentages.
            items.sort(key=lambda item: item.open_rate_percent if item.open_rate_percent is not None else -1, reverse=reverse)
        elif sort_by == "bounce_rate":
            # Same None-sorts-last convention as open_rate above.
            items.sort(key=lambda item: item.bounce_rate_percent if item.bounce_rate_percent is not None else -1, reverse=reverse)
        elif sort_by == "replied":
            items.sort(key=lambda item: item.replied, reverse=reverse)
        elif sort_by == "progress":
            items.sort(key=lambda item: item.progress_percent, reverse=reverse)
        elif sort_by == "created_at":
            items.sort(key=lambda item: item.created_at, reverse=reverse)
        else:
            items.sort(key=lambda item: item.updated_at, reverse=reverse)

        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]

        return MailCampaignListPage(items=page_items, total=total, page=page, page_size=page_size)
