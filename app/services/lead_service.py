"""
Orchestrates the Lead system: turning a selected Apollo prospect into a
durable Lead exactly once, and composing Lead + CampaignLead + Campaign
data for Manager's read views.

ensure_lead() is the ONLY place a Lead is created -- called from
CampaignService.build() strictly after a prospect's Apollo contact has
been confirmed to exist (see build()'s docstring for why). It is never
called merely because a prospect appeared in a search/ranking result.

claude_score/claude_reason are per-(campaign, lead) facts -- they live on
CampaignLead, not Lead (see models/lead.py). ensure_lead() never touches
them; add_to_campaign() is what records them, once, for that specific
membership. A Lead that resurfaces in a second campaign gets a SECOND
CampaignLead row with its own score/reason -- its global Lead fields
(name, title, company, ...) are never overwritten by the second campaign.
"""

import uuid
from datetime import datetime, timezone

from app.models.campaign import Campaign
from app.models.lead import (
    CampaignLead,
    CampaignLeadStatus,
    CampaignLeadView,
    Lead,
    LeadDetail,
    LeadListItem,
    LeadStatus,
)
from app.repositories.campaign_lead_store import CampaignLeadStore, MemoryCampaignLeadStore
from app.repositories.campaign_store import CampaignStore, MemoryCampaignStore
from app.repositories.lead_store import LeadStore, MemoryLeadStore


class LeadService:
    def __init__(
        self,
        store: LeadStore | None = None,
        campaign_lead_store: CampaignLeadStore | None = None,
        campaign_store: CampaignStore | None = None,
    ):
        self.store = store or MemoryLeadStore()
        self.campaign_lead_store = campaign_lead_store or MemoryCampaignLeadStore()
        self.campaign_store = campaign_store or MemoryCampaignStore()

    async def ensure_lead(self, person: dict, apollo_contact_id: str) -> Lead:
        """
        Returns the existing Lead for this Apollo contact if one exists
        (from this campaign or any other), otherwise creates a new one.
        This -- not email/name matching -- is the entire de-dup mechanism:
        one Lead per apollo_contact_id, ever. Never overwrites an existing
        Lead's fields on a repeat call, even if `person` differs slightly
        (e.g. a re-enrichment) -- that kind of refresh is a deliberate,
        separate, explicit action, not a side effect of appearing in
        another campaign.
        """
        existing = await self.store.get_by_apollo_contact_id(apollo_contact_id)
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc)
        organization = person.get("organization") or {}
        lead = Lead(
            lead_id=str(uuid.uuid4()),
            apollo_contact_id=apollo_contact_id,
            first_name=person.get("first_name"),
            last_name=person.get("last_name") or person.get("last_name_obfuscated"),
            email=person.get("email"),
            title=person.get("title"),
            company=organization.get("name"),
            company_domain=organization.get("primary_domain"),
            status=LeadStatus.NEW,
            created_at=now,
            updated_at=now,
            apollo_snapshot=person,
        )
        await self.store.create(lead)
        return lead

    async def add_to_campaign(
        self,
        campaign_id: str,
        lead_id: str,
        claude_score: float | None = None,
        claude_reason: str | None = None,
    ) -> None:
        """Idempotent: safe to call every time build() runs, even if already a member."""
        await self.campaign_lead_store.add(
            CampaignLead(
                campaign_id=campaign_id,
                lead_id=lead_id,
                status=CampaignLeadStatus.ADDED,
                added_at=datetime.now(timezone.utc),
                claude_score=claude_score,
                claude_reason=claude_reason,
            )
        )

    async def list_with_campaign_counts(self) -> list[LeadListItem]:
        leads = await self.store.list()
        items = []
        for lead in leads:
            memberships = await self.campaign_lead_store.list_for_lead(lead.lead_id)
            items.append(LeadListItem(**lead.model_dump(), campaign_count=len(memberships)))
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def get_detail(self, lead_id: str) -> LeadDetail | None:
        lead = await self.store.get(lead_id)
        if lead is None:
            return None

        memberships = await self.campaign_lead_store.list_for_lead(lead_id)
        campaigns = []
        for membership in memberships:
            campaign: Campaign | None = await self.campaign_store.get(membership.campaign_id)
            if campaign is None:
                continue
            campaigns.append(
                {
                    "campaign_id": campaign.campaign_id,
                    "campaign_name": campaign.plan.campaign_name,
                    "campaign_status": campaign.status,
                    "status": membership.status,
                    "added_at": membership.added_at,
                    "claude_score": membership.claude_score,
                    "claude_reason": membership.claude_reason,
                }
            )

        return LeadDetail(**lead.model_dump(), campaigns=campaigns)

    async def list_for_campaign(self, campaign_id: str) -> list[CampaignLeadView]:
        """
        Every Lead belonging to this campaign, with THIS campaign's
        status/score/reason attached -- the real, persisted join, not the
        raw ephemeral `Campaign.selected_prospects` snapshot.
        """
        memberships = await self.campaign_lead_store.list_for_campaign(campaign_id)
        views = []
        for membership in memberships:
            lead = await self.store.get(membership.lead_id)
            if lead is None:
                continue
            views.append(
                CampaignLeadView(
                    lead_id=lead.lead_id,
                    first_name=lead.first_name,
                    last_name=lead.last_name,
                    email=lead.email,
                    title=lead.title,
                    company=lead.company,
                    lead_status=lead.status,
                    campaign_status=membership.status,
                    claude_score=membership.claude_score,
                    claude_reason=membership.claude_reason,
                    added_at=membership.added_at,
                )
            )
        return sorted(views, key=lambda v: (v.claude_score is None, -(v.claude_score or 0)))
