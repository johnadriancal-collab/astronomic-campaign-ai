"""
Storage abstraction for the Campaign<->Lead membership join.

Unlike Campaign.create()/Lead.create() (where a duplicate id is a real
bug), CampaignLead membership is expected to be re-added harmlessly during
a rebuild -- so `add()` is idempotent (a no-op on an existing pair) rather
than raising, keyed on the composite (campaign_id, lead_id).
"""

from abc import ABC, abstractmethod

from app.models.lead import CampaignLead


class CampaignLeadStore(ABC):
    @abstractmethod
    async def add(self, campaign_lead: CampaignLead) -> None:
        """Idempotent: a no-op if this (campaign_id, lead_id) pair already exists."""

    @abstractmethod
    async def list_for_campaign(self, campaign_id: str) -> list[CampaignLead]:
        """Every lead belonging to this campaign."""

    @abstractmethod
    async def list_for_lead(self, lead_id: str) -> list[CampaignLead]:
        """Every campaign this lead belongs to."""


class MemoryCampaignLeadStore(CampaignLeadStore):
    """Dict-backed, keyed by (campaign_id, lead_id) -- not persistent, for tests/local dev."""

    def __init__(self):
        self._memberships: dict[tuple[str, str], CampaignLead] = {}

    async def add(self, campaign_lead: CampaignLead) -> None:
        key = (campaign_lead.campaign_id, campaign_lead.lead_id)
        if key in self._memberships:
            return
        self._memberships[key] = campaign_lead

    async def list_for_campaign(self, campaign_id: str) -> list[CampaignLead]:
        return [cl for cl in self._memberships.values() if cl.campaign_id == campaign_id]

    async def list_for_lead(self, lead_id: str) -> list[CampaignLead]:
        return [cl for cl in self._memberships.values() if cl.lead_id == lead_id]
