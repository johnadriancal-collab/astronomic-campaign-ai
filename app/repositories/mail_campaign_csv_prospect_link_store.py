"""
Storage abstraction for MailCampaignCsvProspectLink -- the durable
(mail_campaign_id, idempotency_key) -> import_batch_id record behind
MailCampaignCsvProspectService's "one CSV Add Prospects operation, one
CrmImportBatch, ever" guarantee (Stage 4B, 2026-09-03). See that model's
own docstring for the full reasoning.

No separate row id: `PRIMARY KEY (mail_campaign_id, idempotency_key)` IS
the identity, matching the composite key's own already-unique semantics --
there is nothing else this row could ever need to be looked up by.
"""

from abc import ABC, abstractmethod

from app.models.mail import MailCampaignCsvProspectLink


class DuplicateCsvProspectLinkError(Exception):
    """Raised by create() when (mail_campaign_id, idempotency_key) already
    has a link -- the losing side of a genuine concurrent-submission race
    (two overlapping requests for the same logical operation). Callers
    must NEVER treat this as a generic failure: it means a link for this
    exact (campaign, key) pair already exists (or is mid-creation by
    someone else) and should be looked up via get_by_idempotency_key() and
    used instead -- see MailCampaignCsvProspectService."""

    def __init__(self, mail_campaign_id: str, idempotency_key: str):
        self.mail_campaign_id = mail_campaign_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"A CSV prospect link already exists for campaign {mail_campaign_id} with idempotency_key {idempotency_key!r}"
        )


class MailCampaignCsvProspectLinkStore(ABC):
    @abstractmethod
    async def create(self, link: MailCampaignCsvProspectLink) -> None:
        """Persist a newly-created link. Raises DuplicateCsvProspectLinkError
        if (mail_campaign_id, idempotency_key) already has one."""

    @abstractmethod
    async def get_by_idempotency_key(
        self, mail_campaign_id: str, idempotency_key: str
    ) -> MailCampaignCsvProspectLink | None:
        """None if no link has ever been created for this exact
        (campaign, key) pair."""


class MemoryMailCampaignCsvProspectLinkStore(MailCampaignCsvProspectLinkStore):
    """Dict-backed, keyed by (mail_campaign_id, idempotency_key) -- not
    persistent, for tests/local dev."""

    def __init__(self):
        self._links: dict[tuple[str, str], MailCampaignCsvProspectLink] = {}

    async def create(self, link: MailCampaignCsvProspectLink) -> None:
        key = (link.mail_campaign_id, link.idempotency_key)
        if key in self._links:
            raise DuplicateCsvProspectLinkError(link.mail_campaign_id, link.idempotency_key)
        self._links[key] = link

    async def get_by_idempotency_key(
        self, mail_campaign_id: str, idempotency_key: str
    ) -> MailCampaignCsvProspectLink | None:
        return self._links.get((mail_campaign_id, idempotency_key))
