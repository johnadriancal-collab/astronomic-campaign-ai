"""
AstroCampaignTools tests -- Astro AI Phase 3 read-only Campaign Manager
surface. Exercised against REAL services/in-memory stores for both
sending systems (Apollo Campaign + Astronomic Mail MailCampaign), never
mocks of the underlying data path.
"""

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.models.email_sequence import EmailSequence, EmailSequenceStatus
from app.models.mail import MailCampaign, MailCampaignStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.crm_contact_list_member_store import MemoryCrmContactListMemberStore
from app.repositories.crm_contact_list_store import MemoryCrmContactListStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_campaign_tools import ASTRO_CAMPAIGN_TOOL_DEFINITIONS, CAMPAIGN_LIST_LIMIT, AstroCampaignTools
from app.services.campaign_service import CampaignService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_suppression_service import MailSuppressionService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_apollo_campaign(**overrides) -> Campaign:
    defaults = dict(
        campaign_id=str(uuid.uuid4()),
        original_prompt="find angel investors in austin",
        created_at=_now(),
        status=CampaignStatus.DRAFT,
        plan=CampaignPlan(campaign_name="Austin Forward", filters=Filters(), sequence=[]),
    )
    defaults.update(overrides)
    return Campaign(**defaults)


def make_mail_campaign(**overrides) -> MailCampaign:
    defaults = dict(
        mail_campaign_id=str(uuid.uuid4()),
        name="Q3 Outreach",
        status=MailCampaignStatus.DRAFT,
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return MailCampaign(**defaults)


@pytest_asyncio.fixture
async def tools():
    activity_log = ActivityLogService(MemoryActivityEventStore())
    campaign_store = MemoryCampaignStore()
    mail_campaign_store = MemoryMailCampaignStore()
    step_store = MemoryMailSequenceStepStore()
    enrollment_store = MemoryMailEnrollmentStore()
    suppression_store = MemoryMailSuppressionStore()
    sequence_store = MemoryEmailSequenceStore()
    crm_service = CrmService(
        contact_store=MemoryCrmContactStore(),
        list_store=MemoryCrmContactListStore(),
        list_member_store=MemoryCrmContactListMemberStore(),
        activity_log=activity_log,
    )
    mail_campaign_service = MailCampaignService(
        campaign_store=mail_campaign_store,
        step_store=step_store,
        enrollment_store=enrollment_store,
        crm_service=crm_service,
        activity_log=activity_log,
        mailbox_store=MemoryMailboxStore(),
        channel_store=MemoryMailCampaignMailboxStore(),
        window_store=MemoryMailSendWindowStore(),
    )
    mail_suppression_service = MailSuppressionService(store=suppression_store, activity_log=activity_log)

    await campaign_store.create(make_apollo_campaign(plan=CampaignPlan(campaign_name="Austin Forward", filters=Filters(), sequence=[])))
    await mail_campaign_store.create(make_mail_campaign(name="Q3 Outreach"))

    return AstroCampaignTools(
        campaign_service=CampaignService(store=campaign_store),
        mail_campaign_service=mail_campaign_service,
        mail_suppression_service=mail_suppression_service,
        email_sequence_store=sequence_store,
    )


async def test_list_campaigns_spans_both_systems(tools):
    result = await tools.dispatch("list_campaigns", {})
    assert result["total"] == 2
    methods = {c["sending_method"] for c in result["campaigns"]}
    assert methods == {"apollo", "astronomic_mail"}


async def test_list_campaigns_filters_by_sending_method(tools):
    result = await tools.dispatch("list_campaigns", {"sending_method": "apollo"})
    assert result["total"] == 1
    assert result["campaigns"][0]["name"] == "Austin Forward"


async def test_list_campaigns_filters_by_status_bucket(tools):
    result = await tools.dispatch("list_campaigns", {"status_bucket": "draft"})
    assert result["total"] == 2  # both seeded campaigns are draft


async def test_list_campaigns_result_cannot_exceed_hard_limit(tools):
    from app.repositories.campaign_store import MemoryCampaignStore as _S

    store = tools.campaign_service.store
    for i in range(CAMPAIGN_LIST_LIMIT + 5):
        await store.create(make_apollo_campaign(plan=CampaignPlan(campaign_name=f"Bulk {i}", filters=Filters(), sequence=[])))

    result = await tools.dispatch("list_campaigns", {"limit": 999})

    assert result["total"] > CAMPAIGN_LIST_LIMIT
    assert result["returned"] == CAMPAIGN_LIST_LIMIT
    assert len(result["campaigns"]) == CAMPAIGN_LIST_LIMIT


async def test_count_campaigns_returns_bare_total(tools):
    result = await tools.dispatch("count_campaigns", {})
    assert result == {"total": 2}


async def test_count_campaigns_respects_filter(tools):
    result = await tools.dispatch("count_campaigns", {"sending_method": "astronomic_mail"})
    assert result == {"total": 1}


# --- get_campaign: Apollo -----------------------------------------------


async def test_get_apollo_campaign_with_no_sequence_deployed(tools):
    result = await tools.dispatch("get_campaign", {"name": "Austin Forward"})
    assert result["status"] == "found"
    assert result["sending_method"] == "apollo"
    assert result["campaign"]["sequence_stats"] is None  # no EmailSequence exists at all


async def test_get_apollo_campaign_deployed_but_never_synced(tools):
    apollo_campaigns = await tools.campaign_service.store.list()
    campaign = apollo_campaigns[0]
    await tools.email_sequence_store.create(
        EmailSequence(
            email_sequence_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            apollo_sequence_id="apollo-seq-1",
            name="Seq",
            status=EmailSequenceStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
            last_synced_at=None,
        )
    )

    result = await tools.dispatch("get_campaign", {"name": "Austin Forward"})

    stats = result["campaign"]["sequence_stats"]
    assert stats["synced"] is False
    assert "never been synced" in stats["message"]


async def test_get_apollo_campaign_synced_reports_real_stats_and_timestamp(tools):
    apollo_campaigns = await tools.campaign_service.store.list()
    campaign = apollo_campaigns[0]
    synced_at = datetime(2026, 8, 19, tzinfo=timezone.utc)
    await tools.email_sequence_store.create(
        EmailSequence(
            email_sequence_id=str(uuid.uuid4()),
            campaign_id=campaign.campaign_id,
            apollo_sequence_id="apollo-seq-1",
            name="Seq",
            status=EmailSequenceStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
            last_synced_at=synced_at,
            unique_scheduled=100,
            unique_delivered=95,
            unique_opened=40,
            unique_clicked=10,
            unique_replied=3,
            unique_bounced=5,
            unique_unsubscribed=1,
        )
    )

    result = await tools.dispatch("get_campaign", {"name": "Austin Forward"})

    stats = result["campaign"]["sequence_stats"]
    assert stats["synced"] is True
    assert stats["last_synced_at"] == synced_at.isoformat()
    assert stats["unique_opened"] == 40
    assert stats["unique_bounced"] == 5


async def test_apollo_campaign_response_has_no_mailbox_or_crm_list_field(tools):
    """Confirms the honest data-model gap: Apollo campaigns have no
    mailbox relationship and no CRM-list relationship in this app."""
    result = await tools.dispatch("get_campaign", {"name": "Austin Forward"})
    keys = set(result["campaign"].keys())
    assert "mailbox" not in keys and "mailbox_id" not in keys
    assert "source_list_id" not in keys and "source_list_name" not in keys


# --- get_campaign: Astronomic Mail ----------------------------------------


async def test_get_mail_campaign_reports_theoretical_audience_clearly_labeled(tools):
    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})
    assert result["status"] == "found"
    assert result["sending_method"] == "astronomic_mail"
    audience = result["campaign"]["audience_theoretical"]
    assert "theoretical" in audience["note"].lower()
    assert "not actual sends" in audience["note"].lower()
    assert audience["contacts_eligible"] == 0  # no source_list_id set


async def test_mail_campaign_source_list_relationship_is_exposed(tools):
    crm_service = tools.mail_campaign_service.crm_service
    contact_list = await crm_service.create_contact_list("Austin Forward List")
    campaign = make_mail_campaign(name="Linked Campaign", source_list_id=contact_list.list_id)
    await tools.mail_campaign_service.campaign_store.create(campaign)

    result = await tools.dispatch("get_campaign", {"name": "Linked Campaign"})

    assert result["campaign"]["source_list_id"] == contact_list.list_id
    assert result["campaign"]["source_list_name"] == "Austin Forward List"


# --- ambiguity / not found -------------------------------------------------


async def test_get_campaign_not_found(tools):
    result = await tools.dispatch("get_campaign", {"name": "Nonexistent Campaign"})
    assert result == {"status": "not_found"}


async def test_get_campaign_ambiguous_across_systems_never_arbitrarily_picks_one(tools):
    """A same-named campaign in BOTH systems must be reported ambiguous,
    never silently resolved to one or the other."""
    await tools.mail_campaign_service.campaign_store.create(make_mail_campaign(name="Austin Forward"))

    result = await tools.dispatch("get_campaign", {"name": "Austin Forward"})

    assert result["status"] == "ambiguous"
    assert result["total"] == 2
    methods = {c["sending_method"] for c in result["candidates"]}
    assert methods == {"apollo", "astronomic_mail"}


async def test_get_campaign_requires_a_name(tools):
    result = await tools.dispatch("get_campaign", {})
    assert result["error"] == "invalid_filter"


# --- security / registry ----------------------------------------------------


async def test_unknown_tool_name_is_rejected(tools):
    result = await tools.dispatch("activate_campaign", {})
    assert result == {"error": "unknown_tool", "message": "'activate_campaign' is not an available tool."}


async def test_write_and_apollo_tool_names_are_not_available(tools):
    for name in [
        "create_campaign", "build_campaign", "activate_campaign", "pause_campaign",
        "search_apollo", "create_apollo_list", "create_apollo_sequence", "sync_sequence",
        "mark_ready", "archive_campaign", "unlock_campaign",
    ]:
        result = await tools.dispatch(name, {})
        assert result["error"] == "unknown_tool"


def test_no_get_campaign_stats_tool_exists():
    """Per the approved architecture: stats are folded into get_campaign,
    not a separate tool."""
    names = {t["name"] for t in ASTRO_CAMPAIGN_TOOL_DEFINITIONS}
    assert names == {"list_campaigns", "get_campaign", "count_campaigns"}
    assert "get_campaign_stats" not in names


def test_astro_campaign_tools_never_imports_apollo_client_or_write_paths():
    tree = ast.parse(Path("app/services/astro_campaign_tools.py").read_text())
    imported_modules = set()
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
    forbidden_modules = {"app.apollo", "app.apollo.client", "app.services.mailbox_service"}
    hit = forbidden_modules & imported_modules
    assert not hit, f"astro_campaign_tools.py imports forbidden module(s): {hit}"
    assert "ApolloClient" not in imported_names


def test_astro_campaign_tools_source_never_calls_write_methods():
    source = Path("app/services/astro_campaign_tools.py").read_text()
    for forbidden in [
        ".build(", ".activate(", ".pause(", ".preview(", ".search(",
        ".create_campaign(", ".mark_ready(", ".unlock_campaign(", ".archive_campaign(",
        ".suppress(", ".unsuppress(",
    ]:
        assert forbidden not in source, f"astro_campaign_tools.py must never call {forbidden}"
