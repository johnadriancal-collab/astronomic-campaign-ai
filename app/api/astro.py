"""
Astro Core: POST /astro/command -- deterministic, Claude-free CRM
search/count, with Phase 1.1 conversational refinement layered on top. This
route makes NO Anthropic/Claude calls anywhere in its call graph
(app/services/astro_parser.py is pure text->FilterQuery, no network access at
all); it only ever talks to CrmService, the exact same service GET
/crm/filterable-fields and POST /crm/contacts/query already use.

Dispatch (Phase 1.1, 2026-08-13): the STANDALONE parser (parse()) always runs
first. Only when it returns Unresolved -- most commonly because there's no
recognized verb, e.g. "Only Austin" -- AND the caller supplied `context` does
this route fall back to the refinement parser (attempt_refinement()). This is
what makes every Phase 1 standalone command behave identically whether or not
`context` is present: a fully-formed sentence like "Find investors in
Aerospace" always resolves via parse() and is treated as a brand-new query,
discarding any prior context.

Read-only: search_contacts and count_contacts are the only two intents this
supports. No lists, no exports, no CRM writes, no Campaign Builder actions,
no backend session state of any kind -- the frontend holds the entire
conversation state (see app/models/astro.py's AstroCommandContext) and resends
it each turn.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_crm_service
from app.models.astro import AstroCommandRequest, AstroCommandResponse
from app.models.crm import FilterQuery
from app.services.astro_parser import (
    CHECK_SIZE_PERSONAL_FIELD_KEY,
    INVESTOR_TYPE_FIELD_KEY,
    ParsedCommand,
    UnresolvedCommand,
    attempt_refinement,
    parse,
)
from app.services.crm_service import CrmService

router = APIRouter(prefix="/astro", tags=["astro"])


@router.post("/command", response_model=AstroCommandResponse)
async def astro_command(req: AstroCommandRequest, service: CrmService = Depends(get_crm_service)):
    """
    Fetches the live filterable-field registry (the SAME call POST
    /crm/contacts/query's validation already depends on) so the parser's
    Investor Type and Check Size resolution are always current -- if the
    investor_type custom field's options change via the admin UI, the very
    next Astro command picks that up with no redeploy needed.
    """
    registry = await service.get_filterable_fields()
    field_by_key = {f.key: f for f in registry}

    investor_type_options = field_by_key[INVESTOR_TYPE_FIELD_KEY].options if INVESTOR_TYPE_FIELD_KEY in field_by_key else []
    check_size_ordered_options = (
        field_by_key[CHECK_SIZE_PERSONAL_FIELD_KEY].ordered_options
        if CHECK_SIZE_PERSONAL_FIELD_KEY in field_by_key
        else []
    )

    result = parse(req.text, investor_type_options, check_size_ordered_options)

    # Refinement fallback: ONLY when the standalone parse failed to resolve on
    # its own AND the caller actually has prior state to refine against.
    if isinstance(result, UnresolvedCommand) and req.context is not None and req.context.query is not None:
        result = attempt_refinement(
            req.text,
            list(req.context.query.filters),
            req.context.query.include_archived,
            req.context.intent or "search_contacts",
            investor_type_options,
            check_size_ordered_options,
        )

    if isinstance(result, UnresolvedCommand):
        # A refinement attempt that stayed Unresolved carries the exact prior
        # query (byte-for-byte) so the caller can confirm nothing changed --
        # re-run it (read-only) to report accurate, current total/contacts
        # alongside the clarification, rather than guessing at stale numbers.
        query = result.unchanged_query
        total = None
        if query is not None:
            page = await service.query_contacts(query)
            total = page.total
        return AstroCommandResponse(
            intent="unresolved",
            understood_as=", ".join(f"{k} = {v}" for k, v in result.understood.items()) or "(nothing understood yet)",
            query=query,
            total=total,
            understood=result.understood,
            unresolved_phrase=result.unresolved_phrase,
            message=result.message,
        )

    assert isinstance(result, ParsedCommand)
    query = FilterQuery(filters=result.filters, logic="AND", include_archived=result.include_archived, page_size=50)
    page = await service.query_contacts(query)
    message = result.message_template(page.total) if result.message_template else None

    if result.intent == "count_contacts":
        return AstroCommandResponse(
            intent="count_contacts",
            understood_as=result.understood_as,
            query=query,
            total=page.total,
            operation=result.operation,
            changed_field=result.changed_field,
            message=message,
        )
    return AstroCommandResponse(
        intent="search_contacts",
        understood_as=result.understood_as,
        query=query,
        total=page.total,
        contacts=page.items,
        operation=result.operation,
        changed_field=result.changed_field,
        message=message,
    )
