"""
Storage abstraction for Lead objects -- mirrors CampaignStore exactly (see
that file's docstring): LeadService depends only on LeadStore, never on a
concrete backend.
"""

from abc import ABC, abstractmethod

from app.models.lead import Lead


class LeadNotFoundError(Exception):
    def __init__(self, lead_id: str):
        self.lead_id = lead_id
        super().__init__(f"Lead not found: {lead_id}")


class LeadStore(ABC):
    @abstractmethod
    async def create(self, lead: Lead) -> None:
        """Persist a newly-created lead. Raises if lead_id or apollo_contact_id already exists."""

    @abstractmethod
    async def get(self, lead_id: str) -> Lead | None:
        """Returns the lead, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> Lead | None:
        """The duplicate-detection lookup -- one Lead per Apollo contact, ever."""

    @abstractmethod
    async def save(self, lead: Lead) -> None:
        """Persist mutations to an existing lead."""

    @abstractmethod
    async def list(self) -> list[Lead]:
        """All stored leads."""


class MemoryLeadStore(LeadStore):
    """Dictionary-backed, keyed by lead_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._leads: dict[str, Lead] = {}

    async def create(self, lead: Lead) -> None:
        if lead.lead_id in self._leads:
            raise ValueError(f"Lead already exists: {lead.lead_id}")
        if any(existing.apollo_contact_id == lead.apollo_contact_id for existing in self._leads.values()):
            raise ValueError(f"Lead already exists for apollo_contact_id: {lead.apollo_contact_id}")
        self._leads[lead.lead_id] = lead

    async def get(self, lead_id: str) -> Lead | None:
        return self._leads.get(lead_id)

    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> Lead | None:
        for lead in self._leads.values():
            if lead.apollo_contact_id == apollo_contact_id:
                return lead
        return None

    async def save(self, lead: Lead) -> None:
        if lead.lead_id not in self._leads:
            raise LeadNotFoundError(lead.lead_id)
        self._leads[lead.lead_id] = lead

    async def list(self) -> list[Lead]:
        return list(self._leads.values())
