"""
Astro Core request/response models -- see app/services/astro_parser.py for
the deterministic parser and app/api/astro.py for the route. Read-only,
two intents only (search_contacts/count_contacts), no Claude/Anthropic
involvement anywhere in this feature.

Phase 1.1 (2026-08-13) adds conversational refinement, entirely via the
optional `context` field below -- the frontend is the ONLY place
conversation state lives (no backend session store of any kind). Omitting
`context` reproduces Phase 1's exact standalone behavior; see
app/api/astro.py's dispatch logic for why.
"""

from typing import Literal

from pydantic import BaseModel

from app.models.crm import CrmContact, FilterQuery


class AstroCommandContext(BaseModel):
    """The entirety of Astro's "conversation state" -- just the last resolved
    FilterQuery + intent, held and resent by the frontend. No session id, no
    backend persistence: see app/services/astro_parser.py's Phase 1.1 module
    docstring for why this is deliberately the smallest reliable design."""

    query: FilterQuery | None = None
    intent: Literal["search_contacts", "count_contacts"] | None = None


class AstroCommandRequest(BaseModel):
    text: str
    context: AstroCommandContext | None = None


class AstroCommandResponse(BaseModel):
    intent: Literal["search_contacts", "count_contacts", "unresolved"]
    understood_as: str

    # Populated only when intent is search_contacts/count_contacts.
    query: FilterQuery | None = None
    total: int | None = None
    contacts: list[CrmContact] | None = None  # search_contacts only -- omitted for count_contacts

    # Populated ONLY for a resolved Phase 1.1 refinement turn (context was
    # supplied and used) -- never set for a standalone Phase 1 command.
    operation: Literal["add", "replace", "remove", "reset", "change_intent"] | None = None
    changed_field: str | None = None

    # `message` serves two purposes depending on `intent`: for a resolved
    # refinement, it's the deterministic explanation of what happened (e.g.
    # "Added a $100k+ check-size filter. 4 contacts match."); for intent ==
    # "unresolved", it's the Phase 1 clarification message. Never both at once.
    message: str | None = None

    # Populated only when intent is "unresolved".
    understood: dict[str, str] | None = None
    unresolved_phrase: str | None = None
