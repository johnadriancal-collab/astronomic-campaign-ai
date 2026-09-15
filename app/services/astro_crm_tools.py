"""
Astro AI Phase 2 (CRM contacts) + Phase 3 (CRM Lists, read AND a narrow,
explicitly-approved write surface) -- the CRM's tool-use surface for
Claude.

Deliberately built on the SAME generic query engine the CRM's own "More
Filters" UI and Astro Search already use (CrmService.query_contacts +
get_filterable_fields / crm_filter_service.validate_query) rather than a
parallel CRM system. Every field, operator, and option Claude can use is
whatever that live registry says exists right now -- there is no
investor-specific or otherwise hardcoded vocabulary here; see
describe_available_fields(), which is what tells Claude (via the system
prompt) what's actually queryable.

Lists live in this SAME file rather than a separate one because they
share CrmService and the CRM filter engine -- the "count/search list
members matching a CRM filter" tools below reuse crm_filter_service's
matches_query()/validate_query() directly (the exact same investor_type
logic count_crm_contacts uses), never a second classification system.

Strictly allowlisted: AstroCrmTools.dispatch() only ever calls one of the
functions in _HANDLERS below -- an unrecognized tool name is rejected,
never dynamically resolved -- so adding any capability to Astro requires
a conscious, reviewable edit to that exact dict.

Write surface (Astro AI Phase 3, 2026-09-15 -- approved architecture,
narrow and explicit on purpose): add_crm_contact_to_list,
remove_crm_contact_from_list, update_crm_contact_investor_field, and
confirm_astro_action. This file NEVER calls CrmService.update_contact
with an arbitrary/Claude-composed patch dict -- update_contact is reached
ONLY through CrmService.apply_investor_field_change(), which accepts
exactly one of five allowlisted field keys and one already-computed
value, never a free-form dict (see that method's own docstring). List
membership writes go through CrmService.bulk_add_to_list/
bulk_remove_from_list, unchanged except for an added `source`/`actor`
parameter so the Activity Log correctly attributes an Astro-driven change.
CrmService.create_contact/archive_contact/create_contact_list/
update_contact_list/delete_contact_list remain completely unreachable
from this file, same as before Phase 3.

Two operations execute immediately once Contact/List resolution is exact
and unambiguous (add_crm_contact_to_list; update_crm_contact_investor_field
with field=investment_industry, operation=add_value) -- both additive,
reversible, and idempotent. Every other write (remove_crm_contact_from_list;
every other update_crm_contact_investor_field operation;
mark_crm_contact_engagement_attendance in astro_client_crm_tools.py) is
PROPOSED into AstroPendingActionStore and only actually runs when
confirm_astro_action is called with that proposal's id in a LATER turn --
see that store's own module docstring for why this is a real, server-side
gate, not Claude merely being asked nicely to confirm first.

ActivityLogService.record() is called for every successful write (list
add/remove reuse their existing CrmService-level call sites;
apply_investor_field_change extends update_contact's own existing single
call rather than adding a second one) -- always attributed
source=ActivitySource.ASTRO_AI, actor="astro_ai".
"""

from typing import Any

from loguru import logger

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact, CrmContactListSummary, FilterCondition, FilterQuery
from app.services.activity_log_service import ActivityLogService
from app.services.astro_export_store import AstroExportStore
from app.services.astro_pending_action_store import AstroPendingActionStore, PendingActionStatus
from app.services.crm_filter_service import FilterValidationError, matches_query, validate_query
from app.services.crm_service import (
    CrmContactListNotFound,
    CrmService,
    InvestorFieldConflict,
    InvestorFieldError,
)
from app.services.csv_export import build_csv, build_export_columns, build_export_filename

# Anthropic tool-use schemas. `filters`' shape is intentionally generic
# (field/operator/value) rather than one property per CRM field, so this
# never needs to change when a custom field is added or removed -- Claude
# is told the live field/operator/option vocabulary separately, via
# describe_available_fields() in the system prompt, not baked into this
# schema.
_FILTER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": "A field key from the CRM field list in your instructions, e.g. 'city' or 'custom:investor_type'. Never invent a field that isn't listed there.",
        },
        "operator": {
            "type": "string",
            "description": "One of that field's allowed operators from the same list, e.g. 'eq', 'contains', 'contains_any', 'gte'.",
        },
        "value": {
            "description": "The value (or list of values) to compare against. Must be one of the field's listed options for a select-type field."
        },
    },
    "required": ["field", "operator"],
}

CRM_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "count_crm_contacts",
        "description": (
            "Count CRM contacts matching zero or more filter conditions. Returns ONLY a total "
            "count, never contact records -- use this for any 'how many' question about the CRM. "
            "Leave filters empty to count every contact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "array", "items": _FILTER_ITEM_SCHEMA},
                "logic": {
                    "type": "string",
                    "enum": ["AND", "OR"],
                    "description": "How multiple filters combine. Defaults to AND.",
                },
            },
            "required": ["filters"],
        },
    },
    {
        "name": "search_crm_contacts",
        "description": (
            "Search for CRM contacts matching filter conditions. Returns at most 20 contacts "
            "plus the true total match count -- e.g. 'total: 143, returned: 20' -- so you can "
            "tell the user how many actually matched without every record being sent to you. "
            "Use this when the user wants to see/find specific contacts, not just a count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "array", "items": _FILTER_ITEM_SCHEMA},
                "logic": {"type": "string", "enum": ["AND", "OR"]},
                "limit": {
                    "type": "integer",
                    "description": "Max contacts to return. Capped at 20 regardless of what you request.",
                },
            },
            "required": ["filters"],
        },
    },
    {
        "name": "get_crm_contact",
        "description": (
            "Look up the CRM record for one specific, named person. Provide whatever identifying "
            "detail you have (first/last name, and company or email if known). If more than one "
            "contact could match, this returns an 'ambiguous' result listing the possible matches "
            "instead of picking one -- tell the user multiple contacts matched and ask them to "
            "narrow it down, never guess which one they meant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string", "description": "Optional, narrows the match."},
                "email": {"type": "string", "description": "Optional, narrows the match to an exact email."},
            },
            "required": [],
        },
    },
    {
        "name": "list_crm_lists",
        "description": (
            "List every named CRM contact list (name, description, and its current member "
            "count). Returns the true total, capped at 50."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_crm_list",
        "description": (
            "Look up one CRM list by its exact name. List names are NOT guaranteed unique -- if "
            "more than one list shares that exact name, this returns an 'ambiguous' result with "
            "the possible matches instead of picking one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_crm_list_members",
        "description": (
            "Get the contacts in one named CRM list, optionally narrowed by the SAME CRM filter "
            "conditions count_crm_contacts/search_crm_contacts use -- e.g. to answer 'angel "
            "investors in the Hotshot list' in one call, pass list_name='Hotshot' and filters=[{"
            "field: custom:investor_type, operator: contains_any, value: [Angel Investor]}]. "
            "Returns at most 20 contacts plus the true total match count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string"},
                "filters": {"type": "array", "items": _FILTER_ITEM_SCHEMA},
                "logic": {"type": "string", "enum": ["AND", "OR"]},
                "limit": {
                    "type": "integer",
                    "description": "Max contacts to return. Capped at 20 regardless of what you request.",
                },
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "count_crm_list_members",
        "description": (
            "Count contacts in one named CRM list, optionally narrowed by CRM filter conditions "
            "-- e.g. 'how many angel investors are in the Hotshot list' resolves in one call: "
            "list_name='Hotshot', filters=[{field: custom:investor_type, operator: contains_any, "
            "value: [Angel Investor]}]. Returns ONLY a total, never contact records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {"type": "string"},
                "filters": {"type": "array", "items": _FILTER_ITEM_SCHEMA},
                "logic": {"type": "string", "enum": ["AND", "OR"]},
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "export_crm_contacts",
        "description": (
            "Export the COMPLETE set of CRM contacts matching zero or more filter conditions as "
            "a downloadable CSV file -- the same filter/logic shape as count_crm_contacts/"
            "search_crm_contacts, but the export is NEVER limited to the 20-contact search "
            "preview: if 287 contacts match, the CSV contains all 287. Use this whenever the "
            "user asks to export, download, or get a CSV of a set of contacts they've been "
            "discussing -- reuse the SAME filters already established in the conversation for "
            "phrases like 'export them', 'export those', or 'download this list'; only ask the "
            "user to clarify first if the reference to a prior result is genuinely ambiguous. "
            "Leave filters empty to export every CRM contact. If the match count exceeds "
            "10,000, nothing is exported -- you'll get a 'too_large' result and should ask the "
            "user to narrow their criteria rather than exporting a truncated or partial file. "
            "On success you get back file metadata only (filename/contact_count/an opaque "
            "export id) -- never the contact rows themselves, and never a download URL. The "
            "download link is attached and rendered automatically; do not try to describe, "
            "construct, or mention a URL yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "array", "items": _FILTER_ITEM_SCHEMA},
                "logic": {"type": "string", "enum": ["AND", "OR"]},
                "label": {
                    "type": "string",
                    "description": (
                        "A short, human-readable name for this segment, e.g. 'Austin Angel "
                        "Investors' -- used only to generate a friendly filename, never a "
                        "literal file path."
                    ),
                },
            },
            "required": ["filters"],
        },
    },
    {
        "name": "get_crm_contact_lists",
        "description": (
            "Which CRM Lists is this ONE specific, named person currently a member of. Same "
            "identifying-detail rules as get_crm_contact (first/last name, optionally company/"
            "email to narrow; 'ambiguous' means more than one contact matched -- ask which one, "
            "never guess). Returns the real, current list-member rows only -- never infers "
            "membership from anything else (title, company, investor type, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string", "description": "Optional, narrows the match."},
                "email": {"type": "string", "description": "Optional, narrows the match to an exact email."},
            },
            "required": [],
        },
    },
    {
        "name": "add_crm_contact_to_list",
        "description": (
            "Add ONE specific, named person to ONE specific, exactly-named CRM List. Executes "
            "immediately once both the Contact and the List resolve to exactly one match -- this "
            "is additive, reversible, and idempotent (adding someone already on the list is a "
            "clean no-op, never a duplicate membership or an error). If the Contact OR the List "
            "is ambiguous (more than one match) or not found, nothing is added -- tell the user "
            "and ask them to narrow it down; never guess which Contact or List was meant, and "
            "never create a new List yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string"},
                "email": {"type": "string", "description": "Preferred over name when you have it -- resolves exactly, never ambiguously."},
                "list_name": {"type": "string", "description": "The List's exact name."},
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "remove_crm_contact_from_list",
        "description": (
            "Remove ONE specific, named person from ONE specific, exactly-named CRM List. Unlike "
            "add_crm_contact_to_list, this does NOT execute immediately -- it PROPOSES the removal "
            "and returns a pending_action_id plus a plain-language description of exactly what "
            "would change. State that description to the user and wait for their explicit "
            "confirmation (e.g. 'yes', 'confirm', 'do it') in a LATER message before calling "
            "confirm_astro_action with that id -- never call confirm_astro_action in the same "
            "turn you proposed it, and never assume confirmation from anything other than the "
            "user's own next message. Removing someone already absent from the list is a clean "
            "no-op once confirmed. If the Contact or the List is ambiguous or not found, nothing "
            "is proposed -- ask the user to narrow it down first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string"},
                "email": {"type": "string"},
                "list_name": {"type": "string", "description": "The List's exact name."},
            },
            "required": ["list_name"],
        },
    },
    {
        "name": "update_crm_contact_investor_field",
        "description": (
            "Change ONE specific investor field on ONE specific, named Contact. ONLY these five "
            "fields are allowed: investment_industry, check_size_personal, check_size_institutional, "
            "deploying_capital, investor_type -- no other Contact field (and never investor_mode, "
            "which is derived automatically from investor_type and cannot be set directly). "
            "investment_industry and investor_type are multi-select: use operation='add_value' or "
            "'remove_value' with a single 'value' string -- this only adds or removes that ONE "
            "value, it never replaces the whole list, so 'add Robotics' never erases the contact's "
            "existing industries. check_size_personal, check_size_institutional, and "
            "deploying_capital use operation='set_value' -- for the two check-size fields pass the "
            "full new bucket list as 'values' (a list of strings, since a person can have more "
            "than one contiguous bucket); for deploying_capital pass a single 'value' string. "
            "check_size_personal/check_size_institutional/investor_type only accept this CRM's "
            "real registered options for that field -- an invalid value is rejected and reported, "
            "never coerced to the nearest option. investment_industry has no closed option list "
            "today, so any reasonably-formed industry name is accepted, BUT a value that matches "
            "an existing one case-insensitively (ignoring extra whitespace) is treated as already "
            "present, never added as a near-duplicate with different capitalization. "
            "add_value on investment_industry executes immediately (additive, reversible, "
            "idempotent). EVERY OTHER operation/field combination (remove_value, and every "
            "set_value) does NOT execute immediately -- it PROPOSES the change and returns a "
            "pending_action_id plus the exact before/after; state that to the user and wait for "
            "their explicit confirmation in a later message before calling confirm_astro_action. "
            "If the Contact is ambiguous or not found, propose nothing -- ask first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "company": {"type": "string"},
                "email": {"type": "string"},
                "field": {
                    "type": "string",
                    "enum": [
                        "investment_industry",
                        "check_size_personal",
                        "check_size_institutional",
                        "deploying_capital",
                        "investor_type",
                    ],
                },
                "operation": {"type": "string", "enum": ["add_value", "remove_value", "set_value"]},
                "value": {"type": "string", "description": "For add_value/remove_value, or set_value on deploying_capital."},
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For set_value on check_size_personal/check_size_institutional -- the complete new bucket list.",
                },
            },
            "required": ["field", "operation"],
        },
    },
    {
        "name": "confirm_astro_action",
        "description": (
            "Execute a write that was previously PROPOSED (by remove_crm_contact_from_list, "
            "update_crm_contact_investor_field's confirmation-gated operations, or "
            "mark_crm_contact_engagement_attendance) and returned a pending_action_id. Call this "
            "ONLY after the user has explicitly confirmed in their own message that they want the "
            "previously-stated change to happen -- never on your own initiative, never in the "
            "same turn as the proposal, and never because you inferred agreement from silence or "
            "an unrelated reply. An expired or unknown id, or one already executed, is reported "
            "back plainly (already-executed is a safe no-op, never re-applied) -- never retried "
            "blindly; if expired, tell the user and offer to propose the change again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pending_action_id": {"type": "string"},
            },
            "required": ["pending_action_id"],
        },
    },
]

EXPORT_MAX_CONTACTS = 10_000

SEARCH_RESULT_LIMIT = 20
_LOOKUP_CANDIDATE_LIMIT = 5
LIST_REGISTRY_LIMIT = 50
# Internal cap on how many of a list's members are fetched before applying
# an optional CRM filter in Python (matches_query has no store-level/SQL
# form -- see crm_filter_service.py). NOT sent to Claude; only the final
# (already-≤SEARCH_RESULT_LIMIT) result and total are. Fine at today's
# list sizes (single digits to ~50 members); if a list ever genuinely grew
# past this, count_crm_list_members/get_crm_list_members would silently
# undercount rather than error -- visible technical debt, not addressed in
# this phase per explicit scope (no caching/store-level filtering added).
_LIST_MEMBER_SCAN_CAP = 1_000


def _project_summary(contact: CrmContact) -> dict:
    """Minimal projection for search results / ambiguous-match candidates --
    just enough to recognize/distinguish a person, never the full record."""
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    return {
        "name": name or None,
        "title": contact.title,
        "company": contact.company,
        "city": contact.city,
        "state": contact.state,
        "email": contact.email,
    }


def _project_full(contact: CrmContact) -> dict:
    """A single confirmed contact's answer-relevant fields -- still not the
    full ~39-field record (no source_snapshot, no raw thesis question
    lists, no custom_fields dump).

    check_size_personal/check_size_institutional, deploying_capital, and
    investment_industry are the real canonical custom_fields (see the
    2026-08-06 Check Size consolidation in crm_service.py/crm_migration.py)
    -- previously omitted here entirely, which is why Astro would claim a
    contact had no check size on record even when the CRM UI showed one.
    Personal and institutional check sizes are kept as two distinct keys
    (never merged/prioritized into one) since a contact can have either,
    both, or neither, exactly like the CRM detail page's own "Check Size
    (Personal)"/"Check Size (Institutional)" labeling.

    "company_industry" (contact.industry, an Apollo-style field describing
    the industry of the company the contact WORKS AT) is deliberately kept
    separate from "investment_industry" (custom_fields, the industries the
    contact INVESTS in) -- these were previously conflated under one
    "industry" key, which risked Astro answering an investment-focus
    question from the wrong field.

    dinners_attended is the legacy free-text custom field (distinct from
    the canonical, EngagementParticipant-derived Contact Event History,
    which this tool does not expose)."""
    data = _project_summary(contact)
    data.update(
        {
            "investor_type": contact.custom_fields.get("investor_type"),
            "investor_mode": contact.thesis_investor_mode,
            "company_industry": contact.industry,
            "investment_industry": contact.custom_fields.get("investment_industry"),
            "check_size_personal": contact.custom_fields.get("check_size_personal"),
            "check_size_institutional": contact.custom_fields.get("check_size_institutional"),
            "deploying_capital": contact.custom_fields.get("deploying_capital"),
            "dinners_attended": contact.custom_fields.get("dinners_attended"),
            "linkedin_url": contact.linkedin_url,
            "phone": contact.phone,
        }
    )
    return data


def _project_list(contact_list: CrmContactListSummary) -> dict:
    return {
        "list_id": contact_list.list_id,
        "name": contact_list.name,
        "description": contact_list.description,
        "contact_count": contact_list.contact_count,
    }


def _parse_filters(raw_filters: list[dict] | None) -> list[FilterCondition]:
    return [
        FilterCondition(field=f["field"], operator=f["operator"], value=f.get("value"))
        for f in (raw_filters or [])
    ]


async def resolve_crm_contact(crm_service: CrmService, tool_input: dict) -> dict:
    """The ONE contact-resolution rule every Astro tool that needs to find
    a specific person shares -- get_crm_contact (this file) AND, via this
    same function, astro_client_crm_tools.py's read/write tools. Kept as a
    single module-level function rather than duplicated per tool file so
    "exact/unambiguous or ask" behaves identically everywhere a contact
    must be resolved, including every write tool's resolution step
    (Phase 3's explicit "prefer email/id; a name search must return
    exactly one candidate before a write can proceed; no fuzzy matching"
    rule).

    Returns raw CrmContact objects (never the projected dict shape) --
    callers that only need to answer a question project with
    _project_full/_project_summary themselves; callers that need to WRITE
    something use contact.crm_contact_id directly. Result shape:
    {"status": "found", "contact": CrmContact}
    {"status": "ambiguous", "total": int, "candidates": list[CrmContact]}
    {"status": "not_found"}
    {"status": "invalid_filter", "message": str}"""
    first_name = (tool_input.get("first_name") or "").strip()
    last_name = (tool_input.get("last_name") or "").strip()
    company = (tool_input.get("company") or "").strip()
    email = (tool_input.get("email") or "").strip()

    if not first_name and not last_name and not email:
        return {
            "status": "invalid_filter",
            "message": "Need at least a first/last name or an email to look someone up.",
        }

    filters = []
    if first_name:
        filters.append(FilterCondition(field="first_name", operator="contains", value=first_name))
    if last_name:
        filters.append(FilterCondition(field="last_name", operator="contains", value=last_name))
    if company:
        filters.append(FilterCondition(field="company", operator="contains", value=company))
    if email:
        filters.append(FilterCondition(field="email", operator="eq", value=email))

    query = FilterQuery(filters=filters, logic="AND", page=1, page_size=_LOOKUP_CANDIDATE_LIMIT + 1)
    page = await crm_service.query_contacts(query)

    if page.total == 0:
        return {"status": "not_found"}
    if page.total == 1:
        return {"status": "found", "contact": page.items[0]}
    return {"status": "ambiguous", "total": page.total, "candidates": page.items[:_LOOKUP_CANDIDATE_LIMIT]}


class AstroCrmTools:
    """CRM tool surface for Astro AI's Claude tool-use loop. Phase 2/3 read
    tools all ultimately call CrmService.query_contacts, the same
    validated, registry-driven engine the CRM's "More Filters" UI and Astro
    Search already use -- no parallel query path, no raw SQL.

    Phase 3 (2026-09-15) adds a NARROW, explicitly-approved write surface:
    add/remove CRM List membership and one field-scoped investor-field
    update tool -- never CrmService.update_contact directly, never an
    arbitrary patch dict from Claude (see update_crm_contact_investor_field's
    own docstring). Two operations execute immediately after exact
    resolution (add-to-list, add an investment_industry value -- both
    additive/reversible/idempotent); every other write goes through
    AstroPendingActionStore first (propose -> Astro states the exact
    change and asks the user to confirm -> confirm_astro_action executes
    it once) -- see that store's own module docstring."""

    def __init__(
        self,
        crm_service: CrmService,
        export_store: AstroExportStore | None = None,
        activity_log_service: ActivityLogService | None = None,
        pending_action_store: AstroPendingActionStore | None = None,
    ):
        self.crm_service = crm_service
        # All optional so tests/callers that don't exercise export/write
        # tools can keep constructing this with just a CrmService, matching
        # every other tool in this file -- production wiring (app/main.py)
        # always provides all four. See the module docstring for why
        # activity_log_service/pending_action_store are the documented
        # write-capable exceptions.
        self.export_store = export_store
        self.activity_log_service = activity_log_service
        self.pending_action_store = pending_action_store

    async def describe_available_fields(self) -> str:
        """Live field/operator/option vocabulary, rendered for the system
        prompt -- this (not this module's Python code) is what teaches
        Claude what's actually queryable right now, so a newly added or
        removed custom field is reflected immediately with no code change."""
        registry = await self.crm_service.get_filterable_fields()
        lines = []
        for field in registry:
            if field.options:
                lines.append(f"- {field.key} ({field.type.value}): {', '.join(field.options)}")
            else:
                lines.append(f"- {field.key} ({field.type.value})")
        return "\n".join(lines)

    async def dispatch(self, name: str, tool_input: dict) -> dict:
        """The ONLY entry point Astro AI's tool-use loop calls. `name` is
        looked up in a fixed dict -- never dynamically resolved/imported/
        eval'd -- so Claude cannot invoke anything beyond exactly these
        three functions no matter what tool name it requests."""
        handler = _HANDLERS.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"'{name}' is not an available tool."}
        try:
            return await handler(self, tool_input or {})
        except FilterValidationError as e:
            # Unknown/disallowed field or operator, or a value outside a
            # select field's real options -- a "missing/unknown CRM field"
            # problem, distinct from a genuine tool/database failure below.
            return {"error": "invalid_filter", "message": str(e)}
        except (KeyError, TypeError, ValueError) as e:
            return {"error": "invalid_filter", "message": f"Malformed tool input: {e}"}
        except Exception as e:  # noqa: BLE001 -- must never crash the chat turn
            logger.error(f"Astro CRM tool '{name}' failed: {type(e).__name__}")
            return {"error": "tool_failed", "message": "The CRM lookup failed -- please try again."}

    async def _count_crm_contacts(self, tool_input: dict) -> dict:
        filters = _parse_filters(tool_input.get("filters"))
        query = FilterQuery(filters=filters, logic=tool_input.get("logic", "AND"), page=1, page_size=1)
        page = await self.crm_service.query_contacts(query)
        return {"total": page.total}

    async def _search_crm_contacts(self, tool_input: dict) -> dict:
        filters = _parse_filters(tool_input.get("filters"))
        requested_limit = int(tool_input.get("limit") or SEARCH_RESULT_LIMIT)
        limit = max(1, min(requested_limit, SEARCH_RESULT_LIMIT))
        query = FilterQuery(filters=filters, logic=tool_input.get("logic", "AND"), page=1, page_size=limit)
        page = await self.crm_service.query_contacts(query)
        return {
            "total": page.total,
            "returned": len(page.items),
            "contacts": [_project_summary(c) for c in page.items],
        }

    async def _get_crm_contact(self, tool_input: dict) -> dict:
        resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if resolution["status"] == "invalid_filter":
            return {"error": "invalid_filter", "message": resolution["message"]}
        if resolution["status"] == "not_found":
            return {"status": "not_found"}
        if resolution["status"] == "found":
            return {"status": "found", "contact": _project_full(resolution["contact"])}
        return {
            "status": "ambiguous",
            "total": resolution["total"],
            "candidates": [_project_summary(c) for c in resolution["candidates"]],
        }

    async def _resolve_list_by_name(self, name: str) -> dict:
        """Shared by get_crm_list / get_crm_list_members / count_crm_list_members.
        Exact, case-insensitive match only -- list names are confirmed NOT
        unique (see CrmContactListStore's own docstring), so 2+ matches is
        a real, expected case, never silently resolved to one."""
        lists = await self.crm_service.list_contact_lists()
        matches = [l for l in lists if l.name.strip().lower() == name.strip().lower()]
        if not matches:
            return {"status": "not_found"}
        if len(matches) == 1:
            return {"status": "found", "list": matches[0]}
        return {
            "status": "ambiguous",
            "total": len(matches),
            "candidates": [_project_list(l) for l in matches[:_LOOKUP_CANDIDATE_LIMIT]],
        }

    async def _list_crm_lists(self, tool_input: dict) -> dict:
        lists = await self.crm_service.list_contact_lists()
        total = len(lists)
        returned = lists[:LIST_REGISTRY_LIMIT]
        return {"total": total, "returned": len(returned), "lists": [_project_list(l) for l in returned]}

    async def _get_crm_list(self, tool_input: dict) -> dict:
        name = (tool_input.get("name") or "").strip()
        if not name:
            return {"error": "invalid_filter", "message": "Provide a list name to look up."}
        resolved = await self._resolve_list_by_name(name)
        if resolved["status"] == "found":
            return {"status": "found", "list": _project_list(resolved["list"])}
        return resolved

    async def _get_crm_list_members(self, tool_input: dict) -> dict:
        name = (tool_input.get("list_name") or "").strip()
        if not name:
            return {"error": "invalid_filter", "message": "Provide a list_name to look up its members."}
        resolved = await self._resolve_list_by_name(name)
        if resolved["status"] != "found":
            return resolved
        contact_list = resolved["list"]

        requested_limit = int(tool_input.get("limit") or SEARCH_RESULT_LIMIT)
        limit = max(1, min(requested_limit, SEARCH_RESULT_LIMIT))
        raw_filters = tool_input.get("filters")

        if raw_filters:
            filters = _parse_filters(raw_filters)
            query = FilterQuery(filters=filters, logic=tool_input.get("logic", "AND"))
            registry = await self.crm_service.get_filterable_fields()
            field_by_key = validate_query(query, registry)
            page = await self.crm_service.get_list_contacts(
                contact_list.list_id, page=1, page_size=_LIST_MEMBER_SCAN_CAP
            )
            matched = [c for c in page.items if matches_query(c, query, field_by_key)]
            total = len(matched)
            returned = matched[:limit]
        else:
            page = await self.crm_service.get_list_contacts(contact_list.list_id, page=1, page_size=limit)
            total = page.total
            returned = page.items

        return {
            "status": "found",
            "list": {"list_id": contact_list.list_id, "name": contact_list.name},
            "total": total,
            "returned": len(returned),
            "contacts": [_project_summary(c) for c in returned],
        }

    async def _count_crm_list_members(self, tool_input: dict) -> dict:
        name = (tool_input.get("list_name") or "").strip()
        if not name:
            return {"error": "invalid_filter", "message": "Provide a list_name to count its members."}
        resolved = await self._resolve_list_by_name(name)
        if resolved["status"] != "found":
            return resolved
        contact_list = resolved["list"]
        raw_filters = tool_input.get("filters")

        if not raw_filters:
            # contact_count is already computed by CrmService for every
            # list -- reuse it directly rather than re-deriving.
            return {
                "status": "found",
                "list": {"list_id": contact_list.list_id, "name": contact_list.name},
                "total": contact_list.contact_count,
            }

        filters = _parse_filters(raw_filters)
        query = FilterQuery(filters=filters, logic=tool_input.get("logic", "AND"))
        registry = await self.crm_service.get_filterable_fields()
        field_by_key = validate_query(query, registry)
        page = await self.crm_service.get_list_contacts(contact_list.list_id, page=1, page_size=_LIST_MEMBER_SCAN_CAP)
        matched_total = sum(1 for c in page.items if matches_query(c, query, field_by_key))
        return {
            "status": "found",
            "list": {"list_id": contact_list.list_id, "name": contact_list.name},
            "total": matched_total,
        }

    async def _export_crm_contacts(self, tool_input: dict) -> dict:
        if self.export_store is None:
            # Only reachable if a caller constructs this class without an
            # export_store -- production wiring always provides one.
            return {"error": "tool_failed", "message": "Export isn't available right now -- please try again."}

        filters = _parse_filters(tool_input.get("filters"))
        logic = tool_input.get("logic", "AND")
        label = (tool_input.get("label") or "").strip()

        # Probe-then-fetch-all -- the SAME pattern frontend/lib/crm-bulk-selection.ts's
        # fetchAllMatchingContacts() already uses to get a complete matching set
        # without guessing a page size up front.
        probe_query = FilterQuery(filters=filters, logic=logic, page=1, page_size=1)
        probe_page = await self.crm_service.query_contacts(probe_query)
        total = probe_page.total

        if total == 0:
            return {"status": "no_matches"}
        if total > EXPORT_MAX_CONTACTS:
            # Hard reject -- never a partial/truncated export.
            return {
                "error": "too_large",
                "total": total,
                "limit": EXPORT_MAX_CONTACTS,
                "message": (
                    f"{total} contacts match, which is over the {EXPORT_MAX_CONTACTS}-contact "
                    "export limit. Ask the user to narrow their criteria before exporting."
                ),
            }

        full_query = FilterQuery(filters=filters, logic=logic, page=1, page_size=total)
        full_page = await self.crm_service.query_contacts(full_query)
        contacts = full_page.items

        custom_fields = await self.crm_service.list_custom_fields(include_inactive=False)
        columns = build_export_columns(custom_fields)
        csv_text = build_csv(columns, contacts)
        filename = build_export_filename(label or _default_export_label(filters))

        export_id = self.export_store.put(
            filename=filename, contact_count=len(contacts), csv_bytes=csv_text.encode("utf-8")
        )

        if self.activity_log_service is not None:
            segment_description = label or _describe_filters(filters, logic)
            await self.activity_log_service.record(
                event_type="contacts.exported",
                category=ActivityCategory.EXPORTS,
                source=ActivitySource.ASTRO_AI,
                summary=f"{len(contacts)} contacts exported via Astro AI ({segment_description}).",
                metadata={"contact_count": len(contacts), "format": "csv", "segment": segment_description},
            )

        return {
            "status": "ready",
            "export_id": export_id,
            "filename": filename,
            "contact_count": len(contacts),
        }

    # --- Phase 3: CRM List membership + investor field writes --------------

    async def _get_crm_contact_lists(self, tool_input: dict) -> dict:
        resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if resolution["status"] != "found":
            return _contact_resolution_response(resolution)
        contact = resolution["contact"]

        list_ids = await self.crm_service.get_list_ids_for_contact(contact.crm_contact_id)
        lists = []
        for list_id in list_ids:
            try:
                summary = await self.crm_service.get_contact_list(list_id)
            except CrmContactListNotFound:
                # A stale membership row pointing at a since-deleted list --
                # skip it rather than crash the whole lookup over one bad row.
                continue
            lists.append({"list_id": summary.list_id, "name": summary.name})

        return {"status": "found", "contact": {"name": _contact_name(contact)}, "lists": lists}

    async def _add_crm_contact_to_list(self, tool_input: dict) -> dict:
        list_name = (tool_input.get("list_name") or "").strip()
        if not list_name:
            return {"error": "invalid_filter", "message": "Provide a list_name to add this contact to."}

        contact_resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if contact_resolution["status"] != "found":
            return _contact_resolution_response(contact_resolution)
        list_resolution = await self._resolve_list_by_name(list_name)
        if list_resolution["status"] != "found":
            return _list_resolution_response(list_resolution)

        contact = contact_resolution["contact"]
        contact_list = list_resolution["list"]
        result = await self.crm_service.bulk_add_to_list(
            contact_list.list_id, [contact.crm_contact_id], actor="astro_ai", source=ActivitySource.ASTRO_AI
        )
        return {
            "status": "added" if result.added else "already_member",
            "contact": {"name": _contact_name(contact)},
            "list": {"list_id": contact_list.list_id, "name": contact_list.name},
        }

    async def _remove_crm_contact_from_list(self, tool_input: dict) -> dict:
        if self.pending_action_store is None:
            return {"error": "tool_failed", "message": "Confirmation isn't available right now -- please try again."}

        list_name = (tool_input.get("list_name") or "").strip()
        if not list_name:
            return {"error": "invalid_filter", "message": "Provide a list_name to remove this contact from."}

        contact_resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if contact_resolution["status"] != "found":
            return _contact_resolution_response(contact_resolution)
        list_resolution = await self._resolve_list_by_name(list_name)
        if list_resolution["status"] != "found":
            return _list_resolution_response(list_resolution)

        contact = contact_resolution["contact"]
        contact_list = list_resolution["list"]
        contact_name = _contact_name(contact)

        current_list_ids = await self.crm_service.get_list_ids_for_contact(contact.crm_contact_id)
        if contact_list.list_id not in current_list_ids:
            return {
                "status": "already_absent",
                "message": f'{contact_name} is not currently a member of "{contact_list.name}" -- nothing to remove.',
            }

        description = f'Remove {contact_name} from the "{contact_list.name}" list.'

        async def _execute() -> dict:
            result = await self.crm_service.bulk_remove_from_list(
                contact_list.list_id, [contact.crm_contact_id], actor="astro_ai", source=ActivitySource.ASTRO_AI
            )
            return {
                "status": "removed" if result.removed else "already_absent",
                "contact": {"name": contact_name},
                "list": {"list_id": contact_list.list_id, "name": contact_list.name},
            }

        pending_action_id = self.pending_action_store.put(
            tool_name="remove_crm_contact_from_list",
            description=description,
            before="member",
            after="not a member",
            execute=_execute,
        )
        return {
            "status": "pending_confirmation",
            "pending_action_id": pending_action_id,
            "description": description,
        }

    async def _update_crm_contact_investor_field(self, tool_input: dict) -> dict:
        field = tool_input.get("field")
        operation = tool_input.get("operation")
        value = tool_input.get("value")
        values = tool_input.get("values")

        contact_resolution = await resolve_crm_contact(self.crm_service, tool_input)
        if contact_resolution["status"] != "found":
            return _contact_resolution_response(contact_resolution)
        contact = contact_resolution["contact"]

        try:
            before, after = await self.crm_service.compute_investor_field_change(contact, field, operation, value, values)
        except InvestorFieldError as e:
            return {"error": "invalid_field_or_value", "message": str(e)}

        if before == after:
            return {"status": "no_change", "field": field, "value": before}

        contact_name = _contact_name(contact)
        direct_execute = field == "investment_industry" and operation == "add_value"

        if direct_execute:
            try:
                updated = await self.crm_service.apply_investor_field_change(
                    contact.crm_contact_id, field, before, after, actor="astro_ai"
                )
            except InvestorFieldConflict:
                return {
                    "error": "target_changed",
                    "message": f"'{field}' changed since it was looked up -- please check the current value and try again.",
                }
            return {"status": "updated", "field": field, "before": before, "after": updated.custom_fields.get(field)}

        if self.pending_action_store is None:
            return {"error": "tool_failed", "message": "Confirmation isn't available right now -- please try again."}

        description = _describe_investor_field_change(contact_name, field, operation, before, after)

        async def _execute() -> dict:
            try:
                updated = await self.crm_service.apply_investor_field_change(
                    contact.crm_contact_id, field, before, after, actor="astro_ai"
                )
            except InvestorFieldConflict:
                return {
                    "error": "target_changed",
                    "message": f"'{field}' changed since this was proposed -- please check the current value and try again.",
                }
            return {"status": "updated", "field": field, "before": before, "after": updated.custom_fields.get(field)}

        pending_action_id = self.pending_action_store.put(
            tool_name="update_crm_contact_investor_field",
            description=description,
            before=before,
            after=after,
            execute=_execute,
        )
        return {
            "status": "pending_confirmation",
            "pending_action_id": pending_action_id,
            "description": description,
            "field": field,
            "before": before,
            "after": after,
        }

    async def _confirm_astro_action(self, tool_input: dict) -> dict:
        if self.pending_action_store is None:
            return {"error": "tool_failed", "message": "Confirmation isn't available right now -- please try again."}

        pending_action_id = (tool_input.get("pending_action_id") or "").strip()
        if not pending_action_id:
            return {"error": "invalid_filter", "message": "Provide a pending_action_id."}

        pending = self.pending_action_store.get(pending_action_id)
        if pending is None:
            return {
                "status": "not_found",
                "message": "This action has expired or no longer exists -- ask the user what they'd "
                "like to do, then propose it again from scratch.",
            }
        if pending.status == PendingActionStatus.EXECUTED:
            return {"status": "already_executed", "message": "This action was already carried out -- nothing more to do."}

        result = await pending.execute()
        self.pending_action_store.mark_executed(pending_action_id)
        return {"status": "confirmed", "result": result}


def _contact_name(contact: CrmContact) -> str:
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
    return name or "This contact"


def _contact_resolution_response(resolution: dict) -> dict:
    """Shared failure-branch shape for every tool that resolves a Contact
    via resolve_crm_contact() -- "not_found"/"ambiguous"/"invalid_filter"
    all look identical no matter which tool hit them."""
    if resolution["status"] == "not_found":
        return {"status": "not_found"}
    if resolution["status"] == "ambiguous":
        return {
            "status": "ambiguous",
            "total": resolution["total"],
            "candidates": [_project_summary(c) for c in resolution["candidates"]],
        }
    return {"error": "invalid_filter", "message": resolution["message"]}


def _list_resolution_response(resolution: dict) -> dict:
    """Same idea as _contact_resolution_response, for _resolve_list_by_name's
    result shape (already {"status": "ambiguous"/"not_found", ...} with
    _project_list-shaped candidates when ambiguous -- passed through as-is)."""
    return resolution


def _describe_investor_field_change(contact_name: str, field: str, operation: str, before: Any, after: Any) -> str:
    if operation == "add_value":
        return f'Add "{after[-1] if isinstance(after, list) and after else after}" to {contact_name}\'s {field}.'
    if operation == "remove_value":
        removed = [v for v in (before or []) if v not in (after or [])]
        removed_desc = removed[0] if len(removed) == 1 else removed
        return f'Remove "{removed_desc}" from {contact_name}\'s {field}.'
    return f"Set {contact_name}'s {field} to {after!r} (currently {before!r})."


def _default_export_label(filters: list[FilterCondition]) -> str:
    if not filters:
        return "all crm contacts"
    return " and ".join(f"{f.field} {f.value}" for f in filters)


def _describe_filters(filters: list[FilterCondition], logic: str) -> str:
    if not filters:
        return "all contacts, no filters"
    joiner = f" {logic} "
    return joiner.join(f"{f.field}={f.value}" for f in filters)


# Deliberately the ONLY tool-name -> function mapping AstroCrmTools.dispatch
# will ever consult -- adding a write/Apollo/campaign/mailbox capability to
# Astro requires a conscious, reviewable edit to this exact dict, not a
# generic dispatch mechanism that could reach one accidentally.
_HANDLERS = {
    "count_crm_contacts": AstroCrmTools._count_crm_contacts,
    "search_crm_contacts": AstroCrmTools._search_crm_contacts,
    "get_crm_contact": AstroCrmTools._get_crm_contact,
    "list_crm_lists": AstroCrmTools._list_crm_lists,
    "get_crm_list": AstroCrmTools._get_crm_list,
    "get_crm_list_members": AstroCrmTools._get_crm_list_members,
    "count_crm_list_members": AstroCrmTools._count_crm_list_members,
    "export_crm_contacts": AstroCrmTools._export_crm_contacts,
    # Phase 3 (2026-09-15) -- approved narrow read+write surface. Every
    # write name below is deliberate and reviewed; nothing more general
    # (update_contact, archive_contact, create/delete_contact_list, etc.)
    # is or will be reachable through this dict.
    "get_crm_contact_lists": AstroCrmTools._get_crm_contact_lists,
    "add_crm_contact_to_list": AstroCrmTools._add_crm_contact_to_list,
    "remove_crm_contact_from_list": AstroCrmTools._remove_crm_contact_from_list,
    "update_crm_contact_investor_field": AstroCrmTools._update_crm_contact_investor_field,
    "confirm_astro_action": AstroCrmTools._confirm_astro_action,
}
