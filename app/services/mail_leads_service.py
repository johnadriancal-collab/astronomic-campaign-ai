"""
MailLeadsService -- Leads V1 (2026-09-17). A pure read-side aggregation:
one row per CRM contact who has at least one REAL MailEnrollment,
gathered across every Astronomic Mail campaign they've ever been part
of. No new persistence, no second "Lead" record -- a "Lead" here is
purely a Campaign-Manager-shaped VIEW of an existing CrmContact, built
entirely from MailEnrollment/MailEnrollmentStep/MailCampaign/MailReply
rows that already exist. This deliberately does NOT touch CrmContact
itself, CRM Engagement Stage, or anything the legacy Apollo `Lead`/
`CampaignLead` system (app/api/leads.py) owns -- two fully separate
systems that happen to share the English word "lead."

V1 pilot scale, same stance as MailInboxService: every read here loops
`MailCampaignStore.list()` and calls the EXISTING per-campaign
`list_for_campaign()` on MailEnrollmentStore/MailEnrollmentStepStore --
no new store methods, no new indexes, nothing added to any store's
public interface. At today's data volume (a handful of campaigns, low
hundreds of enrollments) this is a full but cheap scan; it is not
recommended to leave un-revisited if this app's campaign count grows
into the thousands.
"""

from collections import defaultdict
from datetime import datetime

from app.models.crm import CrmContact
from app.models.mail import (
    MailCampaign,
    MailEnrollment,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailLeadCampaignHistoryEntry,
    MailLeadDetail,
    MailLeadListItem,
    MailLeadPage,
    MailLeadStepSummary,
    MailReply,
)
from app.repositories.crm_contact_store import CrmContactStore
from app.repositories.mail_campaign_store import MailCampaignStore
from app.repositories.mail_enrollment_step_store import MailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MailEnrollmentStore
from app.repositories.mail_reply_store import MailReplyStore
from app.repositories.mailbox_store import MailboxStore

MailLeadSortBy = str  # "name" | "last_activity" | "last_campaign" -- validated at the API layer


class _ContactAggregate:
    """Internal-only scratch structure -- never returned to a caller.
    One per crm_contact_id, built while walking every campaign once."""

    __slots__ = ("enrollments",)

    def __init__(self) -> None:
        self.enrollments: list[tuple[MailEnrollment, MailCampaign]] = []


class MailLeadsService:
    def __init__(
        self,
        campaign_store: MailCampaignStore,
        enrollment_store: MailEnrollmentStore,
        enrollment_step_store: MailEnrollmentStepStore,
        contact_store: CrmContactStore,
        reply_store: MailReplyStore,
        mailbox_store: MailboxStore,
    ):
        self.campaign_store = campaign_store
        self.enrollment_store = enrollment_store
        self.enrollment_step_store = enrollment_step_store
        self.contact_store = contact_store
        self.reply_store = reply_store
        self.mailbox_store = mailbox_store

    async def _load_all(
        self,
    ) -> tuple[
        dict[str, list[tuple[MailEnrollment, MailCampaign]]],
        dict[str, list[MailEnrollmentStep]],
        dict[str, MailReply],
    ]:
        """One full pass over every campaign's enrollments/steps, shared
        by list_leads() and get_lead_detail() so neither duplicates the
        scan. Returns (enrollments_by_contact, steps_by_enrollment,
        replies_by_enrollment)."""
        campaigns = await self.campaign_store.list()

        enrollments_by_contact: dict[str, list[tuple[MailEnrollment, MailCampaign]]] = defaultdict(list)
        steps_by_enrollment: dict[str, list[MailEnrollmentStep]] = {}

        for campaign in campaigns:
            enrollments = await self.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
            for enrollment in enrollments:
                enrollments_by_contact[enrollment.crm_contact_id].append((enrollment, campaign))

            steps = await self.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
            for step in steps:
                steps_by_enrollment.setdefault(step.enrollment_id, []).append(step)

        replies = await self.reply_store.list_all()
        replies_by_enrollment = {reply.enrollment_id: reply for reply in replies}

        return dict(enrollments_by_contact), steps_by_enrollment, replies_by_enrollment

    def _last_activity(
        self,
        contact_enrollments: list[tuple[MailEnrollment, MailCampaign]],
        steps_by_enrollment: dict[str, list[MailEnrollmentStep]],
    ) -> datetime:
        """Latest of: every step's sent_at across every enrollment, every
        enrollment's replied_at, or (if genuinely nothing else exists
        yet) the most recent enrolled_at -- never a fabricated
        timestamp. MailEnrollment has no updated_at of its own, so this
        is the most truthful "last touched" signal available."""
        candidates: list[datetime] = []
        for enrollment, _campaign in contact_enrollments:
            if enrollment.replied_at is not None:
                candidates.append(enrollment.replied_at)
            for step in steps_by_enrollment.get(enrollment.enrollment_id, []):
                if step.sent_at is not None:
                    candidates.append(step.sent_at)
        if candidates:
            return max(candidates)
        return max(enrollment.enrolled_at for enrollment, _campaign in contact_enrollments)

    def _contact_name(self, contact: CrmContact | None) -> str | None:
        if contact is None:
            return None
        return " ".join(part for part in [contact.first_name, contact.last_name] if part) or None

    async def list_leads(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        campaign_id: str | None = None,
        replied: bool | None = None,
        sort_by: MailLeadSortBy = "last_activity",
        sort_dir: str = "desc",
        page: int = 1,
        page_size: int = 50,
    ) -> MailLeadPage:
        """Pure read, computed fresh on every call -- never cached.
        Filtering/sorting/pagination all happen in Python over the full
        aggregated set (V1 pilot scale, same stance as
        CrmService.list_contacts's plain scan) -- there is no DB-level
        query here to push these down into."""
        enrollments_by_contact, steps_by_enrollment, replies_by_enrollment = await self._load_all()

        contact_ids = list(enrollments_by_contact.keys())
        contacts = await self.contact_store.list_by_ids(contact_ids)
        contacts_by_id = {c.crm_contact_id: c for c in contacts}

        items: list[MailLeadListItem] = []
        for crm_contact_id, contact_enrollments in enrollments_by_contact.items():
            contact = contacts_by_id.get(crm_contact_id)
            if contact is None:
                # Defensive only -- CrmContact is archived, never hard-deleted.
                continue

            sorted_enrollments = sorted(contact_enrollments, key=lambda pair: pair[0].enrolled_at, reverse=True)
            most_recent_enrollment, most_recent_campaign = sorted_enrollments[0]
            distinct_campaign_ids = {campaign.mail_campaign_id for _e, campaign in contact_enrollments}
            has_replied = any(e.replied_at is not None for e, _c in contact_enrollments)

            items.append(
                MailLeadListItem(
                    crm_contact_id=crm_contact_id,
                    name=self._contact_name(contact),
                    email=contact.email or most_recent_enrollment.email_at_enrollment,
                    company=contact.company,
                    title=contact.title,
                    status=most_recent_enrollment.status,
                    last_campaign_id=most_recent_campaign.mail_campaign_id,
                    last_campaign_name=most_recent_campaign.name,
                    campaigns_count=len(distinct_campaign_ids),
                    replied=has_replied,
                    last_activity_at=self._last_activity(contact_enrollments, steps_by_enrollment),
                )
            )

        if q:
            needle = q.strip().lower()
            items = [
                item
                for item in items
                if needle
                in " ".join(
                    filter(None, [item.name, item.email, item.company, item.title, item.last_campaign_name])
                ).lower()
            ]

        if status:
            items = [item for item in items if item.status.value == status]

        if campaign_id:
            allowed_ids = {
                crm_contact_id
                for crm_contact_id, contact_enrollments in enrollments_by_contact.items()
                if any(campaign.mail_campaign_id == campaign_id for _e, campaign in contact_enrollments)
            }
            items = [item for item in items if item.crm_contact_id in allowed_ids]

        if replied is not None:
            items = [item for item in items if item.replied == replied]

        reverse = sort_dir != "asc"
        if sort_by == "name":
            items.sort(key=lambda item: (item.name or item.email or "").lower(), reverse=reverse)
        elif sort_by == "last_campaign":
            items.sort(key=lambda item: item.last_campaign_name.lower(), reverse=reverse)
        else:
            items.sort(key=lambda item: item.last_activity_at, reverse=reverse)

        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]

        return MailLeadPage(items=page_items, total=total, page=page, page_size=page_size)

    async def get_lead_detail(self, crm_contact_id: str) -> MailLeadDetail | None:
        """Returns None if this contact has zero enrollments anywhere --
        the exact "not a Lead" case the API layer maps to 404."""
        contact = await self.contact_store.get(crm_contact_id)
        if contact is None:
            return None

        enrollments_by_contact, steps_by_enrollment, replies_by_enrollment = await self._load_all()
        contact_enrollments = enrollments_by_contact.get(crm_contact_id, [])
        if not contact_enrollments:
            return None

        sorted_enrollments = sorted(contact_enrollments, key=lambda pair: pair[0].enrolled_at, reverse=True)

        history: list[MailLeadCampaignHistoryEntry] = []
        replies_count = 0
        for enrollment, campaign in sorted_enrollments:
            steps = sorted(
                steps_by_enrollment.get(enrollment.enrollment_id, []), key=lambda s: s.step_number
            )
            step_summaries = [
                MailLeadStepSummary(
                    step_number=step.step_number,
                    status=step.status,
                    subject=step.rendered_subject or step.subject,
                    sent_at=step.sent_at,
                    last_error=step.last_error,
                )
                for step in steps
            ]

            mailbox_email = None
            if enrollment.assigned_mailbox_id:
                mailbox = await self.mailbox_store.get(enrollment.assigned_mailbox_id)
                mailbox_email = mailbox.email if mailbox is not None else None

            reply = replies_by_enrollment.get(enrollment.enrollment_id)
            if reply is not None:
                replies_count += 1

            history.append(
                MailLeadCampaignHistoryEntry(
                    mail_campaign_id=campaign.mail_campaign_id,
                    campaign_name=campaign.name,
                    campaign_status=campaign.status,
                    enrollment_id=enrollment.enrollment_id,
                    enrollment_status=enrollment.status,
                    mailbox_email=mailbox_email,
                    enrolled_at=enrollment.enrolled_at,
                    replied_at=enrollment.replied_at,
                    has_reply=reply is not None,
                    reply_enrollment_id=enrollment.enrollment_id if reply is not None else None,
                    steps=step_summaries,
                )
            )

        distinct_campaign_ids = {campaign.mail_campaign_id for _e, campaign in contact_enrollments}
        most_recent_enrollment, _most_recent_campaign = sorted_enrollments[0]

        return MailLeadDetail(
            crm_contact_id=crm_contact_id,
            name=self._contact_name(contact),
            email=contact.email or most_recent_enrollment.email_at_enrollment,
            company=contact.company,
            title=contact.title,
            status=most_recent_enrollment.status,
            campaigns_count=len(distinct_campaign_ids),
            replies_count=replies_count,
            first_campaign_at=min(e.enrolled_at for e, _c in contact_enrollments),
            last_activity_at=self._last_activity(contact_enrollments, steps_by_enrollment),
            campaign_history=history,
        )
