"""
Storage abstraction for LumaQuestionMapping -- the configurable
label -> CRM field mapping layer (never hardcoded in application logic,
see app/services/luma_sync_service.py). A plain DB table, same "config
lives in a table, not code" precedent as CrmCustomFieldDefinition.
"""

from abc import ABC, abstractmethod

from app.models.luma import LumaQuestionMapping


class LumaQuestionMappingStore(ABC):
    @abstractmethod
    async def create(self, mapping: LumaQuestionMapping) -> None: ...

    @abstractmethod
    async def save(self, mapping: LumaQuestionMapping) -> None:
        """Full update of an existing mapping, keyed on luma_question_mapping_id."""

    @abstractmethod
    async def get(self, luma_question_mapping_id: str) -> LumaQuestionMapping | None: ...

    @abstractmethod
    async def list(self, include_inactive: bool = True) -> list[LumaQuestionMapping]: ...


class MemoryLumaQuestionMappingStore(LumaQuestionMappingStore):
    """Dict-backed, keyed by luma_question_mapping_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._mappings: dict[str, LumaQuestionMapping] = {}

    async def create(self, mapping: LumaQuestionMapping) -> None:
        self._mappings[mapping.luma_question_mapping_id] = mapping

    async def save(self, mapping: LumaQuestionMapping) -> None:
        self._mappings[mapping.luma_question_mapping_id] = mapping

    async def get(self, luma_question_mapping_id: str) -> LumaQuestionMapping | None:
        return self._mappings.get(luma_question_mapping_id)

    async def list(self, include_inactive: bool = True) -> list[LumaQuestionMapping]:
        mappings = list(self._mappings.values())
        if include_inactive:
            return mappings
        return [m for m in mappings if m.active]
