"""
Storage abstraction for CrmContact. Evolving-shape aggregate (same
JSON-blob convention as CampaignStore) plus three confident-tier dedup
lookups and one non-confident fallback lookup -- the exact hierarchy from
crm_service.py's merge/dedup logic: email -> apollo_contact_id ->
linkedin_url (all exact, normalized, safe to auto-treat as "existing") ->
name+company (exact, normalized, NEVER auto-treated as existing -- always
surfaced as a possible duplicate for a human decision).

NOTE: `list()` is deliberately declared LAST in both classes below (same
convention CampaignStore/LeadStore already use) -- a method named `list`
shadows the builtin `list` name within the REST of that class body once
defined, breaking any `-> list[...]` annotation on a method declared after
it. Declaring it last avoids this entirely.
"""

from abc import ABC, abstractmethod

from app.models.crm import CrmContact


class CrmContactNotFoundError(Exception):
    def __init__(self, crm_contact_id: str):
        self.crm_contact_id = crm_contact_id
        super().__init__(f"CrmContact not found: {crm_contact_id}")


class CrmContactStore(ABC):
    @abstractmethod
    async def create(self, contact: CrmContact) -> None:
        """Persist a newly-created contact. Raises ValueError if crm_contact_id already exists."""

    @abstractmethod
    async def get(self, crm_contact_id: str) -> CrmContact | None:
        """Returns the contact, or None if it doesn't exist."""

    @abstractmethod
    async def save(self, contact: CrmContact) -> None:
        """Persist mutations to an existing contact."""

    @abstractmethod
    async def get_by_email(self, normalized_email: str) -> CrmContact | None:
        """Confident-tier dedup lookup. Caller passes an already-normalized value."""

    @abstractmethod
    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> CrmContact | None:
        """Confident-tier dedup lookup."""

    @abstractmethod
    async def get_by_linkedin_url(self, normalized_linkedin_url: str) -> CrmContact | None:
        """Confident-tier dedup lookup. Caller passes an already-normalized value."""

    @abstractmethod
    async def find_by_name_and_company(self, normalized_name_company: str) -> list[CrmContact]:
        """
        Fallback-tier lookup -- may return more than one match (unlike the
        three lookups above, which are each backed by a unique constraint).
        Every result is a POSSIBLE duplicate only, never auto-merged.
        """

    @abstractmethod
    async def list(self) -> list[CrmContact]:
        """Every stored contact -- filtering/search happens in crm_service.py, same
        convention as LeadStore (no search method on the store itself)."""


class MemoryCrmContactStore(CrmContactStore):
    """Dict-backed, keyed by crm_contact_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._contacts: dict[str, CrmContact] = {}

    async def create(self, contact: CrmContact) -> None:
        if contact.crm_contact_id in self._contacts:
            raise ValueError(f"CrmContact already exists: {contact.crm_contact_id}")
        self._contacts[contact.crm_contact_id] = contact

    async def get(self, crm_contact_id: str) -> CrmContact | None:
        return self._contacts.get(crm_contact_id)

    async def save(self, contact: CrmContact) -> None:
        if contact.crm_contact_id not in self._contacts:
            raise CrmContactNotFoundError(contact.crm_contact_id)
        self._contacts[contact.crm_contact_id] = contact

    async def get_by_email(self, normalized_email: str) -> CrmContact | None:
        from app.models.crm import normalize_email

        for contact in self._contacts.values():
            if normalize_email(contact.email) == normalized_email:
                return contact
        return None

    async def get_by_apollo_contact_id(self, apollo_contact_id: str) -> CrmContact | None:
        for contact in self._contacts.values():
            if contact.apollo_contact_id == apollo_contact_id:
                return contact
        return None

    async def get_by_linkedin_url(self, normalized_linkedin_url: str) -> CrmContact | None:
        from app.models.crm import normalize_linkedin_url

        for contact in self._contacts.values():
            if normalize_linkedin_url(contact.linkedin_url) == normalized_linkedin_url:
                return contact
        return None

    async def find_by_name_and_company(self, normalized_name_company: str) -> list[CrmContact]:
        from app.models.crm import normalize_name_company

        return [
            contact
            for contact in self._contacts.values()
            if normalize_name_company(contact.first_name, contact.last_name, contact.company)
            == normalized_name_company
        ]

    async def list(self) -> list[CrmContact]:
        return list(self._contacts.values())
