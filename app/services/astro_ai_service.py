"""
AstroAiService -- the general-purpose Claude assistant inside Astronomic
Hub. Phase 1 (general chat, no Hub-data access) is unchanged in shape;
Phase 2 (this revision) adds read-only CRM tool-use -- see
app/services/astro_crm_tools.py for the three allowlisted tools and the
exact CRM query engine they reuse.

When no `crm_tools` is configured (the `crm_tools=None` default), this
class behaves EXACTLY as Phase 1 did: no `tools` are sent to Claude, and
SYSTEM_PROMPT (unchanged text, still says Astro has no CRM access) is used
verbatim -- this is honest, not a fallback fiction: without a CRM service
wired in, Astro genuinely has no tools to call. Production wiring
(app/main.py) always provides one; only tests that don't care about CRM
behavior omit it.

Stateless: the caller (the API route) passes the full message history on
every call; nothing here persists a conversation, including the
intermediate tool_use/tool_result exchange -- only the final assistant
text reply is ever returned to the frontend and resent as history on the
next turn. A follow-up like "how many of those are in Austin" therefore
works only because Claude re-reasons from the prior turns' plain-text
content (ideally restated criteria in its own prior answer), never because
this service remembers a previous filter -- see the SYSTEM_PROMPT wording
below, which nudges Claude to restate criteria for exactly this reason.
This mirrors Astro Search's own established precedent (app/api/astro.py)
rather than inventing a new pattern.

Cost controls (MAX_MESSAGE_LENGTH/MAX_MESSAGES/MAX_OUTPUT_TOKENS/
REQUEST_TIMEOUT_SECONDS/MAX_TOOL_ITERATIONS below) are deliberately plain
module constants, not environment-configurable settings -- unlike the
MODEL (see app/config.py's astro_chat_model), these are code-level safety
valves, not per-environment deployment config.
"""

import json

from loguru import logger

from app.claude.client import ClaudeClient
from app.config import settings
from app.models.astro_ai import AstroChatMessage, AstroChatRole
from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS, AstroCrmTools

SYSTEM_PROMPT = """You are Astro AI, the AI assistant inside Astronomic Hub. You help the Astronomic team with research, analysis, writing, operations, prospecting, events, campaigns, investors, founders, and other business tasks.

When Hub data or tools are available to you, use them when relevant. Right now, in this phase, you do NOT have access to Astronomic's CRM contacts, campaigns, connected mailboxes, activity log, or any other internal Hub data -- you are operating in general-knowledge mode only.

If asked something that would require Astronomic-specific data you don't actually have access to (for example: "how many investors are in our CRM", "what campaigns are running", "what do we know about person X", "which contacts came from ITF submissions"), say clearly and honestly that you don't have access to that data yet, rather than guessing, estimating, or inventing an answer. Never claim to have looked something up in Astronomic's systems, and never present a number or fact as if it came from Astronomic's data, unless that data was actually provided to you through an approved Hub tool or context in this conversation.

For everything else -- general knowledge questions, research, writing, analysis, drafting emails or messages, brainstorming, and similar business tasks -- answer normally and helpfully, as a capable general-purpose assistant."""

CRM_SYSTEM_PROMPT_TEMPLATE = """You are Astro AI, the AI assistant inside Astronomic Hub. You help the Astronomic team with research, analysis, writing, operations, prospecting, events, campaigns, investors, founders, and other business tasks.

You have READ-ONLY access to Astronomic's CRM contacts through three tools: count_crm_contacts, search_crm_contacts, and get_crm_contact. Use one of these tools whenever a question requires real CRM data (a count, a search, or a specific person's record) -- never guess, estimate, or invent a CRM fact from memory or general knowledge, even if it sounds plausible. When you restate a result, restate the actual filter criteria in plain language (e.g. "142 contacts matching the angel-investor criteria") so a likely follow-up question such as "how many of those are in Austin" can be understood correctly from this conversation's history alone.

Only use the CRM fields, operators, and options listed below -- never invent a field, a custom field, or an option that isn't listed here, and never fall back to matching on a contact's freeform title/job-title text as a substitute for a real classification field. This is the complete list of fields you can filter or search on right now:

{fields_description}

Handle these cases honestly and distinctly, never blurring one into another:
- Zero matching records: say plainly that nothing matched.
- A field/value you're unsure is valid: check it against the list above rather than guessing; if a tool reports it's invalid, tell the user the lookup couldn't use that criterion rather than silently dropping it or making up a number.
- Ambiguous person lookups: if get_crm_contact reports more than one possible match, tell the user multiple contacts matched and ask for (or offer) more identifying detail -- never arbitrarily pick one.
- A failed tool/database call: say the lookup didn't work and offer to try again, never fabricate a result to fill the gap.

You do NOT have access to Astronomic's campaigns, connected mailboxes, activity log, or Apollo, and none of your CRM tools can create, modify, or delete anything -- there is no write, send, or campaign-building capability available to you at all. If asked to do something outside read-only CRM lookups (running a campaign, sending email, searching Apollo, editing a contact, etc.), say clearly and honestly that you can't do that, rather than pretending to.

For everything else -- general knowledge questions, research, writing, analysis, drafting emails or messages, brainstorming, and similar business tasks -- answer normally and helpfully, as a capable general-purpose assistant."""

MAX_MESSAGE_LENGTH = 4_000
MAX_MESSAGES = 20
MAX_OUTPUT_TOKENS = 1_024
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TOOL_ITERATIONS = 4

NO_TOOL_RESULT_FALLBACK = "I wasn't able to finish looking that up -- could you rephrase or narrow your question?"


class AstroAiValidationError(ValueError):
    """A request-shape problem this service rejects before ever calling
    Claude -- never a cost, always a clean 400."""


class AstroAiService:
    def __init__(self, claude_client: ClaudeClient, crm_tools: AstroCrmTools | None = None):
        self.claude_client = claude_client
        self.crm_tools = crm_tools

    async def chat(self, messages: list[AstroChatMessage]) -> AstroChatMessage:
        self._validate(messages)

        payload = [{"role": m.role.value, "content": m.content} for m in messages]
        system_prompt = await self._build_system_prompt()
        tools = CRM_TOOL_DEFINITIONS if self.crm_tools else None

        for _ in range(MAX_TOOL_ITERATIONS):
            text, usage, stop_reason, content = await self.claude_client.generate_chat_reply(
                system_prompt=system_prompt,
                messages=payload,
                max_tokens=MAX_OUTPUT_TOKENS,
                timeout=REQUEST_TIMEOUT_SECONDS,
                tools=tools,
            )
            # Token counts and stop_reason only -- never message content or
            # tool arguments/results, per this phase's explicit
            # logging/privacy requirement.
            logger.info(
                f"Astro AI chat reply generated (input_tokens={usage.get('input_tokens')}, "
                f"output_tokens={usage.get('output_tokens')}, stop_reason={stop_reason})."
            )

            if stop_reason != "tool_use":
                return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=text)

            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
            if not tool_use_blocks:
                return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=text or NO_TOOL_RESULT_FALLBACK)

            payload.append({"role": "assistant", "content": content})
            tool_result_blocks = []
            for block in tool_use_blocks:
                result = await self.crm_tools.dispatch(block.get("name", ""), block.get("input", {}))
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": json.dumps(result),
                    }
                )
            payload.append({"role": "user", "content": tool_result_blocks})

        return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=NO_TOOL_RESULT_FALLBACK)

    async def _build_system_prompt(self) -> str:
        if not self.crm_tools:
            return SYSTEM_PROMPT
        fields_description = await self.crm_tools.describe_available_fields()
        return CRM_SYSTEM_PROMPT_TEMPLATE.format(fields_description=fields_description)

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
