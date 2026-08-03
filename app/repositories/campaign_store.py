"""
Storage abstraction for Campaign objects.

CampaignService depends ONLY on CampaignStore -- never on a concrete
backend, and never reaches into dict/SQL/etc. directly. Swapping in
Postgres, Redis, or SQLite later means writing one new class in this file
(or a new module implementing the same interface) and changing the single
place CampaignService is constructed; no business logic changes.

Every method is async, even on MemoryCampaignStore where nothing actually
awaits -- so a real I/O-bound backend is a drop-in replacement with
identical call sites.
"""

from abc import ABC, abstractmethod

from app.models.campaign import Campaign


class CampaignNotFoundError(Exception):
    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        super().__init__(f"Campaign not found: {campaign_id}")


class CampaignStore(ABC):
    @abstractmethod
    async def create(self, campaign: Campaign) -> None:
        """Persist a newly-created campaign. Raises if campaign_id already exists."""

    @abstractmethod
    async def get(self, campaign_id: str) -> Campaign | None:
        """Returns the campaign, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, campaign: Campaign) -> None:
        """Persist mutations to an existing campaign."""

    @abstractmethod
    async def list(self) -> list[Campaign]:
        """All stored campaigns -- for a future history view."""


class MemoryCampaignStore(CampaignStore):
    """
    Dictionary-backed, keyed by campaign_id. Not persistent across process
    restarts or redeploys -- deliberately the simplest possible
    implementation of the interface, intended for local development until
    a real backend is added.
    """

    def __init__(self):
        self._campaigns: dict[str, Campaign] = {}

    async def create(self, campaign: Campaign) -> None:
        if campaign.campaign_id in self._campaigns:
            raise ValueError(f"Campaign already exists: {campaign.campaign_id}")
        self._campaigns[campaign.campaign_id] = campaign

    async def get(self, campaign_id: str) -> Campaign | None:
        return self._campaigns.get(campaign_id)

    async def save(self, campaign: Campaign) -> None:
        if campaign.campaign_id not in self._campaigns:
            raise CampaignNotFoundError(campaign.campaign_id)
        self._campaigns[campaign.campaign_id] = campaign

    async def list(self) -> list[Campaign]:
        return list(self._campaigns.values())
