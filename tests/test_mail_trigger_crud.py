"""
MailTriggerService CRUD -- Stage 5D (2026-09-04). Trigger definition
create/list/update/delete, lifecycle gating, and the one-way
lead_start_mode transition. Occurrence execution itself is covered
separately in tests/test_mail_trigger_occurrence_execution.py.

Same in-memory-stores/fixture convention as
tests/test_lead_start_mode_activation_gate.py (itself reusing
test_mail_campaign_service.py's helpers via local import) -- no
conftest.py exists in this project.
"""

from datetime import datetime, time, timezone

import pytest

from app.models.mail import MailCampaignStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MemoryMailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_lead_start_trigger_store import MailLeadStartTriggerNotFoundError, MemoryMailLeadStartTriggerStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mail_trigger_occurrence_store import MemoryMailTriggerOccurrenceStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignNotFound, MailCampaignService
from app.services.mail_sending_service import MailSendingService
from app.services.mail_trigger_service import MailCampaignNotEligibleForTriggersError, MailTriggerService
from app.services import mail_campaign_service as mail_campaign_service_module
from tests.test_mail_campaign_service import _make_mailbox, _make_valid_schedule_campaign

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mail_sending_engine_enabled(monkeypatch):
    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", True)


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def crm():
    return CrmService()


@pytest.fixture
def mailbox_store():
    return MemoryMailboxStore()


@pytest.fixture
def channel_store():
    return MemoryMailCampaignMailboxStore()


@pytest.fixture
def window_store():
    return MemoryMailSendWindowStore()


@pytest.fixture
def enrollment_step_store():
    return MemoryMailEnrollmentStepStore()


@pytest.fixture
def batch_store():
    return MemoryMailEnrollmentBatchStore()


@pytest.fixture
def batch_member_store():
    return MemoryMailEnrollmentBatchMemberStore()


@pytest.fixture
def suppression_store():
    return MemoryMailSuppressionStore()


@pytest.fixture
def campaign_store():
    return MemoryMailCampaignStore()


@pytest.fixture
def enrollment_store():
    return MemoryMailEnrollmentStore()


@pytest.fixture
def trigger_store():
    return MemoryMailLeadStartTriggerStore()


@pytest.fixture
def occurrence_store():
    return MemoryMailTriggerOccurrenceStore()


@pytest.fixture
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def campaign_service(
    crm, activity_log, mailbox_store, channel_store, window_store, enrollment_step_store, suppression_store,
    batch_store, batch_member_store, campaign_store, enrollment_store, crm_import_service,
):
    sending_service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=enrollment_step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store, activity_log=activity_log,
    )
    return MailCampaignService(
        campaign_store=campaign_store, step_store=MemoryMailSequenceStepStore(), enrollment_store=enrollment_store,
        crm_service=crm, activity_log=activity_log, mailbox_store=mailbox_store, channel_store=channel_store,
        window_store=window_store, enrollment_step_store=enrollment_step_store, sending_service=sending_service,
        batch_store=batch_store, batch_member_store=batch_member_store, suppression_store=suppression_store,
        crm_import_reader=crm_import_service,
    )


@pytest.fixture
def sending_service(campaign_store, enrollment_store, enrollment_step_store, mailbox_store, channel_store, suppression_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=enrollment_step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store, activity_log=activity_log,
    )


@pytest.fixture
def trigger_service(
    trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
    sending_service, campaign_service, activity_log,
):
    return MailTriggerService(
        trigger_store=trigger_store, occurrence_store=occurrence_store, campaign_store=campaign_store,
        enrollment_store=enrollment_store, enrollment_step_store=enrollment_step_store,
        suppression_store=suppression_store, sending_service=sending_service, mail_campaign_service=campaign_service,
        activity_log=activity_log,
    )


async def _make_draft_campaign(campaign_service):
    return await campaign_service.create_campaign("Draft Campaign")


# =====================================================================
# 1. CRUD validation / lifecycle
# =====================================================================


async def test_create_trigger_persists_and_returns_expected_fields(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1, 2, 3, 4], "09:00", 20)
    assert trigger.mail_campaign_id == campaign.mail_campaign_id
    assert trigger.weekdays == [0, 1, 2, 3, 4]
    assert trigger.local_time == time(9, 0)
    assert trigger.leads_to_start == 20
    assert trigger.enabled is True

    listed = await trigger_service.list_triggers(campaign.mail_campaign_id)
    assert [t.trigger_id for t in listed] == [trigger.trigger_id]


async def test_create_trigger_rejects_empty_weekdays(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    with pytest.raises(ValueError):
        await trigger_service.create_trigger(campaign.mail_campaign_id, [], "09:00", 20)


async def test_create_trigger_rejects_invalid_weekday(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    with pytest.raises(ValueError):
        await trigger_service.create_trigger(campaign.mail_campaign_id, [7], "09:00", 20)


async def test_create_trigger_rejects_invalid_local_time(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    with pytest.raises(ValueError):
        await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "25:99", 20)


async def test_create_trigger_rejects_non_positive_leads_to_start(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    with pytest.raises(ValueError):
        await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 0)


async def test_create_trigger_missing_campaign_raises_not_found(trigger_service):
    with pytest.raises(MailCampaignNotFound):
        await trigger_service.create_trigger("does-not-exist", [0], "09:00", 20)


async def test_update_trigger_partial_fields(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 20)
    updated = await trigger_service.update_trigger(campaign.mail_campaign_id, trigger.trigger_id, leads_to_start=5)
    assert updated.leads_to_start == 5
    assert updated.weekdays == [0, 1]  # unchanged


async def test_update_trigger_missing_raises_not_found(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    with pytest.raises(MailLeadStartTriggerNotFoundError):
        await trigger_service.update_trigger(campaign.mail_campaign_id, "no-such-trigger", leads_to_start=5)


async def test_delete_trigger_removes_it(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    await trigger_service.delete_trigger(campaign.mail_campaign_id, trigger.trigger_id)
    assert await trigger_service.list_triggers(campaign.mail_campaign_id) == []


# =====================================================================
# 2-3. Mode transition
# =====================================================================


async def test_first_trigger_flips_immediate_to_triggered(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    assert campaign.lead_start_mode == "immediate"

    await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.lead_start_mode == "triggered"


async def test_second_trigger_creation_is_a_pure_mode_no_op(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    await trigger_service.create_trigger(campaign.mail_campaign_id, [1], "14:00", 10)
    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.lead_start_mode == "triggered"
    assert len(await trigger_service.list_triggers(campaign.mail_campaign_id)) == 2


async def test_deleting_the_last_trigger_does_not_revert_mode(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    await trigger_service.delete_trigger(campaign.mail_campaign_id, trigger.trigger_id)

    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.lead_start_mode == "triggered"
    assert await trigger_service.list_triggers(campaign.mail_campaign_id) == []


async def test_disabling_the_last_trigger_does_not_revert_mode(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    await trigger_service.update_trigger(campaign.mail_campaign_id, trigger.trigger_id, enabled=False)

    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.lead_start_mode == "triggered"


# =====================================================================
# 4. Trigger CRUD itself never starts leads
# =====================================================================


async def test_trigger_crud_never_creates_step1_or_flips_enrollment(trigger_service, campaign_service, crm, enrollment_step_store):
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm, n_contacts=2)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    trigger = await trigger_service.create_trigger(ready.mail_campaign_id, [0, 1, 2, 3, 4, 5, 6], "09:00", 20)
    await trigger_service.update_trigger(ready.mail_campaign_id, trigger.trigger_id, leads_to_start=5)

    enrollments = await campaign_service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status.value == "pending" for e in enrollments)
    assert await enrollment_step_store.list_for_campaign(ready.mail_campaign_id) == []


# =====================================================================
# 5-6. DRAFT/READY trigger creation starts nothing
# =====================================================================


async def test_draft_trigger_creation_starts_nothing(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0], "09:00", 20)
    assert trigger.trigger_id is not None
    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.status == MailCampaignStatus.DRAFT
    assert fresh.lead_start_mode == "triggered"


async def test_ready_trigger_creation_starts_nothing_and_pending_pool_is_untouched(trigger_service, campaign_service, crm, enrollment_step_store):
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm, n_contacts=3)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    await trigger_service.create_trigger(ready.mail_campaign_id, [0, 1, 2, 3, 4, 5, 6], "09:00", 20)

    enrollments = await campaign_service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert len(enrollments) == 3
    assert all(e.status.value == "pending" for e in enrollments)
    assert await enrollment_step_store.list_for_campaign(ready.mail_campaign_id) == []


# =====================================================================
# Lifecycle gating: ARCHIVED / COMPLETED rejected
# =====================================================================


async def test_archived_campaign_rejects_trigger_creation(trigger_service, campaign_service):
    campaign = await _make_draft_campaign(campaign_service)
    archived = await campaign_service.archive_campaign(campaign.mail_campaign_id)
    with pytest.raises(MailCampaignNotEligibleForTriggersError):
        await trigger_service.create_trigger(archived.mail_campaign_id, [0], "09:00", 20)


async def test_legacy_completed_campaign_rejects_trigger_creation(trigger_service, campaign_service, campaign_store):
    campaign = await _make_draft_campaign(campaign_service)
    completed = campaign.model_copy(update={"status": MailCampaignStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)})
    await campaign_store.save(completed)
    with pytest.raises(MailCampaignNotEligibleForTriggersError):
        await trigger_service.create_trigger(completed.mail_campaign_id, [0], "09:00", 20)


async def test_active_and_paused_campaigns_allow_trigger_creation(trigger_service, campaign_service, crm):
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm, n_contacts=1)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await campaign_service.activate_campaign(ready.mail_campaign_id)
    trigger = await trigger_service.create_trigger(active.mail_campaign_id, [0], "09:00", 20)
    assert trigger.trigger_id is not None

    paused = await campaign_service.pause_campaign(active.mail_campaign_id)
    trigger2 = await trigger_service.create_trigger(paused.mail_campaign_id, [1], "10:00", 5)
    assert trigger2.trigger_id is not None
