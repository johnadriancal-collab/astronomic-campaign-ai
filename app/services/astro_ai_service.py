"""
AstroAiService -- Phase 1 foundation for Astro AI, the general-purpose
Claude assistant inside Astronomic Hub. This phase deliberately has NO
access to CRM/campaign/mailbox/activity data of any kind; see this
module's SYSTEM_PROMPT for the exact, backend-owned instruction that keeps
Astro from claiming otherwise. Phase 2 (not built here) would add narrowly
scoped, read-only Hub tools Claude can call -- see the architecture
recommendation in this phase's final report, not in code.

Stateless: the caller (the API route) passes the full message history on
every call; nothing here persists a conversation. This mirrors Astro
Search's own established precedent (app/api/astro.py) rather than
inventing a new pattern.

Cost controls (MAX_MESSAGE_LENGTH/MAX_MESSAGES/MAX_OUTPUT_TOKENS/
REQUEST_TIMEOUT_SECONDS below) are deliberately plain module constants,
not environment-configurable settings -- unlike the MODEL (see
app/config.py's astro_chat_model), these are code-level safety valves, not
per-environment deployment config.
"""

from loguru import logger

from app.claude.client import ClaudeClient
from app.config import settings
from app.models.astro_ai import AstroChatMessage, AstroChatRole

SYSTEM_PROMPT = """You are Astro AI, the AI assistant inside Astronomic Hub. You help the Astronomic team with research, analysis, writing, operations, prospecting, events, campaigns, investors, founders, and other business tasks.

When Hub data or tools are available to you, use them when relevant. Right now, in this phase, you do NOT have access to Astronomic's CRM contacts, campaigns, connected mailboxes, activity log, or any other internal Hub data -- you are operating in general-knowledge mode only.

If asked something that would require Astronomic-specific data you don't actually have access to (for example: "how many investors are in our CRM", "what campaigns are running", "what do we know about person X", "which contacts came from ITF submissions"), say clearly and honestly that you don't have access to that data yet, rather than guessing, estimating, or inventing an answer. Never claim to have looked something up in Astronomic's systems, and never present a number or fact as if it came from Astronomic's data, unless that data was actually provided to you through an approved Hub tool or context in this conversation.

For everything else -- general knowledge questions, research, writing, analysis, drafting emails or messages, brainstorming, and similar business tasks -- answer normally and helpfully, as a capable general-purpose assistant."""

MAX_MESSAGE_LENGTH = 4_000
MAX_MESSAGES = 20
MAX_OUTPUT_TOKENS = 1_024
REQUEST_TIMEOUT_SECONDS = 30.0


class AstroAiValidationError(ValueError):
    """A request-shape problem this service rejects before ever calling
    Claude -- never a cost, always a clean 400."""


class AstroAiService:
    def __init__(self, claude_client: ClaudeClient):
        self.claude_client = claude_client

    async def chat(self, messages: list[AstroChatMessage]) -> AstroChatMessage:
        self._validate(messages)

        payload = [{"role": m.role.value, "content": m.content} for m in messages]
        text, usage = await self.claude_client.generate_chat_reply(
            system_prompt=SYSTEM_PROMPT,
            messages=payload,
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        # Token counts only -- never message content, per this phase's
        # explicit logging/privacy requirement.
        logger.info(
            f"Astro AI chat reply generated (input_tokens={usage.get('input_tokens')}, "
            f"output_tokens={usage.get('output_tokens')})."
        )
        return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=text)

    def _validate(self, messages: list[AstroChatMessage]) -> None:
        if not messages:
            raise AstroAiValidationError("At least one message is required.")
        if len(messages) > MAX_MESSAGES:
            raise AstroAiValidationError(
                f"This conversation has gotten too long (max {MAX_MESSAGES} messages) -- please start a new one."
            )
        for message in messages:
            if len(message.content) > MAX_MESSAGE_LENGTH:
                raise AstroAiValidationError(
                    f"That message is too long (max {MAX_MESSAGE_LENGTH} characters)."
                )
        if messages[-1].role != AstroChatRole.USER:
            raise AstroAiValidationError("The most recent message must be from the user.")


def build_default_claude_client() -> ClaudeClient:
    """The one place Astro AI's Claude model choice is wired in -- see
    app/config.py's astro_chat_model docstring for why this is a separate
    setting from Campaign Builder's claude_model."""
    return ClaudeClient(model=settings.astro_chat_model)
