"""
Pydantic models for the Lead system.

A Lead is a durable, global person/entity in this application -- it
outlives any single campaign. CampaignLead is the join between a Campaign
and a Lead, since the same real person (the same Apollo contact) can
belong to more than one campaign over time; Lead is never owned by a
single campaign.

Duplicate detection is keyed on `apollo_contact_id` (UNIQUE at the storage
layer) -- NOT on email/name fuzzy-matching, which is a harder, separate
problem this doesn't attempt to solve. See app/services/lead_service.py
for exactly how/when a Lead gets created.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.models.campaign import CampaignStatus


class LeadStatus(str, Enum):
    NEW = "new"  # the only status Leads can have today -- no editing/transitions yet


class CampaignLeadStatus(str, Enum):
    ADDED = "added"  # the only status a campaign membership can have today


class Lead(BaseModel):
    """
    A Lead is global, person-level data -- true regardless of which
    campaign(s) it belongs to. Anything that's only meaningful in the
    context of ONE specific campaign (how well Claude thought this person
    fit THAT campaign) does NOT belong here -- see CampaignLead below.
    Appearing in a second campaign must never overwrite these fields.
    """

    lead_id: str
    workspace_id: str | None = None  # unused today -- for future multi-tenancy, same convention as Campaign

    apollo_contact_id: str  # the de-dup key: one Lead per Apollo contact, ever

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    title: str | None = None
    company: str | None = None
    company_domain: str | None = None

    status: LeadStatus = LeadStatus.NEW
    created_at: datetime
    updated_at: datetime

    # Point-in-time snapshot of the raw Apollo prospect dict at the moment
    # this Lead was FIRST created -- not synced live, and not touched again
    # if the same Apollo contact resurfaces in a later campaign. Apollo
    # fields that can change out from under us are treated as a deliberate,
    # one-time snapshot here, never silently refreshed (see
    # docs/ARCHITECTURE.md).
    apollo_snapshot: dict = Field(default_factory=dict)


class CampaignLead(BaseModel):
    """
    The relationship between one Lead and one Campaign. claude_score/
    claude_reason live HERE, not on Lead -- they describe how well Claude
    thought this person fit THIS campaign's plan, which is inherently
    per-(campaign, lead), not a fact about the person globally. The same
    Lead in two campaigns can (and often will) carry two different scores.
    """

    campaign_id: str
    lead_id: str
    status: CampaignLeadStatus = CampaignLeadStatus.ADDED
    added_at: datetime
    claude_score: float | None = None
    claude_reason: str | None = None


class LeadListItem(Lead):
    """Lead plus a join-derived fact that doesn't belong on Lead itself."""

    campaign_count: int


class LeadCampaignMembership(BaseModel):
    """One row of 'which campaigns does this lead belong to', for the lead detail page."""

    campaign_id: str
    campaign_name: str
    campaign_status: CampaignStatus
    status: CampaignLeadStatus
    added_at: datetime
    claude_score: float | None = None
    claude_reason: str | None = None


class LeadDetail(Lead):
    campaigns: list[LeadCampaignMembership]


class CampaignLeadView(BaseModel):
    """
    One row of 'which leads belong to this campaign', for the campaign
    detail page (GET /campaign/{id}/leads) -- Lead's global fields plus
    this specific campaign's status/score/reason.
    """

    lead_id: str
    first_name: str | None
    last_name: str | None
    email: str | None
    title: str | None
    company: str | None
    lead_status: LeadStatus
    campaign_status: CampaignLeadStatus
    claude_score: float | None
    claude_reason: str | None
    added_at: datetime
