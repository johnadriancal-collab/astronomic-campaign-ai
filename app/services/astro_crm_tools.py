"""
Astro AI Phase 2 (CRM contacts) + Phase 3 (CRM Lists) -- the CRM's
read-only surface for Claude tool-use.

Deliberately built on the SAME generic query engine the CRM's own "More
Filters" UI and Astro Search already use (CrmService.query_contacts +
get_filterable_fields / crm_filter_service.validate_query) rather than a
parallel CRM system. Every field, operator, and option Claude can use is
whatever that live registry says exists right now -- there is no
investor-specific or otherwise hardcoded vocabulary here; see
describe_available_fields(), which is what tells Claude (via the system
prompt) what's actually queryable.

Lists (Phase 3) live in this SAME file rather than a separate one because
they share CrmService and the CRM filter engine -- the "count/search list
members matching a CRM filter" tools below reuse crm_filter_service's
matches_query()/validate_query() directly (the exact same investor_type
logic count_crm_contacts uses), never a second classification system.

Strictly read-only and strictly allowlisted: this module imports nothing
from crm_service.py except query_contacts/get_filterable_fields/
list_contact_lists/get_contact_list/get_list_contacts/list_custom_fields
(never create_contact/update_contact/archive_contact/create_contact_list/
update_contact_list/delete_contact_list/bulk_add_to_list/
bulk_remove_from_list), imports nothing Apollo- or campaign- or
mailbox-related at all, and AstroCrmTools.dispatch() only ever calls one
of the functions in _HANDLERS below -- an unrecognized tool name is
rejected, never dynamically resolved.

ONE narrow, deliberate exception to the above (added for
export_crm_contacts, the CSV export tool): this file may call
ActivityLogService.record() SOLELY to write a single audit event after a
successful export (contact_count/format/segment description only --
never contact rows). This is the only write path reachable from Astro
AI's whole tool surface; it does not touch CrmService, and it cannot
create, modify, or delete a CRM contact, list, or anything else.
"""

from loguru import logger

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact, CrmContactListSummary, FilterCondition, FilterQuery
from app.services.activity_log_service import ActivityLogService
from app.services.astro_export_store import AstroExportStore
from app.services.crm_filter_service import FilterValidationError, matches_query, validate_query
from app.services.crm_service import CrmService
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
    lists, no custom_fields dump)."""
    data = _project_summary(contact)
    data.update(
        {
            "investor_type": contact.custom_fields.get("investor_type"),
            "investor_mode": contact.thesis_investor_mode,
            "industry": contact.industry,
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


class AstroCrmTools:
    """Read-only CRM tool surface for Astro AI's Claude tool-use loop.
    Every method here ultimately calls CrmService.query_contacts, the same
    validated, registry-driven engine the CRM's "More Filters" UI and Astro
    Search already use -- no parallel query path, no raw SQL, no write
    method is ever reachable from this class."""

    def __init__(
        self,
        crm_service: CrmService,
        export_store: AstroExportStore | None = None,
        activity_log_service: ActivityLogService | None = None,
    ):
        self.crm_service = crm_service
        # Both optional so tests/callers that don't exercise export_crm_contacts
        # can keep constructing this with just a CrmService, matching every
        # other tool in this file -- production wiring (app/main.py) always
        # provides both. See the module docstring for why activity_log_service
        # is the one documented write-capable exception.
        self.export_store = export_store
        self.activity_log_service = activity_log_service

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
        first_name = (tool_input.get("first_name") or "").strip()
        last_name = (tool_input.get("last_name") or "").strip()
        company = (tool_input.get("company") or "").strip()
        email = (tool_input.get("email") or "").strip()

        if not first_name and not last_name and not email:
            return {
                "error": "invalid_filter",
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
        page = await self.crm_service.query_contacts(query)

        if page.total == 0:
            return {"status": "not_found"}
        if page.total == 1:
            return {"status": "found", "contact": _project_full(page.items[0])}
        return {
            "status": "ambiguous",
            "total": page.total,
            "candidates": [_project_summary(c) for c in page.items[:_LOOKUP_CANDIDATE_LIMIT]],
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
}
