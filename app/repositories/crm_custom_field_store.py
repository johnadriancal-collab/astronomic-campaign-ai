"""
Storage abstraction for CrmCustomFieldDefinition -- small, low-volume
records (a handful to a few dozen, expected), but stored with the same
JSON-blob convention as everything else here for consistency and because
`options`/future definition fields can then grow with no migration.
"""

from abc import ABC, abstractmethod

from app.models.crm import CrmCustomFieldDefinition


class CrmCustomFieldNotFoundError(Exception):
    def __init__(self, crm_custom_field_id: str):
        self.crm_custom_field_id = crm_custom_field_id
        super().__init__(f"CrmCustomFieldDefinition not found: {crm_custom_field_id}")


class CrmCustomFieldStore(ABC):
    @abstractmethod
    async def create(self, definition: CrmCustomFieldDefinition) -> None:
        """Persist a new definition. Raises ValueError if field_key already exists."""

    @abstractmethod
    async def get(self, crm_custom_field_id: str) -> CrmCustomFieldDefinition | None:
        """Returns the definition, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_field_key(self, field_key: str) -> CrmCustomFieldDefinition | None:
        """Lookup by the key used in CrmContact.custom_fields -- the uniqueness constraint."""

    @abstractmethod
    async def save(self, definition: CrmCustomFieldDefinition) -> None:
        """Persist mutations (edits, active/inactive toggles) to an existing definition."""

    @abstractmethod
    async def list(self) -> list[CrmCustomFieldDefinition]:
        """Every definition, active and inactive -- filtering to active-only happens in crm_service.py."""


class MemoryCrmCustomFieldStore(CrmCustomFieldStore):
    """Dict-backed, keyed by crm_custom_field_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._definitions: dict[str, CrmCustomFieldDefinition] = {}

    async def create(self, definition: CrmCustomFieldDefinition) -> None:
        if definition.crm_custom_field_id in self._definitions:
            raise ValueError(f"CrmCustomFieldDefinition already exists: {definition.crm_custom_field_id}")
        if await self.get_by_field_key(definition.field_key) is not None:
            raise ValueError(f"field_key already exists: {definition.field_key}")
        self._definitions[definition.crm_custom_field_id] = definition

    async def get(self, crm_custom_field_id: str) -> CrmCustomFieldDefinition | None:
        return self._definitions.get(crm_custom_field_id)

    async def get_by_field_key(self, field_key: str) -> CrmCustomFieldDefinition | None:
        for definition in self._definitions.values():
            if definition.field_key == field_key:
                return definition
        return None

    async def save(self, definition: CrmCustomFieldDefinition) -> None:
        if definition.crm_custom_field_id not in self._definitions:
            raise CrmCustomFieldNotFoundError(definition.crm_custom_field_id)
        self._definitions[definition.crm_custom_field_id] = definition

    async def list(self) -> list[CrmCustomFieldDefinition]:
        return list(self._definitions.values())
