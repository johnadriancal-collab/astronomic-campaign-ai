"""
Astro Core Phase 1: POST /astro/command -- deterministic, Claude-free CRM
search/count. This route makes NO Anthropic/Claude calls anywhere in its
call graph (app/services/astro_parser.py is pure text->FilterQuery, no
network access at all); it only ever talks to CrmService, the exact same
service GET /crm/filterable-fields and POST /crm/contacts/query already use.
Read-only: search_contacts and count_contacts are the only two intents this
phase supports. No lists, no exports, no CRM writes, no Campaign Builder
actions, no conversational state -- each request is parsed standalone.
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

    if isinstance(result, UnresolvedCommand):
        return AstroCommandResponse(
            intent="unresolved",
            understood_as=", ".join(f"{k} = {v}" for k, v in result.understood.items()) or "(nothing understood yet)",
            understood=result.understood,
            unresolved_phrase=result.unresolved_phrase,
            message=result.message,
        )

    assert isinstance(result, ParsedCommand)
    query = FilterQuery(filters=result.filters, logic="AND", include_archived=result.include_archived, page_size=50)
    page = await service.query_contacts(query)

    if result.intent == "count_contacts":
        return AstroCommandResponse(
            intent="count_contacts", understood_as=result.understood_as, query=query, total=page.total
        )
    return AstroCommandResponse(
        intent="search_contacts", understood_as=result.understood_as, query=query, total=page.total, contacts=page.items
    )
