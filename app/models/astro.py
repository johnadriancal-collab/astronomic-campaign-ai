"""
Astro Core Phase 1 request/response models -- see app/services/astro_parser.py
for the deterministic parser and app/api/astro.py for the route. Read-only,
two intents only (search_contacts/count_contacts), no Claude/Anthropic
involvement anywhere in this feature.
"""

from typing import Literal

from pydantic import BaseModel

from app.models.crm import CrmContact, FilterQuery


class AstroCommandRequest(BaseModel):
    text: str


class AstroCommandResponse(BaseModel):
    intent: Literal["search_contacts", "count_contacts", "unresolved"]
    understood_as: str

    # Populated only when intent is search_contacts/count_contacts.
    query: FilterQuery | None = None
    total: int | None = None
    contacts: list[CrmContact] | None = None  # search_contacts only -- omitted for count_contacts

    # Populated only when intent is "unresolved".
    understood: dict[str, str] | None = None
    unresolved_phrase: str | None = None
    message: str | None = None
