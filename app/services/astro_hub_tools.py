"""
Astro AI Phase 3 -- composes the four per-domain read-only tool surfaces
(CRM/Lists, Mailboxes, Activity Log, Campaign Manager) into the single
`tools` list and single dispatch entry point AstroAiService's tool-use
loop needs, without merging their implementations into one generic
"query the Hub" abstraction. Each domain keeps its own file, its own
allowlisted dispatch, and its own read-only service dependency -- this
class only routes a tool NAME to the one domain object that owns it.

Every sub-tools object is optional so a caller that only cares about one
domain (e.g. a Phase-2-only test) doesn't have to construct all four --
production wiring (app/main.py) always provides all four.

Raises ValueError at construction time if two domains ever declare the
same tool name -- a fast, load-time guard against an accidental name
collision, rather than a silent "whichever domain was registered first
wins."
"""

from app.services.astro_activity_tools import ASTRO_ACTIVITY_TOOL_DEFINITIONS, AstroActivityTools
from app.services.astro_campaign_tools import ASTRO_CAMPAIGN_TOOL_DEFINITIONS, AstroCampaignTools
from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS, AstroCrmTools
from app.services.astro_mailbox_tools import ASTRO_MAILBOX_TOOL_DEFINITIONS, AstroMailboxTools


class AstroHubTools:
    def __init__(
        self,
        crm_tools: AstroCrmTools | None = None,
        mailbox_tools: AstroMailboxTools | None = None,
        activity_tools: AstroActivityTools | None = None,
        campaign_tools: AstroCampaignTools | None = None,
    ):
        self.crm_tools = crm_tools
        self.mailbox_tools = mailbox_tools
        self.activity_tools = activity_tools
        self.campaign_tools = campaign_tools

        domains_and_definitions = []
        if crm_tools is not None:
            domains_and_definitions.append((crm_tools, CRM_TOOL_DEFINITIONS))
        if mailbox_tools is not None:
            domains_and_definitions.append((mailbox_tools, ASTRO_MAILBOX_TOOL_DEFINITIONS))
        if activity_tools is not None:
            domains_and_definitions.append((activity_tools, ASTRO_ACTIVITY_TOOL_DEFINITIONS))
        if campaign_tools is not None:
            domains_and_definitions.append((campaign_tools, ASTRO_CAMPAIGN_TOOL_DEFINITIONS))

        self._tool_definitions: list[dict] = []
        self._name_to_domain: dict[str, object] = {}
        for domain, definitions in domains_and_definitions:
            for definition in definitions:
                name = definition["name"]
                if name in self._name_to_domain:
                    raise ValueError(f"Duplicate Astro tool name across domains: '{name}'")
                self._name_to_domain[name] = domain
                self._tool_definitions.append(definition)

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_definitions

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        """The ONLY entry point AstroAiService's tool-use loop calls.
        Routes by exact tool name to the one domain object that declared
        it -- an unrecognized name (one no domain declared) is rejected
        here directly, never forwarded anywhere."""
        domain = self._name_to_domain.get(name)
        if domain is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        return await domain.dispatch(name, tool_input)

    async def describe_available_fields(self) -> str:
        """CRM field/operator/option vocabulary only -- the other three
        domains' tool input schemas are constrained enough (closed enums,
        simple params) that their `description` strings alone are enough
        for Claude to select and use them correctly, so they don't get an
        equivalent live-registry dump (keeps the system prompt lean)."""
        if self.crm_tools is None:
            return ""
        return await self.crm_tools.describe_available_fields()
