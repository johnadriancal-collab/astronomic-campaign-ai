"""
Storage abstraction for MailCampaign -- small, low-volume records, stored
whole via the same JSON-blob convention as CrmContactList (see
app/repositories/crm_contact_list_store.py). No uniqueness constraint on
`name` -- matching that same precedent, nothing in Phase 1 scope calls for
unique campaign names.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailCampaign


class MailCampaignNotFoundError(Exception):
    def __init__(self, mail_campaign_id: str):
        self.mail_campaign_id = mail_campaign_id
        super().__init__(f"MailCampaign not found: {mail_campaign_id}")


class MailCampaignStore(ABC):
    @abstractmethod
    async def create(self, campaign: MailCampaign) -> None:
        """Persist a newly-created campaign. Raises ValueError if mail_campaign_id
        already exists."""

    @abstractmethod
    async def get(self, mail_campaign_id: str) -> MailCampaign | None:
        """Returns the campaign, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, campaign: MailCampaign) -> None:
        """Persist mutations to an existing campaign."""

    @abstractmethod
    async def list(self) -> list[MailCampaign]:
        """Every stored campaign, oldest first."""


class MemoryMailCampaignStore(MailCampaignStore):
    """Dict-backed, keyed by mail_campaign_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._campaigns: dict[str, MailCampaign] = {}

    async def create(self, campaign: MailCampaign) -> None:
        if campaign.mail_campaign_id in self._campaigns:
            raise ValueError(f"MailCampaign already exists: {campaign.mail_campaign_id}")
        self._campaigns[campaign.mail_campaign_id] = campaign

    async def get(self, mail_campaign_id: str) -> MailCampaign | None:
        return self._campaigns.get(mail_campaign_id)

    async def save(self, campaign: MailCampaign) -> None:
        if campaign.mail_campaign_id not in self._campaigns:
            raise MailCampaignNotFoundError(campaign.mail_campaign_id)
        self._campaigns[campaign.mail_campaign_id] = campaign

    async def list(self) -> list[MailCampaign]:
        return list(self._campaigns.values())
