"""
Astro AI chat (Phase 1 -- general assistant foundation, no Hub-data access
yet). Deliberately separate from app/models/astro.py (Astro Search's
deterministic, Claude-free CRM query parser) and app/models/campaign.py's
CampaignPlan (Campaign Builder's structured, single-shot Claude JSON
generation) -- three different "Astro"-branded features, three different
contracts, none of which this phase merges or replaces.

Stateless by design, matching Astro Search's own precedent (see
app/api/astro.py's module docstring: "the frontend holds the entire
conversation state ... and resends it each turn") -- there is no
conversation-persistence model here on purpose. `role` is restricted to
user/assistant ONLY: a request cannot smuggle in a `system` message to
override the backend-owned system prompt (see astro_ai_service.py's
SYSTEM_PROMPT), which is exactly the property that keeps Astro from being
tricked into claiming Hub-data access it doesn't have.
"""

from enum import Enum

from pydantic import BaseModel


class AstroChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class AstroChatMessage(BaseModel):
    role: AstroChatRole
    content: str


class AstroChatRequest(BaseModel):
    messages: list[AstroChatMessage]
