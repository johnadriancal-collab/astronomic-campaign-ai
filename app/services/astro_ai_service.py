"""
AstroAiService -- the general-purpose Claude assistant inside Astronomic
Hub. Phase 1 (general chat, no Hub-data access) is unchanged in shape;
Phase 2 added read-only CRM tool-use; Phase 3 (this revision) extends the
SAME tool-use loop to three more read-only domains -- CRM Lists, Campaign
Manager, connected mailboxes, and the Activity Log -- via AstroHubTools
(app/services/astro_hub_tools.py), which composes each domain's own tool
file (astro_crm_tools.py, astro_campaign_tools.py, astro_mailbox_tools.py,
astro_activity_tools.py) rather than merging them into one generic
"query the Hub" abstraction. Nothing about the loop mechanics below
changed for Phase 3 -- only the tool list and the system prompt's domain
guidance grew.

When no `hub_tools` is configured (the `hub_tools=None` default), this
class behaves EXACTLY as Phase 1 did: no `tools` are sent to Claude, and
SYSTEM_PROMPT (unchanged text, still says Astro has no Hub-data access) is
used verbatim -- this is honest, not a fallback fiction: without any Hub
tools wired in, Astro genuinely has none to call. Production wiring
(app/main.py) always provides a fully-populated AstroHubTools; only tests
that don't care about Hub-tool behavior omit it.

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
from datetime import datetime

from loguru import logger

from app.claude.client import ClaudeClient
from app.config import settings
from app.models.astro_ai import AstroChatAttachment, AstroChatMessage, AstroChatRole
from app.services.astro_activity_tools import BUSINESS_TIMEZONE
from app.services.astro_hub_tools import AstroHubTools

SYSTEM_PROMPT = """You are Astro AI, the AI assistant inside Astronomic Hub. You help the Astronomic team with research, analysis, writing, operations, prospecting, events, campaigns, investors, founders, and other business tasks.

When Hub data or tools are available to you, use them when relevant. Right now, in this phase, you do NOT have access to Astronomic's CRM contacts, campaigns, connected mailboxes, activity log, or any other internal Hub data -- you are operating in general-knowledge mode only.

If asked something that would require Astronomic-specific data you don't actually have access to (for example: "how many investors are in our CRM", "what campaigns are running", "what do we know about person X", "which contacts came from ITF submissions"), say clearly and honestly that you don't have access to that data yet, rather than guessing, estimating, or inventing an answer. Never claim to have looked something up in Astronomic's systems, and never present a number or fact as if it came from Astronomic's data, unless that data was actually provided to you through an approved Hub tool or context in this conversation.

For everything else -- general knowledge questions, research, writing, analysis, drafting emails or messages, brainstorming, and similar business tasks -- answer normally and helpfully, as a capable general-purpose assistant."""

HUB_SYSTEM_PROMPT_TEMPLATE = """You are Astro AI, the AI assistant inside Astronomic Hub. You help the Astronomic team with research, analysis, writing, operations, prospecting, events, campaigns, investors, founders, and other business tasks.

You have READ-ONLY access to live Hub data through these tool groups -- use one of them whenever a question requires real Hub data; never guess, estimate, or invent a fact from memory or general knowledge, even if it sounds plausible:
- CRM contacts: count_crm_contacts, search_crm_contacts, get_crm_contact, export_crm_contacts (downloads the COMPLETE matching set as a CSV, never limited to the 20-contact search preview).
- CRM Lists: list_crm_lists, get_crm_list, get_crm_list_members, count_crm_list_members -- the last two accept the SAME CRM filter conditions as the contact tools, so a question combining a list with a CRM criterion (e.g. "angel investors in the Hotshot list") resolves in ONE call, not two.
- Campaign Manager: list_campaigns, get_campaign, count_campaigns. Apollo campaigns and Astronomic Mail campaigns are two separate, different systems with different data -- never present them as identical. Astronomic Mail cannot send email yet: it has no real send/open/click statistics, only a THEORETICAL planned-audience estimate (contacts_eligible/theoretical_total_sends) that you must always label as theoretical, never as actual sends. Apollo campaigns DO have real send/open/click/reply/bounce statistics, but ONLY for a campaign whose sequence has been manually synced -- get_campaign tells you whether/when that happened (synced=false or a null sequence_stats means "no data available," never "zero activity"). Apollo campaigns have no CRM-list relationship (only Astronomic Mail does, via source_list_id), and NO campaign has any mailbox relationship at all in this Hub today -- if asked which campaign uses a given inbox, say plainly that this relationship isn't stored, never infer one.
- Connected mailboxes: list_connected_mailboxes, get_mailbox. There is no sending, deliverability, "emails sent today," or queue data for mailboxes at all -- never ask for or report any of that.
- Activity Log: search_activity -- a record of meaningful CRM/list/import/campaign/mail actions (never ordinary reads/views). Every event's actor (who did it) is always unavailable today -- never state or guess who performed an action, only what happened and when. Whenever a question involves ANY date or time period -- explicit ("August 20", "between August 20 and 25") or relative ("today", "yesterday", "this week", "last week") -- you MUST translate it into date_from/date_to and pass them to search_activity; never call it with no date filter and then manually reason over the raw results yourself, since that only returns the 20 most-recent events overall and can silently miss earlier activity from the very period you were asked about. The current date/time is {current_datetime} (America/Chicago, Central Time) -- use this as your anchor for any relative date, and state the date range you used (in Central Time) in your answer so the user can adjust for their own timezone.

When you restate a result, restate the actual filter/criteria in plain language (e.g. "142 contacts matching the angel-investor criteria") so a likely follow-up question such as "how many of those are in Austin" can be understood correctly from this conversation's history alone.

When the user asks to export, download, or get a CSV of a set of contacts (e.g. "export them," "download this list," "give me a CSV of those 287"), call export_crm_contacts using the SAME filter criteria most recently established in this conversation -- never re-derive the population from a short preview list you already showed (search results are capped at 20; export_crm_contacts always re-queries the complete matching set on its own, so a filter that matched 287 contacts exports all 287). Only ask the user to clarify first if which prior result they mean is genuinely ambiguous. After a successful export, just confirm what was exported in plain language (e.g. "Exported 287 contacts.") -- never invent, describe, or repeat a download link or URL yourself; the file's download link is attached and rendered automatically from the tool's result, not from your words. If export_crm_contacts returns a "too_large" result, tell the user the segment is too big to export and ask them to narrow their criteria; if it returns "no_matches," say plainly that nothing matched.

Only use the CRM fields, operators, and options listed below -- never invent a field, a custom field, or an option that isn't listed here, and never fall back to matching on a contact's freeform title/job-title text as a substitute for a real classification field. This is the complete list of CRM fields you can filter or search contacts and list members on right now:

{fields_description}

Handle these cases honestly and distinctly, never blurring one into another:
- Zero matching records: say plainly that nothing matched.
- A field/value/name you're unsure is valid: check it against the information available rather than guessing; if a tool reports it's invalid, tell the user the lookup couldn't use that criterion rather than silently dropping it or making up an answer.
- Ambiguous lookups (a list, campaign, mailbox, or person name that matches more than one real record): a tool will tell you explicitly ("ambiguous") -- always tell the user multiple records matched and ask for more identifying detail, never arbitrarily pick one.
- A failed tool/database call: say the lookup didn't work and offer to try again, never fabricate a result to fill the gap.
- A relationship or statistic that genuinely doesn't exist in the Hub's data model (e.g. which campaign uses a given mailbox, or real send stats for an unsynced or Astronomic Mail campaign): say plainly that this information isn't available, never infer or estimate it.

None of your tools can create, modify, launch, pause, archive, or delete anything, send or schedule email, connect or disconnect a mailbox, or search/build in Apollo -- there is no write, send, or campaign-building capability available to you at all. If asked to do something outside these read-only lookups, say clearly and honestly that you can't do that, rather than pretending to.

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
    def __init__(self, claude_client: ClaudeClient, hub_tools: AstroHubTools | None = None):
        self.claude_client = claude_client
        self.hub_tools = hub_tools

    async def chat(self, messages: list[AstroChatMessage]) -> AstroChatMessage:
        self._validate(messages)

        payload = [{"role": m.role.value, "content": m.content} for m in messages]
        system_prompt = await self._build_system_prompt()
        tools = self.hub_tools.tool_definitions if self.hub_tools else None
        # Populated from a successful export_crm_contacts tool result, never
        # from Claude's own text -- see the module docstring on
        # AstroChatAttachment. Only the LAST successful export in this turn
        # is kept if more than one somehow happens.
        pending_attachment: AstroChatAttachment | None = None

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
                return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=text, attachment=pending_attachment)

            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
            if not tool_use_blocks:
                return AstroChatMessage(
                    role=AstroChatRole.ASSISTANT, content=text or NO_TOOL_RESULT_FALLBACK, attachment=pending_attachment
                )

            payload.append({"role": "assistant", "content": content})
            tool_result_blocks = []
            for block in tool_use_blocks:
                name = block.get("name", "")
                result = await self.hub_tools.dispatch(name, block.get("input", {}))
                if name == "export_crm_contacts" and result.get("status") == "ready":
                    # The download URL is built HERE, deterministically, from
                    # the tool's own export_id -- Claude never sees or
                    # constructs this URL.
                    pending_attachment = AstroChatAttachment(
                        filename=result["filename"],
                        url=f"/astro-ai/exports/{result['export_id']}",
                        contact_count=result["contact_count"],
                    )
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": json.dumps(result),
                    }
                )
            payload.append({"role": "user", "content": tool_result_blocks})

        return AstroChatMessage(role=AstroChatRole.ASSISTANT, content=NO_TOOL_RESULT_FALLBACK, attachment=pending_attachment)

    async def _build_system_prompt(self) -> str:
        if not self.hub_tools:
            return SYSTEM_PROMPT
        fields_description = await self.hub_tools.describe_available_fields()
        # Computed fresh on every call (never cached) so "today"/"this
        # week" are always anchored to the actual current moment -- see
        # astro_activity_tools.py's module docstring for why
        # America/Chicago (BUSINESS_TIMEZONE) and not UTC or a caller's
        # local time.
        current_datetime = datetime.now(BUSINESS_TIMEZONE).strftime("%A, %B %d, %Y, %I:%M %p")
        return HUB_SYSTEM_PROMPT_TEMPLATE.format(
            fields_description=fields_description, current_datetime=current_datetime
        )

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
