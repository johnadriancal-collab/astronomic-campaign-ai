"""
AstroHubTools tests -- Astro AI Phase 3's multi-domain composition layer.
Proves aggregation/routing correctness across all four domains without
re-testing each domain's own internal logic (that's each astro_*_tools
test file's job).
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory, ActivitySource
from app.models.crm import CrmContact
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_activity_tools import ASTRO_ACTIVITY_TOOL_DEFINITIONS, AstroActivityTools
from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS, AstroCrmTools
from app.services.astro_hub_tools import AstroHubTools
from app.services.astro_mailbox_tools import ASTRO_MAILBOX_TOOL_DEFINITIONS, AstroMailboxTools
from app.services.crm_service import CrmService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def crm_tools():
    service = CrmService()
    await service.contact_store.create(
        CrmContact(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now(), first_name="Test")
    )
    return AstroCrmTools(service)


@pytest.fixture
def mailbox_tools():
    return AstroMailboxTools(MemoryMailboxStore())


@pytest.fixture
def activity_tools():
    return AstroActivityTools(ActivityLogService(MemoryActivityEventStore()))


async def test_tool_definitions_aggregate_all_configured_domains(crm_tools, mailbox_tools, activity_tools):
    hub = AstroHubTools(crm_tools=crm_tools, mailbox_tools=mailbox_tools, activity_tools=activity_tools)
    names = {t["name"] for t in hub.tool_definitions}
    assert names == set(t["name"] for t in CRM_TOOL_DEFINITIONS) | set(
        t["name"] for t in ASTRO_MAILBOX_TOOL_DEFINITIONS
    ) | set(t["name"] for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS)


async def test_tool_definitions_omit_unconfigured_domains(crm_tools):
    hub = AstroHubTools(crm_tools=crm_tools)
    names = {t["name"] for t in hub.tool_definitions}
    assert names == {t["name"] for t in CRM_TOOL_DEFINITIONS}
    assert "list_connected_mailboxes" not in names
    assert "search_activity" not in names


async def test_dispatch_routes_to_the_correct_domain(crm_tools, mailbox_tools, activity_tools):
    hub = AstroHubTools(crm_tools=crm_tools, mailbox_tools=mailbox_tools, activity_tools=activity_tools)

    crm_result = await hub.dispatch("count_crm_contacts", {"filters": []})
    assert crm_result == {"total": 1}

    mailbox_result = await hub.dispatch("list_connected_mailboxes", {})
    assert mailbox_result["total"] == 0

    activity_result = await hub.dispatch("search_activity", {})
    assert activity_result["total"] == 0


async def test_dispatch_rejects_unknown_tool_name_at_hub_level(crm_tools):
    hub = AstroHubTools(crm_tools=crm_tools)
    result = await hub.dispatch("delete_everything", {})
    assert result == {"error": "unknown_tool", "message": "'delete_everything' is not an available tool."}


async def test_dispatch_with_no_domains_configured_rejects_everything():
    hub = AstroHubTools()
    result = await hub.dispatch("count_crm_contacts", {})
    assert result["error"] == "unknown_tool"
    assert hub.tool_definitions == []


async def test_describe_available_fields_empty_when_no_crm_tools(mailbox_tools):
    hub = AstroHubTools(mailbox_tools=mailbox_tools)
    assert await hub.describe_available_fields() == ""


def test_duplicate_tool_name_across_domains_raises_at_construction(monkeypatch, crm_tools, mailbox_tools):
    """A load-time guard: if a future domain ever declared a name another
    domain already owns, construction must fail loudly, never silently
    let one domain's tool shadow another's."""
    monkeypatch.setattr(
        "app.services.astro_hub_tools.ASTRO_MAILBOX_TOOL_DEFINITIONS",
        [{"name": "count_crm_contacts", "description": "colliding", "input_schema": {}}],
    )
    with pytest.raises(ValueError, match="Duplicate Astro tool name"):
        AstroHubTools(crm_tools=crm_tools, mailbox_tools=mailbox_tools)


def test_full_registry_contains_exactly_the_fourteen_approved_tools(crm_tools, mailbox_tools, activity_tools):
    from app.services.astro_campaign_tools import ASTRO_CAMPAIGN_TOOL_DEFINITIONS

    all_names = (
        {t["name"] for t in CRM_TOOL_DEFINITIONS}
        | {t["name"] for t in ASTRO_MAILBOX_TOOL_DEFINITIONS}
        | {t["name"] for t in ASTRO_ACTIVITY_TOOL_DEFINITIONS}
        | {t["name"] for t in ASTRO_CAMPAIGN_TOOL_DEFINITIONS}
    )
    assert all_names == {
        "count_crm_contacts",
        "search_crm_contacts",
        "get_crm_contact",
        "list_connected_mailboxes",
        "get_mailbox",
        "search_activity",
        "list_crm_lists",
        "get_crm_list",
        "get_crm_list_members",
        "count_crm_list_members",
        "list_campaigns",
        "get_campaign",
        "count_campaigns",
        "export_crm_contacts",
    }
    assert len(all_names) == 14
