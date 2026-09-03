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
from app.models.mail import MailCampaign, MailCampaignStatus, MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.crm_contact_list_member_store import MemoryCrmContactListMemberStore
from app.repositories.crm_contact_list_store import MemoryCrmContactListStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.email_sequence_store import MemoryEmailSequenceStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.astro_campaign_tools import ASTRO_CAMPAIGN_TOOL_DEFINITIONS, CAMPAIGN_LIST_LIMIT, AstroCampaignTools
from app.services.campaign_service import CampaignService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService
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


def make_step(mail_campaign_id: str, **overrides) -> MailEnrollmentStep:
    defaults = dict(
        enrollment_step_id=str(uuid.uuid4()),
        mail_campaign_id=mail_campaign_id,
        enrollment_id=str(uuid.uuid4()),
        crm_contact_id=str(uuid.uuid4()),
        step_id=str(uuid.uuid4()),
        step_number=1,
        subject="Hi {{first_name}}",
        body="Hello",
        delay_days=0,
        reply_in_thread=False,
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return MailEnrollmentStep(**defaults)


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
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    enrollment_step_store = MemoryMailEnrollmentStepStore()
    sending_service = MailSendingService(
        campaign_store=mail_campaign_store,
        enrollment_store=enrollment_store,
        step_store=enrollment_step_store,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store,
        activity_log=activity_log,
    )
    mail_campaign_service = MailCampaignService(
        campaign_store=mail_campaign_store,
        step_store=step_store,
        enrollment_store=enrollment_store,
        crm_service=crm_service,
        activity_log=activity_log,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        window_store=MemoryMailSendWindowStore(),
        enrollment_step_store=enrollment_step_store,
        sending_service=sending_service,
        batch_store=MemoryMailEnrollmentBatchStore(),
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


# --- get_campaign: Astronomic Mail real execution data ---------------------
#
# Regression coverage for the exact scenario this fix exists for: a
# campaign that genuinely completed a real Gmail send used to still be
# described by Astro as "cannot send email yet," because the tool never
# queried real MailEnrollmentStep data at all -- only the pre-send,
# always-theoretical Review projection.


async def test_draft_campaign_with_no_execution_rows_reports_truthful_zero_execution(tools):
    """No fabricated activity -- a campaign that was never activated has
    zero MailEnrollmentStep rows, and must report exactly that, not omit
    the block or invent a number."""
    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})
    execution = result["campaign"]["execution"]
    assert execution["total_steps"] == 0
    assert execution["sent"] == 0
    for status in MailEnrollmentStepStatus:
        assert execution[status.value] == 0


async def test_completed_campaign_with_one_sent_step_reports_actual_sent_count(tools):
    """The exact scenario we hit in production: a COMPLETED campaign with
    one real, persisted SENT step must report sent=1 from that real row --
    never inferred from theoretical_total_sends."""
    mail_campaigns = await tools.mail_campaign_service.list_campaigns()
    campaign = mail_campaigns[0]
    campaign.status = MailCampaignStatus.COMPLETED
    await tools.mail_campaign_service.campaign_store.save(campaign)
    await tools.mail_campaign_service.enrollment_step_store.create(
        make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.SENT)
    )

    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})

    assert result["campaign"]["status"] == "completed"
    execution = result["campaign"]["execution"]
    assert execution["total_steps"] == 1
    assert execution["sent"] == 1
    assert execution["failed"] == 0
    # Independent of the theoretical block -- no source_list_id is set on
    # this campaign, so theoretical_total_sends is 0 while the real sent
    # count is 1. Proves "sent" is never derived from the theoretical
    # numbers.
    assert result["campaign"]["audience_theoretical"]["theoretical_total_sends"] == 0


async def test_execution_counts_every_real_status_correctly(tools):
    """FAILED/UNKNOWN/SKIPPED_SUPPRESSED (and every other status) must
    each be counted under their own real MailEnrollmentStepStatus name,
    not folded into "sent" or silently dropped."""
    mail_campaigns = await tools.mail_campaign_service.list_campaigns()
    campaign = mail_campaigns[0]
    step_store = tools.mail_campaign_service.enrollment_step_store
    await step_store.create(make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.SENT))
    await step_store.create(make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.FAILED))
    await step_store.create(make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.UNKNOWN))
    await step_store.create(make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.SKIPPED_SUPPRESSED))
    await step_store.create(make_step(campaign.mail_campaign_id, status=MailEnrollmentStepStatus.PENDING))

    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})
    execution = result["campaign"]["execution"]

    assert execution["total_steps"] == 5
    assert execution["sent"] == 1
    assert execution["failed"] == 1
    assert execution["unknown"] == 1
    assert execution["skipped_suppressed"] == 1
    assert execution["pending"] == 1
    assert execution["queued"] == 0
    assert execution["claimed"] == 0
    assert execution["sending"] == 0


async def test_execution_block_never_exposes_recipient_or_provider_details(tools):
    """Aggregate counts only -- no recipient email, message content,
    mailbox id, or provider message/thread id must ever appear in the
    execution block, even though the underlying MailEnrollmentStep rows
    carry that data internally."""
    mail_campaigns = await tools.mail_campaign_service.list_campaigns()
    campaign = mail_campaigns[0]
    await tools.mail_campaign_service.enrollment_step_store.create(
        make_step(
            campaign.mail_campaign_id,
            status=MailEnrollmentStepStatus.SENT,
            mailbox_id="mbx-secret",
            gmail_message_id="gmail-msg-123",
            gmail_thread_id="gmail-thread-456",
            rfc_message_id="abc123@example.com",
        )
    )

    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})
    execution = result["campaign"]["execution"]

    serialized = str(execution)
    for forbidden in ("mbx-secret", "gmail-msg-123", "gmail-thread-456", "abc123@example.com"):
        assert forbidden not in serialized
    assert set(execution.keys()) == {"note", "total_steps"} | {s.value for s in MailEnrollmentStepStatus}


async def test_theoretical_and_execution_blocks_are_both_present_and_distinct(tools):
    """The core distinction this fix exists to preserve: two separate
    keys, never merged into one number."""
    result = await tools.dispatch("get_campaign", {"name": "Q3 Outreach"})
    campaign = result["campaign"]
    assert "audience_theoretical" in campaign
    assert "execution" in campaign
    assert "theoretical" in campaign["audience_theoretical"]["note"].lower()
    assert "real" in campaign["execution"]["note"].lower()
    assert "never" in campaign["execution"]["note"].lower()  # "never derived from audience_theoretical"


def test_tool_descriptions_no_longer_claim_astronomic_mail_cannot_send():
    """Regression: the list_campaigns/get_campaign tool descriptions (and
    the module docstring, checked via the source-scanning tests below)
    used to assert categorically that Astronomic Mail "has no send/open/
    click statistics at all (it cannot send email yet)" -- now stale."""
    full_text = " ".join(t["description"] for t in ASTRO_CAMPAIGN_TOOL_DEFINITIONS).lower()
    assert "cannot send email yet" not in full_text
    assert "no sending capability" not in full_text
    assert "can send real email" in full_text or "can send" in full_text
    # Still true and must remain asserted: open/click/reply/bounce
    # tracking genuinely doesn't exist for Astronomic Mail.
    assert "open" in full_text and "click" in full_text and "reply" in full_text


def test_module_docstring_no_longer_claims_no_sending_capability():
    source = Path("app/services/astro_campaign_tools.py").read_text()
    assert "has no sending capability at all yet" not in source
    assert "CAN send real email today" in source


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
