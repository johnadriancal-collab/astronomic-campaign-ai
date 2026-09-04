"""
Tests for MailCampaignService -- Astronomic Mail Phase 1's campaign/
sequence/audience/review orchestration. Uses in-memory stores throughout,
same convention as test_itf_ingestion_service.py's fixtures. A real
CrmService() (in-memory) provides the audience (CrmContactList/CrmContact)
this service reads from.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.mail import (
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentBatch,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailEnrollmentStatus,
    MailScheduleValidationError,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MemoryMailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import (
    DuplicateMailSequenceStepNumberError,
    MemoryMailSequenceStepStore,
)
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import (
    DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS,
    InvalidMailSequenceStepDelayError,
    InvalidMailTemplateVariableError,
    MailboxChannelNotFoundError,
    MailboxChannelNotUsableError,
    MailCampaignChannelsFrozenError,
    MailCampaignInvalidTransitionError,
    MailCampaignLegacyScheduleLockedError,
    MailCampaignNotEditableError,
    MailCampaignNotFound,
    MailCampaignNotReadyError,
    MailCampaignService,
    MailSendingEngineDisabledError,
)
from app.services.mail_sending_service import MailSendingService
from app.services import mail_campaign_service as mail_campaign_service_module

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mail_sending_engine_enabled(monkeypatch):
    """Every activate_campaign()/resume_campaign() test in this file
    exercises the state machine itself, not the deployment-wide safety gate
    (see MailSendingEngineDisabledError / settings.mail_sending_engine_enabled
    in app/config.py) -- that gate has its own dedicated test below
    (test_activate_refused_when_sending_engine_disabled) which explicitly
    overrides this back to False."""
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
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def service(
    crm, activity_log, mailbox_store, channel_store, window_store, enrollment_step_store, suppression_store,
    batch_store, batch_member_store, crm_import_service,
):
    campaign_store = MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    sending_service = MailSendingService(
        campaign_store=campaign_store,
        enrollment_store=enrollment_store,
        step_store=enrollment_step_store,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store,
        activity_log=activity_log,
    )
    return MailCampaignService(
        campaign_store=campaign_store,
        step_store=MemoryMailSequenceStepStore(),
        enrollment_store=enrollment_store,
        crm_service=crm,
        activity_log=activity_log,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        window_store=window_store,
        enrollment_step_store=enrollment_step_store,
        sending_service=sending_service,
        batch_store=batch_store,
        batch_member_store=batch_member_store,
        suppression_store=suppression_store,
        crm_import_reader=crm_import_service,
    )


def _make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED, email="victoria@example.com"):
    now = datetime.now(timezone.utc)
    return Mailbox(
        mailbox_id=mailbox_id,
        provider=MailboxProvider.GOOGLE,
        email=email,
        display_name="Victoria Bennett",
        status=status,
        google_user_id="google-user-1",
        granted_scopes=["openid", "email", "profile"],
        connected_at=now,
        updated_at=now,
        disconnected_at=None if status != MailboxStatus.DISCONNECTED else now,
    )


async def _make_valid_schedule_campaign(service, crm, name="Q1 Outreach", n_contacts=3):
    """Helper: a DRAFT campaign with a real audience list, one step, a valid
    complete schedule, and one connected mailbox selected as its Channel --
    everything mark_ready() requires."""
    contact_list = await crm.create_contact_list("Test Audience")
    contact_ids = []
    for i in range(n_contacts):
        c = await crm.create_contact({"email": f"person{i}@example.com", "first_name": f"Person{i}"})
        contact_ids.append(c.crm_contact_id)
    await crm.bulk_add_to_list(contact_list.list_id, contact_ids)

    campaign = await service.create_campaign(name)
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": contact_list.list_id,
            "sending_days": [0, 1, 2, 3, 4],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "America/Chicago",
        },
    )
    await service.add_step(campaign.mail_campaign_id, "Hello {{first_name}}", "Body text")

    mailbox_id = f"mbx-{campaign.mail_campaign_id}"
    await service.mailbox_store.create(_make_mailbox(mailbox_id=mailbox_id, email=f"{mailbox_id}@example.com"))
    await service.set_channel_mailboxes(campaign.mail_campaign_id, [mailbox_id])

    return campaign, contact_list


# --- Campaign CRUD -----------------------------------------------------


async def test_create_draft_campaign(service):
    campaign = await service.create_campaign("My Campaign")
    assert campaign.status == MailCampaignStatus.DRAFT
    assert campaign.name == "My Campaign"
    assert campaign.source_list_id is None


# --- Trigger foundation (Stage 5A, 2026-09-04): lead_start_mode /
# execution_active_since -- durable schema only, nothing branches on
# lead_start_mode yet, and no Trigger row is ever created automatically. --


async def test_new_campaign_defaults_to_immediate_lead_start_mode_and_null_active_since(service):
    campaign = await service.create_campaign("My Campaign")
    assert campaign.lead_start_mode == "immediate"
    assert campaign.execution_active_since is None


async def test_ready_campaign_still_has_null_execution_active_since(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.execution_active_since is None
    assert ready.lead_start_mode == "immediate"


async def test_get_campaign_not_found(service):
    with pytest.raises(MailCampaignNotFound):
        await service.get_campaign("does-not-exist")


async def test_update_draft_campaign_name_and_schedule(service, crm):
    contact_list = await crm.create_contact_list("Some List")
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(
        campaign.mail_campaign_id,
        {
            "name": "Renamed",
            "source_list_id": contact_list.list_id,
            "sending_days": [0, 1, 2],
            "start_time": "08:00",
            "end_time": "12:00",
            "timezone": "America/New_York",
        },
    )
    assert updated.name == "Renamed"
    assert updated.source_list_id == contact_list.list_id
    assert updated.sending_days == [0, 1, 2]
    assert updated.timezone == "America/New_York"


async def test_update_rejects_unknown_source_list_id(service):
    campaign = await service.create_campaign("Draft")
    from app.services.crm_service import CrmContactListNotFound

    with pytest.raises(CrmContactListNotFound):
        await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": "does-not-exist"})


async def test_update_rejects_start_time_not_before_end_time(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailScheduleValidationError):
        await service.update_campaign(campaign.mail_campaign_id, {"start_time": "17:00", "end_time": "09:00"})


async def test_update_rejects_invalid_timezone(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailScheduleValidationError):
        await service.update_campaign(campaign.mail_campaign_id, {"timezone": "Mars/Cydonia"})


async def test_update_rejects_invalid_sending_day(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailScheduleValidationError):
        await service.update_campaign(campaign.mail_campaign_id, {"sending_days": [7]})


async def test_update_allows_partial_incomplete_schedule_while_draft(service):
    """A DRAFT campaign is legitimately mid-configuration -- setting just
    sending_days with no times/timezone yet must not be rejected."""
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"sending_days": [0, 1]})
    assert updated.sending_days == [0, 1]
    assert updated.start_time is None


# --- Campaign Manager Integration Phase: campaign-level config fields ----


async def test_new_campaign_defaults_are_non_send_capable(service):
    """A brand-new campaign carries the new config fields at their neutral
    defaults -- sharing visible-to-everyone, no start-immediately intent,
    no daily limit -- and, most importantly, is still a plain DRAFT with no
    path to an active-sending state."""
    campaign = await service.create_campaign("Draft")
    assert campaign.sharing.value == "everyone"
    assert campaign.all_hours is False
    assert campaign.start_immediately is False
    assert campaign.daily_lead_start_limit is None
    assert campaign.status == MailCampaignStatus.DRAFT


async def test_update_sharing_persists_as_enum(service):
    from app.models.mail import MailCampaignSharing

    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"sharing": "only_me"})
    assert updated.sharing == MailCampaignSharing.ONLY_ME

    reloaded = await service.get_campaign(campaign.mail_campaign_id)
    assert reloaded.sharing == MailCampaignSharing.ONLY_ME


async def test_update_rejects_invalid_sharing_value(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(ValueError):
        await service.update_campaign(campaign.mail_campaign_id, {"sharing": "everyone_in_the_world"})


async def test_update_start_immediately_never_changes_status(service):
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"start_immediately": True})
    assert updated.start_immediately is True
    assert updated.status == MailCampaignStatus.DRAFT


async def test_update_daily_lead_start_limit_persists(service):
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"daily_lead_start_limit": 50})
    assert updated.daily_lead_start_limit == 50


async def test_update_daily_lead_start_limit_none_means_unlimited(service):
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"daily_lead_start_limit": 50})
    updated = await service.update_campaign(updated.mail_campaign_id, {"daily_lead_start_limit": None})
    assert updated.daily_lead_start_limit is None


@pytest.mark.parametrize("bad_limit", [0, -5, 1.5, True])
async def test_update_rejects_non_positive_daily_lead_start_limit(service, bad_limit):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(ValueError):
        await service.update_campaign(campaign.mail_campaign_id, {"daily_lead_start_limit": bad_limit})


async def test_all_hours_forces_full_day_bounds_overriding_explicit_times(service):
    """Setting all_hours True forces literal 00:00/23:59 bounds even if the
    same patch also tried to set different explicit times -- all_hours wins."""
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(
        campaign.mail_campaign_id,
        {"all_hours": True, "start_time": "09:00", "end_time": "17:00"},
    )
    assert updated.all_hours is True
    assert updated.start_time.isoformat() == "00:00:00"
    assert updated.end_time.isoformat() == "23:59:00"


async def test_all_hours_true_with_no_explicit_times_still_forces_full_day(service):
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(campaign.mail_campaign_id, {"all_hours": True})
    assert updated.start_time.isoformat() == "00:00:00"
    assert updated.end_time.isoformat() == "23:59:00"


async def test_all_hours_satisfies_mark_ready_schedule_validation(service, crm):
    """An all_hours campaign is a genuinely complete, valid schedule for
    mark_ready() -- it must not need real start/end times from the user."""
    contact_list = await crm.create_contact_list("Audience")
    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {"source_list_id": contact_list.list_id, "sending_days": [0, 1, 2], "all_hours": True, "timezone": "UTC"},
    )
    await service.add_step(campaign.mail_campaign_id, "Hi", "Body")
    await service.mailbox_store.create(_make_mailbox(mailbox_id="mbx-all-hours-test"))
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-all-hours-test"])
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.status == MailCampaignStatus.READY


async def test_turning_all_hours_off_does_not_retroactively_clear_times(service):
    """Turning all_hours back off leaves whatever start/end times are
    already stored untouched -- the caller must explicitly set new ones if
    they want a real window instead of the full-day bounds."""
    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(campaign.mail_campaign_id, {"all_hours": True})
    updated = await service.update_campaign(campaign.mail_campaign_id, {"all_hours": False})
    assert updated.all_hours is False
    assert updated.start_time.isoformat() == "00:00:00"
    assert updated.end_time.isoformat() == "23:59:00"


async def test_create_campaign_still_works_with_only_a_name(service):
    """The original, minimal create_campaign(name) contract is completely
    unchanged -- every existing call site keeps working identically."""
    campaign = await service.create_campaign("Just A Name")
    assert campaign.name == "Just A Name"
    assert campaign.sending_days == []
    assert campaign.timezone is None
    assert campaign.sharing.value == "everyone"


async def test_update_ignores_disallowed_patch_keys_including_status(service):
    """Matches CrmService.update_contact_list()'s exact convention: unknown/
    disallowed keys are silently dropped, never applied, never erroring."""
    campaign = await service.create_campaign("Draft")
    updated = await service.update_campaign(
        campaign.mail_campaign_id, {"status": "active", "mail_campaign_id": "hijacked"}
    )
    assert updated.status == MailCampaignStatus.DRAFT  # untouched
    assert updated.mail_campaign_id == campaign.mail_campaign_id  # untouched


async def test_active_paused_completed_are_real_enum_values_now():
    """Phase A addition -- ACTIVE/PAUSED/COMPLETED are real, valid
    MailCampaignStatus members (see that enum's docstring), reachable only
    via activate_campaign()/pause_campaign()/resume_campaign() and the
    system-driven completion path -- never via create_campaign() or
    update_campaign()."""
    assert MailCampaignStatus("active") == MailCampaignStatus.ACTIVE
    assert MailCampaignStatus("paused") == MailCampaignStatus.PAUSED
    assert MailCampaignStatus("completed") == MailCampaignStatus.COMPLETED


async def test_archive_from_draft(service, activity_log):
    campaign = await service.create_campaign("Draft")
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    assert archived.status == MailCampaignStatus.ARCHIVED
    assert archived.archived_at is not None

    events = [e.event_type for e in await activity_log.store.list()]
    assert "mail_campaign.archived" in events


async def test_archive_from_ready(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    archived = await service.archive_campaign(ready.mail_campaign_id)
    assert archived.status == MailCampaignStatus.ARCHIVED


async def test_archive_clears_execution_active_since_from_active(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    assert activated.execution_active_since is not None

    archived = await service.archive_campaign(ready.mail_campaign_id)
    assert archived.execution_active_since is None


async def test_archive_clears_execution_active_since_from_paused(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    await service.pause_campaign(ready.mail_campaign_id)  # already None here, confirming archive keeps it None too

    archived = await service.archive_campaign(ready.mail_campaign_id)
    assert archived.execution_active_since is None


async def test_archive_twice_raises_invalid_transition(service):
    campaign = await service.create_campaign("Draft")
    await service.archive_campaign(campaign.mail_campaign_id)
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.archive_campaign(campaign.mail_campaign_id)


async def test_update_archived_campaign_is_rejected(service):
    campaign = await service.create_campaign("Draft")
    await service.archive_campaign(campaign.mail_campaign_id)
    with pytest.raises(MailCampaignNotEditableError):
        await service.update_campaign(campaign.mail_campaign_id, {"name": "New name"})


async def test_activate_and_pause_exist_but_no_send_capable_method_ever_will(service):
    """Phase A addition -- activate_campaign()/pause_campaign()/
    resume_campaign() are now real, legitimate lifecycle methods (see their
    own docstrings). What remains permanently absent is anything that could
    ever DISPATCH a message: no launch_campaign()/send_campaign(), and
    (confirmed elsewhere, see test_mail_sending_service.py) no concrete
    MailSenderPort implementation exists anywhere under app/."""
    assert hasattr(service, "activate_campaign")
    assert hasattr(service, "pause_campaign")
    assert hasattr(service, "resume_campaign")
    assert not hasattr(service, "launch_campaign")
    assert not hasattr(service, "send_campaign")


# --- Phase A: activate / pause / resume -----------------------------------


async def test_activate_requires_ready(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.activate_campaign(campaign.mail_campaign_id)


async def test_activate_refused_when_sending_engine_disabled(service, crm, monkeypatch):
    """The deployment-wide gate (settings.mail_sending_engine_enabled,
    default False) refuses activation regardless of how ready/valid the
    campaign itself is -- checked FIRST, before even the READY status
    check, so this fires even for a campaign that isn't READY at all."""
    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    with pytest.raises(MailSendingEngineDisabledError):
        await service.activate_campaign(ready.mail_campaign_id)
    unchanged = await service.get_campaign(ready.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.READY


async def test_resume_refused_when_sending_engine_disabled(service, crm, monkeypatch):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    await service.pause_campaign(ready.mail_campaign_id)

    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)
    with pytest.raises(MailSendingEngineDisabledError):
        await service.resume_campaign(ready.mail_campaign_id)
    still_paused = await service.get_campaign(ready.mail_campaign_id)
    assert still_paused.status == MailCampaignStatus.PAUSED


async def test_pause_is_never_gated_by_the_sending_engine_flag(service, crm, monkeypatch):
    """Pausing is always allowed regardless of the flag -- it only ever
    makes execution safer, never activates anything."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)

    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)
    paused = await service.pause_campaign(ready.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED


async def test_activate_a_valid_ready_campaign_succeeds(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE


async def test_activate_creates_exactly_one_step1_execution_per_eligible_enrollment(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=3)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)

    # Stage 5A regression: activation still eagerly activates EVERY
    # eligible PENDING enrollment, unconditionally, exactly as before --
    # lead_start_mode stays "immediate" (nothing sets it to "triggered" on
    # its own) and nothing here gates on it.
    rows = await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
    assert len(rows) == 3
    assert all(r.step_number == 1 for r in rows)

    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)

    activated = await service.get_campaign(campaign.mail_campaign_id)
    assert activated.lead_start_mode == "immediate"


async def test_activate_sets_execution_active_since(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    before = datetime.now(timezone.utc)
    activated = await service.activate_campaign(ready.mail_campaign_id)
    after = datetime.now(timezone.utc)
    assert activated.execution_active_since is not None
    assert before <= activated.execution_active_since <= after


async def test_repeated_activation_cannot_duplicate_step1_rows(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    # A second call on an already-ACTIVE campaign is rejected as an invalid
    # transition (READY-only) -- but even a hypothetical re-entrant call
    # into the same activation logic (exercised directly here) must not
    # duplicate rows, since create_step1_execution() is idempotent and no
    # PENDING enrollment remains to loop over.
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.activate_campaign(ready.mail_campaign_id)
    rows = await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
    assert len(rows) == 2


async def test_activate_rejected_with_zero_partial_mutation_when_channels_lost(service, crm):
    """A READY campaign that loses all its connected Channels since Mark
    Ready must have activation rejected -- and nothing partially written:
    campaign stays READY, no MailEnrollmentStep row is created, no
    enrollment status changes."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    # Disconnect the only selected mailbox (Channels selection unchanged,
    # but no longer usable) -- exactly the drift activate_campaign() must
    # re-detect since READY doesn't re-validate this on its own.
    mailbox_ids = await service.list_channel_mailboxes(ready.mail_campaign_id)
    for mailbox in mailbox_ids:
        await service.mailbox_store.save(mailbox.model_copy(update={"status": MailboxStatus.DISCONNECTED}))

    with pytest.raises(MailCampaignNotReadyError):
        await service.activate_campaign(ready.mail_campaign_id)

    unchanged = await service.get_campaign(ready.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.READY
    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert rows == []
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


async def test_pause_requires_active(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.pause_campaign(campaign.mail_campaign_id)


async def test_pause_then_resume_round_trip(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)

    paused = await service.pause_campaign(ready.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED

    resumed = await service.resume_campaign(ready.mail_campaign_id)
    assert resumed.status == MailCampaignStatus.ACTIVE


async def test_pause_does_not_touch_enrollment_or_step_rows(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    before = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)

    await service.pause_campaign(ready.mail_campaign_id)

    after = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert [r.status for r in before] == [r.status for r in after]


async def test_pause_clears_execution_active_since(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    assert activated.execution_active_since is not None

    paused = await service.pause_campaign(ready.mail_campaign_id)
    assert paused.execution_active_since is None


async def test_resume_sets_a_fresh_execution_active_since(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    first_active_since = activated.execution_active_since
    await service.pause_campaign(ready.mail_campaign_id)

    before = datetime.now(timezone.utc)
    resumed = await service.resume_campaign(ready.mail_campaign_id)
    after = datetime.now(timezone.utc)

    assert resumed.execution_active_since is not None
    assert before <= resumed.execution_active_since <= after
    assert resumed.execution_active_since != first_active_since


async def test_unrelated_active_campaign_edit_does_not_touch_execution_active_since(service, crm):
    """add_prospects() on an ALREADY-ACTIVE (not legacy-COMPLETED)
    campaign is a real, legitimate operation that never changes campaign
    lifecycle status at all -- only its own dedicated legacy-COMPLETED
    reopen branch may ever set execution_active_since; ordinary Add
    Prospects onto a campaign that's already ACTIVE must never touch it."""
    from app.models.mail import MailEnrollmentBatchSource

    campaign, contact_list = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    original_active_since = activated.execution_active_since
    assert original_active_since is not None

    new_contact = await crm.create_contact({"email": "unrelated-edit@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])
    batch = await service.add_prospects(
        activated.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="unrelated-edit-key", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1  # confirms this really did something, not a no-op

    unchanged = await service.get_campaign(activated.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.ACTIVE  # never touched lifecycle status
    assert unchanged.execution_active_since == original_active_since


async def test_resume_requires_paused(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.resume_campaign(campaign.mail_campaign_id)


async def test_resume_rejected_when_no_connected_mailbox_remains(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    await service.pause_campaign(ready.mail_campaign_id)

    mailbox_ids = await service.list_channel_mailboxes(ready.mail_campaign_id)
    for mailbox in mailbox_ids:
        await service.mailbox_store.save(mailbox.model_copy(update={"status": MailboxStatus.DISCONNECTED}))

    with pytest.raises(MailCampaignNotReadyError):
        await service.resume_campaign(ready.mail_campaign_id)
    still_paused = await service.get_campaign(ready.mail_campaign_id)
    assert still_paused.status == MailCampaignStatus.PAUSED


# --- Phase A: Channels locked once ACTIVE/PAUSED/COMPLETED ------------------


async def test_set_channel_mailboxes_locked_once_active(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    with pytest.raises(MailCampaignChannelsFrozenError):
        await service.set_channel_mailboxes(ready.mail_campaign_id, [])


async def test_set_channel_mailboxes_locked_once_paused(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)
    await service.pause_campaign(ready.mail_campaign_id)
    with pytest.raises(MailCampaignChannelsFrozenError):
        await service.set_channel_mailboxes(ready.mail_campaign_id, [])


async def test_set_channel_mailboxes_still_editable_at_ready(service, crm):
    """Unchanged shipped behavior -- READY alone (never activated) keeps
    Channels editable, only ACTIVE/PAUSED/COMPLETED/ARCHIVED freeze it."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    result = await service.set_channel_mailboxes(ready.mail_campaign_id, [])
    assert result == []


# --- Phase A: unlock cascade -------------------------------------------------


async def test_unlock_cascade_deletes_enrollment_step_rows_too(service, crm, enrollment_step_store):
    """Defense-in-depth: unlock_campaign() (READY-only, and there is no path
    from ACTIVE/PAUSED back to READY in this phase) must also cascade-delete
    any MailEnrollmentStep rows, not just MailEnrollment rows."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    now = enrollments[0].enrolled_at
    from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus

    await enrollment_step_store.create(
        MailEnrollmentStep(
            enrollment_step_id="manual-es1", mail_campaign_id=ready.mail_campaign_id,
            enrollment_id=enrollments[0].enrollment_id, crm_contact_id=enrollments[0].crm_contact_id,
            step_id="s1", step_number=1, subject="x", body="x", delay_days=0, reply_in_thread=True,
            status=MailEnrollmentStepStatus.QUEUED, created_at=now, updated_at=now,
        )
    )
    await service.unlock_campaign(ready.mail_campaign_id)
    remaining = await enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert remaining == []


# --- Sequence steps -----------------------------------------------------


async def test_add_steps_are_ordered_and_numbered_deterministically(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "Subject 1", "Body 1")
    s2 = await service.add_step(campaign.mail_campaign_id, "Subject 2", "Body 2", delay_days=2)
    s3 = await service.add_step(campaign.mail_campaign_id, "Subject 3", "Body 3", delay_days=2)

    assert [s1.step_number, s2.step_number, s3.step_number] == [1, 2, 3]
    steps = await service.list_steps(campaign.mail_campaign_id)
    assert [s.step_number for s in steps] == [1, 2, 3]
    assert [s.step_id for s in steps] == [s1.step_id, s2.step_id, s3.step_id]


async def test_duplicate_step_number_is_rejected_at_the_store_layer(service):
    """The service always auto-assigns step_number, so this exercises the
    actual DB-level (here: in-memory store's mirrored) backstop directly --
    the same UNIQUE(mail_campaign_id, step_number) guarantee the SQLite
    schema enforces for real."""
    campaign = await service.create_campaign("Draft")
    from app.models.mail import MailSequenceStep
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    await service.step_store.create(
        MailSequenceStep(
            step_id="s1", mail_campaign_id=campaign.mail_campaign_id, step_number=1,
            subject="A", body="B", created_at=now, updated_at=now,
        )
    )
    with pytest.raises(DuplicateMailSequenceStepNumberError):
        await service.step_store.create(
            MailSequenceStep(
                step_id="s2", mail_campaign_id=campaign.mail_campaign_id, step_number=1,
                subject="C", body="D", created_at=now, updated_at=now,
            )
        )


async def test_add_step_rejects_unknown_variable(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(InvalidMailTemplateVariableError):
        await service.add_step(campaign.mail_campaign_id, "Hi {{deal_size}}", "Body")


async def test_add_step_accepts_all_whitelisted_variables(service):
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(
        campaign.mail_campaign_id,
        "Hi {{first_name}} {{last_name}}",
        "You work at {{company}}, right?",
    )
    assert step.subject == "Hi {{first_name}} {{last_name}}"


async def test_update_step_rejects_unknown_variable(service):
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(campaign.mail_campaign_id, "Subject", "Body")
    with pytest.raises(InvalidMailTemplateVariableError):
        await service.update_step(campaign.mail_campaign_id, step.step_id, {"body": "{{unknown_var}}"})


async def test_delete_step_renumbers_remaining_steps_contiguously(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2")
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3")

    await service.delete_step(campaign.mail_campaign_id, s2.step_id)
    remaining = await service.list_steps(campaign.mail_campaign_id)
    assert [s.step_id for s in remaining] == [s1.step_id, s3.step_id]
    assert [s.step_number for s in remaining] == [1, 2]  # no gap


async def test_reorder_steps(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2")
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3")

    reordered = await service.reorder_steps(campaign.mail_campaign_id, [s3.step_id, s1.step_id, s2.step_id])
    assert [s.step_id for s in reordered] == [s3.step_id, s1.step_id, s2.step_id]
    assert [s.step_number for s in reordered] == [1, 2, 3]

    persisted = await service.list_steps(campaign.mail_campaign_id)
    assert [s.step_id for s in persisted] == [s3.step_id, s1.step_id, s2.step_id]


async def test_reorder_rejects_mismatched_step_id_set(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    await service.add_step(campaign.mail_campaign_id, "S2", "B2")
    with pytest.raises(ValueError):
        await service.reorder_steps(campaign.mail_campaign_id, [s1.step_id])  # missing s2


async def test_step_mutation_rejected_once_ready(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    with pytest.raises(MailCampaignNotEditableError):
        await service.add_step(ready.mail_campaign_id, "New step", "Body")


# --- Step 1 delay_days invariant -----------------------------------------
#
# Whatever step currently has step_number == 1 must have delay_days == 0 --
# a real, stored fact (not a display trick), enforced by the service on
# every add/edit/reorder/delete. See add_step()/update_step()/_renumber()'s
# docstrings for the exact rule and reasoning.


async def test_first_ever_step_forces_delay_to_zero_even_if_supplied_nonzero(service):
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(campaign.mail_campaign_id, "Subject", "Body", delay_days=2)
    assert step.step_number == 1
    assert step.delay_days == 0


async def test_first_ever_step_forces_delay_to_zero_even_if_supplied_negative(service):
    """Step 1 is force-overridden to 0 unconditionally, never rejected --
    only a Step 2+'s negative delay_days is a validation error (see
    test_add_step_rejects_negative_delay below). A negative value here is
    silently normalized the same way a positive one is."""
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(campaign.mail_campaign_id, "Subject", "Body", delay_days=-5)
    assert step.step_number == 1
    assert step.delay_days == 0


async def test_second_step_defaults_and_honors_explicit_delay(service):
    campaign = await service.create_campaign("Draft")
    await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2_default = await service.add_step(campaign.mail_campaign_id, "S2", "B2")
    assert s2_default.delay_days == 0  # add_step()'s own signature default, unrelated to the frontend's UI default

    campaign2 = await service.create_campaign("Draft 2")
    await service.add_step(campaign2.mail_campaign_id, "S1", "B1")
    s2_explicit = await service.add_step(campaign2.mail_campaign_id, "S2", "B2", delay_days=5)
    assert s2_explicit.delay_days == 5


async def test_add_step_rejects_negative_delay_for_a_follow_up(service):
    campaign = await service.create_campaign("Draft")
    await service.add_step(campaign.mail_campaign_id, "S1", "B1")  # step 1, unaffected
    with pytest.raises(InvalidMailSequenceStepDelayError):
        await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=-1)


async def test_editing_step_1_delay_is_always_forced_to_zero(service):
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    updated = await service.update_step(campaign.mail_campaign_id, step.step_id, {"delay_days": 5})
    assert updated.step_number == 1
    assert updated.delay_days == 0


async def test_editing_step_1_subject_alone_self_heals_a_legacy_nonzero_delay(service):
    """Simulates a pre-invariant record (e.g. the known production Step 1
    with delay_days=2) by writing directly through the step store, bypassing
    add_step()'s own enforcement -- then proves a completely unrelated edit
    (just the subject) still normalizes the stale delay_days as a side
    effect, with no separate migration and no delay_days key in the patch
    at all."""
    from app.models.mail import MailSequenceStep

    campaign = await service.create_campaign("Draft")
    now = datetime.now(timezone.utc)
    legacy_step = MailSequenceStep(
        step_id="legacy-1",
        mail_campaign_id=campaign.mail_campaign_id,
        step_number=1,
        subject="Old subject",
        body="Old body",
        delay_days=2,
        created_at=now,
        updated_at=now,
    )
    await service.step_store.create(legacy_step)

    updated = await service.update_step(campaign.mail_campaign_id, "legacy-1", {"subject": "New subject"})
    assert updated.subject == "New subject"
    assert updated.delay_days == 0


async def test_editing_step_2_delay_works(service):
    campaign = await service.create_campaign("Draft")
    await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=2)
    updated = await service.update_step(campaign.mail_campaign_id, s2.step_id, {"delay_days": 7})
    assert updated.delay_days == 7


async def test_editing_step_2_to_negative_delay_is_rejected(service):
    campaign = await service.create_campaign("Draft")
    await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=2)
    with pytest.raises(InvalidMailSequenceStepDelayError):
        await service.update_step(campaign.mail_campaign_id, s2.step_id, {"delay_days": -3})
    # rejected -- nothing was written
    persisted = await service.step_store.get(s2.step_id)
    assert persisted.delay_days == 2


async def test_reorder_moving_a_later_step_to_first_zeroes_its_delay_and_promotes_old_first(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")  # delay 0 (forced, first)
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=2)
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3", delay_days=9)

    reordered = await service.reorder_steps(campaign.mail_campaign_id, [s2.step_id, s1.step_id, s3.step_id])
    by_id = {s.step_id: s for s in reordered}

    assert by_id[s2.step_id].step_number == 1
    assert by_id[s2.step_id].delay_days == 0  # new #1 -- forced to 0

    assert by_id[s1.step_id].step_number == 2
    assert by_id[s1.step_id].delay_days == DEFAULT_MAIL_SEQUENCE_FOLLOWUP_DELAY_DAYS  # demoted old #1 -- default follow-up

    assert by_id[s3.step_id].step_number == 3
    assert by_id[s3.step_id].delay_days == 9  # untouched -- never was #1, position unaffected


async def test_reorder_a_step_that_was_never_first_keeps_its_configured_delay(service):
    """A pure swap of two non-first steps must never touch either delay --
    only a step's OWN transition into/out of position 1 changes anything."""
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=3)
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3", delay_days=8)

    reordered = await service.reorder_steps(campaign.mail_campaign_id, [s1.step_id, s3.step_id, s2.step_id])
    by_id = {s.step_id: s for s in reordered}

    assert by_id[s1.step_id].delay_days == 0
    assert by_id[s3.step_id].delay_days == 8
    assert by_id[s2.step_id].delay_days == 3


async def test_delete_step_1_promotes_and_zeroes_new_first_without_disturbing_others(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=4)
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3", delay_days=6)

    remaining = await service.delete_step(campaign.mail_campaign_id, s1.step_id)
    by_id = {s.step_id: s for s in remaining}

    assert by_id[s2.step_id].step_number == 1
    assert by_id[s2.step_id].delay_days == 0  # promoted to #1 -- forced to 0

    assert by_id[s3.step_id].step_number == 2
    assert by_id[s3.step_id].delay_days == 6  # never was #1 -- untouched


async def test_deleting_a_non_first_step_does_not_disturb_unrelated_delays(service):
    campaign = await service.create_campaign("Draft")
    s1 = await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    s2 = await service.add_step(campaign.mail_campaign_id, "S2", "B2", delay_days=4)
    s3 = await service.add_step(campaign.mail_campaign_id, "S3", "B3", delay_days=6)

    remaining = await service.delete_step(campaign.mail_campaign_id, s2.step_id)
    by_id = {s.step_id: s for s in remaining}

    assert by_id[s1.step_id].step_number == 1
    assert by_id[s1.step_id].delay_days == 0
    assert by_id[s3.step_id].step_number == 2
    assert by_id[s3.step_id].delay_days == 6  # unrelated delay survives a deletion elsewhere


# --- mark_ready / audience snapshot --------------------------------------


async def test_mark_ready_fails_with_no_audience_no_steps_no_schedule(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignNotReadyError) as exc_info:
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    # All three problems reported together, not just the first.
    reasons_text = " ".join(exc_info.value.reasons)
    assert "audience" in reasons_text.lower()
    assert "sequence step" in reasons_text.lower()
    assert "sending day" in reasons_text.lower() or "timezone" in reasons_text.lower() or "time" in reasons_text.lower()


async def test_mark_ready_succeeds_and_creates_enrollments(service, crm):
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=3)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    assert ready.status == MailCampaignStatus.READY
    assert ready.ready_at is not None

    enrollments = await service.list_enrollments(ready.mail_campaign_id)
    assert len(enrollments) == 3
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


async def test_mark_ready_normalizes_a_legacy_nonzero_step_1_delay(service, crm):
    """Simulates a pre-invariant Step 1 (e.g. the known production campaign
    with delay_days=2 on its first step) by overwriting it directly through
    the step store after _make_valid_schedule_campaign() sets everything
    else up -- add_step()/update_step() can no longer produce this state
    themselves, so this is the only way to reproduce genuinely legacy data
    in a test. Proves mark_ready() lazily corrects it as part of the
    explicit Ready transition, with no separate migration."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    steps = await service.list_steps(campaign.mail_campaign_id)
    legacy_step = steps[0]
    await service.step_store.save(legacy_step.model_copy(update={"delay_days": 2}))

    await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    normalized = await service.step_store.get(legacy_step.step_id)
    assert normalized.delay_days == 0


async def test_mark_ready_does_not_touch_steps_when_readiness_checks_fail(service):
    """A campaign that ISN'T otherwise ready must never have its Step 1
    silently rewritten by a failed Mark Ready attempt -- normalization only
    runs after every other readiness reason has already been cleared (see
    mark_ready()'s docstring)."""
    campaign = await service.create_campaign("Draft")
    step = await service.add_step(campaign.mail_campaign_id, "Subject", "Body")
    await service.step_store.save(step.model_copy(update={"delay_days": 2}))  # simulate legacy bad data

    with pytest.raises(MailCampaignNotReadyError):
        # missing audience/mailbox/schedule -- mark_ready() must fail before
        # ever reaching the normalization write
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    untouched = await service.step_store.get(step.step_id)
    assert untouched.delay_days == 2  # still the legacy value -- nothing was written


async def test_mark_ready_marks_suppressed_contacts_as_suppressed_not_pending(service, crm):
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=3)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails={"person1@example.com"})

    enrollments = await service.list_enrollments(ready.mail_campaign_id)
    by_email = {e.email_at_enrollment: e.status for e in enrollments}
    assert by_email["person1@example.com"] == MailEnrollmentStatus.SUPPRESSED
    assert by_email["person0@example.com"] == MailEnrollmentStatus.PENDING


async def test_mark_ready_skips_contacts_with_no_email(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    with_email = await crm.create_contact({"email": "has-email@example.com"})
    no_email = await crm.create_contact({"first_name": "NoEmail"})
    await crm.bulk_add_to_list(contact_list.list_id, [with_email.crm_contact_id, no_email.crm_contact_id])

    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": contact_list.list_id,
            "sending_days": [0],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "UTC",
        },
    )
    await service.add_step(campaign.mail_campaign_id, "Subject", "Body")
    await service.mailbox_store.create(_make_mailbox(mailbox_id="mbx-no-email-test"))
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-no-email-test"])
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    enrollments = await service.list_enrollments(ready.mail_campaign_id)
    assert len(enrollments) == 1
    assert enrollments[0].crm_contact_id == with_email.crm_contact_id


async def test_enrollment_uniqueness_enforced_at_store_layer(service, crm):
    """UNIQUE(mail_campaign_id, crm_contact_id) -- direct store-level proof,
    the same guarantee the SQLite schema enforces via a composite PRIMARY
    KEY (see test_sqlite_mail_stores.py for the real-DB version)."""
    from app.models.mail import MailEnrollment, MailEnrollmentStatus as Status
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="a@example.com", status=Status.PENDING, enrolled_at=now, created_at=now,
    )
    first = await service.enrollment_store.create(enrollment)
    duplicate = enrollment.model_copy(update={"enrollment_id": "e2"})
    second = await service.enrollment_store.create(duplicate)
    assert first is True
    assert second is False  # no-op, never a second row

    all_rows = await service.enrollment_store.list_for_campaign("c1")
    assert len(all_rows) == 1


async def test_mark_ready_twice_from_draft_does_not_duplicate_enrollments(service, crm):
    """Simulates unlock -> re-mark-ready: enrollment rows must never
    duplicate even across two real snapshot runs for the same campaign."""
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.unlock_campaign(campaign.mail_campaign_id)
    await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    enrollments = await service.list_enrollments(campaign.mail_campaign_id)
    assert len(enrollments) == 2  # not 4


async def test_mark_ready_from_non_draft_raises_invalid_transition(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.mark_ready(ready.mail_campaign_id, suppressed_emails=set())


async def test_unlock_deletes_enrollment_snapshot(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert len(await service.list_enrollments(ready.mail_campaign_id)) == 2

    unlocked = await service.unlock_campaign(ready.mail_campaign_id)
    assert unlocked.status == MailCampaignStatus.DRAFT
    assert unlocked.ready_at is None
    assert await service.list_enrollments(unlocked.mail_campaign_id) == []


async def test_unlock_from_draft_raises_invalid_transition(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.unlock_campaign(campaign.mail_campaign_id)


async def test_list_membership_change_after_ready_does_not_retroactively_change_enrollment(service, crm):
    """The whole point of snapshotting at mark_ready(): a list edit after
    that point must not silently alter what the campaign's Review or
    enrollment reports -- Review (a live view) WILL reflect the change,
    but enrollments (the frozen snapshot) will not, until an explicit
    unlock + re-mark-ready."""
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    original_enrollments = await service.list_enrollments(ready.mail_campaign_id)
    assert len(original_enrollments) == 2

    new_contact = await crm.create_contact({"email": "new-person@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])

    unchanged_enrollments = await service.list_enrollments(ready.mail_campaign_id)
    assert len(unchanged_enrollments) == 2  # snapshot, not live


# --- Review (pure, read-only) --------------------------------------------


async def test_review_before_any_configuration_reports_zeros(service):
    campaign = await service.create_campaign("Empty Draft")
    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.total_contacts == 0
    assert review.sequence_step_count == 0
    assert review.theoretical_total_sends == 0
    assert review.source_list_exists is False


async def test_review_counts_total_missing_email_suppressed_eligible(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    has_email_1 = await crm.create_contact({"email": "alice@example.com"})
    has_email_2 = await crm.create_contact({"email": "bob@example.com"})
    suppressed_contact = await crm.create_contact({"email": "carol@example.com"})
    no_email = await crm.create_contact({"first_name": "NoEmail"})
    await crm.bulk_add_to_list(
        contact_list.list_id,
        [has_email_1.crm_contact_id, has_email_2.crm_contact_id, suppressed_contact.crm_contact_id, no_email.crm_contact_id],
    )

    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    await service.add_step(campaign.mail_campaign_id, "S1", "B1")
    await service.add_step(campaign.mail_campaign_id, "S2", "B2")

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails={"carol@example.com"})

    assert review.total_contacts == 4
    assert review.contacts_missing_email == 1
    assert review.contacts_suppressed == 1
    assert review.contacts_eligible == 2
    assert review.sequence_step_count == 2
    assert review.theoretical_total_sends == 4  # 2 eligible * 2 steps
    assert review.daily_capacity_estimate is None  # no mailbox config exists yet


async def test_review_reports_missing_list_gracefully(service, crm):
    contact_list = await crm.create_contact_list("Temp")
    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    await crm.delete_contact_list(contact_list.list_id)

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.source_list_exists is False
    assert review.total_contacts == 0


async def test_review_readiness_warnings_lists_every_real_problem_on_an_empty_draft(service):
    campaign = await service.create_campaign("Empty Draft")
    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())

    assert "No audience (CRM List) has been selected." in review.readiness_warnings
    assert "At least one sequence step is required." in review.readiness_warnings
    assert "At least one connected sending inbox must be selected." in review.readiness_warnings
    assert "A timezone is required." in review.readiness_warnings
    assert "At least one send window is required." in review.readiness_warnings
    assert len(review.readiness_warnings) == 5


async def test_review_readiness_warnings_is_empty_when_fully_configured(service, crm):
    campaign, _contact_list = await _make_valid_schedule_campaign(service, crm)
    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.readiness_warnings == []


async def test_review_readiness_warnings_reports_deleted_list_not_missing_audience(service, crm):
    contact_list = await crm.create_contact_list("Temp")
    campaign = await service.create_campaign("Draft")
    campaign = await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    await crm.delete_contact_list(contact_list.list_id)

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())

    assert "The selected CRM List no longer exists." in review.readiness_warnings
    assert "No audience (CRM List) has been selected." not in review.readiness_warnings


async def test_review_readiness_warnings_exactly_matches_mark_ready_rejection_reasons(service, crm):
    """The core guarantee this feature exists for: get_review()'s warnings
    and mark_ready()'s actual rejection are computed by the identical shared
    function, never two independently-maintained checks that could drift."""
    campaign = await service.create_campaign("Draft")
    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())

    with pytest.raises(MailCampaignNotReadyError) as exc_info:
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    assert exc_info.value.reasons == review.readiness_warnings


async def test_opening_review_never_mutates_anything(service, crm):
    """Zero-mutation guarantee -- call get_review() several times, at every
    campaign status reachable, and confirm no enrollment/step/campaign
    state changes as a side effect."""
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=3)

    before_steps = await service.list_steps(campaign.mail_campaign_id)
    before_campaign = await service.get_campaign(campaign.mail_campaign_id)

    for _ in range(3):
        await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())

    after_steps = await service.list_steps(campaign.mail_campaign_id)
    after_campaign = await service.get_campaign(campaign.mail_campaign_id)
    after_enrollments = await service.list_enrollments(campaign.mail_campaign_id)

    assert before_steps == after_steps
    assert before_campaign == after_campaign
    assert after_enrollments == []  # Review never enrolls anyone

    # Also true once READY.
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    enrollments_after_ready = await service.list_enrollments(ready.mail_campaign_id)
    for _ in range(3):
        await service.get_review(ready.mail_campaign_id, suppressed_emails=set())
    assert await service.list_enrollments(ready.mail_campaign_id) == enrollments_after_ready


# --- Channels (selected sending mailboxes) --------------------------------


async def test_channels_start_empty(service):
    campaign = await service.create_campaign("Channels")
    assert await service.list_channel_mailboxes(campaign.mail_campaign_id) == []


async def test_set_channel_mailboxes_selects_connected_mailboxes(service):
    campaign = await service.create_campaign("Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-a", email="a@example.com"))
    await service.mailbox_store.create(_make_mailbox("mbx-b", email="b@example.com"))

    resolved = await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a", "mbx-b"])
    assert {m.mailbox_id for m in resolved} == {"mbx-a", "mbx-b"}

    channels = await service.list_channel_mailboxes(campaign.mail_campaign_id)
    assert {m.mailbox_id for m in channels} == {"mbx-a", "mbx-b"}


async def test_set_channel_mailboxes_is_a_full_replace(service):
    campaign = await service.create_campaign("Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-a"))
    await service.mailbox_store.create(_make_mailbox("mbx-b", email="b@example.com"))

    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a"])
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-b"])

    channels = await service.list_channel_mailboxes(campaign.mail_campaign_id)
    assert [m.mailbox_id for m in channels] == ["mbx-b"]


async def test_set_channel_mailboxes_deduplicates_naturally(service):
    campaign = await service.create_campaign("Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-a"))
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a", "mbx-a", "mbx-a"])
    assert len(await service.list_channel_mailboxes(campaign.mail_campaign_id)) == 1


async def test_set_channel_mailboxes_rejects_unknown_mailbox_id(service):
    campaign = await service.create_campaign("Channels")
    with pytest.raises(MailboxChannelNotFoundError):
        await service.set_channel_mailboxes(campaign.mail_campaign_id, ["does-not-exist"])
    # Rejected atomically -- nothing partial was saved.
    assert await service.list_channel_mailboxes(campaign.mail_campaign_id) == []


async def test_set_channel_mailboxes_rejects_newly_selecting_a_disconnected_mailbox(service):
    campaign = await service.create_campaign("Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-gone", status=MailboxStatus.DISCONNECTED))
    with pytest.raises(MailboxChannelNotUsableError):
        await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-gone"])


async def test_set_channel_mailboxes_rejects_newly_selecting_a_needs_reauth_mailbox(service):
    campaign = await service.create_campaign("Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-stale", status=MailboxStatus.NEEDS_REAUTH))
    with pytest.raises(MailboxChannelNotUsableError):
        await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-stale"])


async def test_already_selected_mailbox_may_remain_after_becoming_disconnected(service):
    """The core 'don't silently remove historical selections' guarantee."""
    campaign = await service.create_campaign("Channels")
    mailbox = _make_mailbox("mbx-a")
    await service.mailbox_store.create(mailbox)
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a"])

    disconnected = mailbox.model_copy(update={"status": MailboxStatus.DISCONNECTED})
    await service.mailbox_store.save(disconnected)

    # Re-saving the SAME already-selected id must succeed even though it's
    # now disconnected -- only a genuinely NEW selection is blocked.
    resolved = await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a"])
    assert resolved[0].status == MailboxStatus.DISCONNECTED

    channels = await service.list_channel_mailboxes(campaign.mail_campaign_id)
    assert len(channels) == 1
    assert channels[0].status == MailboxStatus.DISCONNECTED


async def test_channels_editable_on_draft_campaign(service):
    campaign = await service.create_campaign("Draft Channels")
    await service.mailbox_store.create(_make_mailbox("mbx-draft"))
    resolved = await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-draft"])
    assert resolved[0].mailbox_id == "mbx-draft"


async def test_channels_editable_on_ready_campaign(service, crm):
    """Mailbox assignment is orthogonal to the audience/sequence/schedule
    lock -- unlike update_campaign()/add_step(), set_channel_mailboxes() is
    never blocked by MailCampaignNotEditableError on a READY campaign. This
    is deliberate: it's how a disconnected sender gets replaced without an
    unlock/re-snapshot round trip."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.mailbox_store.create(_make_mailbox("mbx-post-ready", email="post-ready@example.com"))
    resolved = await service.set_channel_mailboxes(ready.mail_campaign_id, ["mbx-post-ready"])
    assert resolved[0].mailbox_id == "mbx-post-ready"


async def test_channels_frozen_on_archived_campaign(service, crm):
    """ARCHIVED is terminal (no un-archive) -- its Channels selection is
    permanently frozen at whatever it was when archived."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    await service.mailbox_store.create(_make_mailbox("mbx-too-late", email="too-late@example.com"))
    with pytest.raises(MailCampaignChannelsFrozenError):
        await service.set_channel_mailboxes(archived.mail_campaign_id, ["mbx-too-late"])


async def test_channels_still_readable_on_archived_campaign(service, crm):
    """GET (list_channel_mailboxes) is never affected by the archive freeze
    -- an archived campaign's historical selection must remain visible."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    before_archive = await service.list_channel_mailboxes(campaign.mail_campaign_id)
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    after_archive = await service.list_channel_mailboxes(archived.mail_campaign_id)
    assert [m.mailbox_id for m in after_archive] == [m.mailbox_id for m in before_archive]
    assert len(after_archive) == 1


# --- Readiness: connected sending inbox requirement -----------------------


async def test_readiness_requires_a_connected_mailbox(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    c1 = await crm.create_contact({"email": "person@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])

    campaign = await service.create_campaign("No Mailbox")
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {"source_list_id": contact_list.list_id, "sending_days": [0], "start_time": "09:00", "end_time": "17:00", "timezone": "UTC"},
    )
    await service.add_step(campaign.mail_campaign_id, "Subject", "Body")

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert "At least one connected sending inbox must be selected." in review.readiness_warnings

    with pytest.raises(MailCampaignNotReadyError) as exc_info:
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert any("connected sending inbox" in r for r in exc_info.value.reasons)


async def test_readiness_fails_when_every_selected_mailbox_is_unusable(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    c1 = await crm.create_contact({"email": "person@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])

    campaign = await service.create_campaign("Stale Mailbox")
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {"source_list_id": contact_list.list_id, "sending_days": [0], "start_time": "09:00", "end_time": "17:00", "timezone": "UTC"},
    )
    await service.add_step(campaign.mail_campaign_id, "Subject", "Body")
    mailbox = _make_mailbox("mbx-a")
    await service.mailbox_store.create(mailbox)
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a"])
    await service.mailbox_store.save(mailbox.model_copy(update={"status": MailboxStatus.NEEDS_REAUTH}))

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert "At least one connected sending inbox must be selected." in review.readiness_warnings
    with pytest.raises(MailCampaignNotReadyError):
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())


async def test_readiness_succeeds_with_one_connected_selected_mailbox(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.readiness_warnings == []
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.status == MailCampaignStatus.READY


# --- Schedule (real send windows, legacy-compatible) -----------------------


async def test_schedule_starts_as_none_source_with_no_windows(service):
    campaign = await service.create_campaign("Fresh")
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert schedule.source == "none"
    assert schedule.windows == []
    assert schedule.timezone is None


async def test_legacy_schedule_synthesizes_correct_windows(service):
    """A campaign edited only through the OLD generic PATCH (sending_days/
    start_time/end_time/timezone) must read back as the equivalent windows,
    computed on the fly, without ever saving through the new Schedule API."""
    campaign = await service.create_campaign("Legacy")
    await service.update_campaign(
        campaign.mail_campaign_id,
        {"sending_days": [0, 2, 4], "start_time": "09:00", "end_time": "17:00", "timezone": "America/Chicago"},
    )
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert schedule.source == "legacy"
    assert schedule.timezone == "America/Chicago"
    assert {w.day_of_week for w in schedule.windows} == {0, 2, 4}
    assert all(w.start_time.isoformat() == "09:00:00" for w in schedule.windows)
    assert all(w.end_time.isoformat() == "17:00:00" for w in schedule.windows)


async def test_legacy_all_hours_synthesizes_the_00_00_2359_sentinel(service):
    campaign = await service.create_campaign("Legacy All Hours")
    await service.update_campaign(
        campaign.mail_campaign_id, {"sending_days": [0, 1], "all_hours": True, "timezone": "UTC"}
    )
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert schedule.source == "legacy"
    for w in schedule.windows:
        assert w.start_time.isoformat() == "00:00:00"
        assert w.end_time.isoformat() == "23:59:00"


async def test_no_legacy_fields_and_no_windows_resolves_to_none_source(service, crm):
    """A campaign with SOME legacy fields set but not a usable schedule
    (e.g. only a source_list_id, no schedule fields at all) must not
    fabricate a schedule -- source stays "none", not "legacy"."""
    contact_list = await crm.create_contact_list("Audience")
    campaign = await service.create_campaign("Partial")
    await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert schedule.source == "none"
    assert schedule.windows == []


async def test_new_explicit_windows_are_authoritative_over_legacy_fields(service):
    """The core compatibility guarantee: once real MailSendWindow rows
    exist, the legacy sending_days/start_time/end_time (still present and
    UNCHANGED on the campaign row) are permanently ignored."""
    campaign = await service.create_campaign("Migrating")
    await service.update_campaign(
        campaign.mail_campaign_id,
        {"sending_days": [0, 1, 2], "start_time": "09:00", "end_time": "17:00", "timezone": "UTC"},
    )
    legacy_schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert legacy_schedule.source == "legacy"

    await service.set_schedule(
        campaign.mail_campaign_id, "America/New_York", [(None, 3, "10:00", "14:00")]
    )

    resolved = await service.get_schedule(campaign.mail_campaign_id)
    assert resolved.source == "windows"
    assert resolved.timezone == "America/New_York"
    assert [w.day_of_week for w in resolved.windows] == [3]

    # And the legacy campaign fields are untouched, not zeroed/synced.
    reloaded_campaign = await service.get_campaign(campaign.mail_campaign_id)
    assert reloaded_campaign.sending_days == [0, 1, 2]
    assert reloaded_campaign.start_time.isoformat() == "09:00:00"


async def test_first_new_format_save_transitions_cleanly_from_legacy(service):
    campaign = await service.create_campaign("Transition")
    await service.update_campaign(
        campaign.mail_campaign_id,
        {"sending_days": [0], "start_time": "08:00", "end_time": "12:00", "timezone": "UTC"},
    )
    before = await service.get_schedule(campaign.mail_campaign_id)
    assert before.source == "legacy"

    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00")])
    assert saved.source == "windows"

    after = await service.get_schedule(campaign.mail_campaign_id)
    assert after.source == "windows"
    assert len(after.windows) == 1


async def test_different_times_on_different_weekdays(service):
    campaign = await service.create_campaign("Varying Hours")
    await service.set_schedule(
        campaign.mail_campaign_id,
        "UTC",
        [(None, 0, "08:00", "12:00"), (None, 1, "09:00", "17:00"), (None, 4, "06:00", "10:00")],
    )
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    by_day = {w.day_of_week: (w.start_time.isoformat(), w.end_time.isoformat()) for w in schedule.windows}
    assert by_day[0] == ("08:00:00", "12:00:00")
    assert by_day[1] == ("09:00:00", "17:00:00")
    assert by_day[4] == ("06:00:00", "10:00:00")


async def test_multiple_windows_on_one_weekday(service):
    campaign = await service.create_campaign("Split Monday")
    await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00"), (None, 0, "14:00", "18:00")]
    )
    schedule = await service.get_schedule(campaign.mail_campaign_id)
    assert len(schedule.windows) == 2
    assert all(w.day_of_week == 0 for w in schedule.windows)


async def test_adding_and_removing_windows_via_resave(service):
    campaign = await service.create_campaign("Add Remove")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00")])
    await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00"), (None, 0, "14:00", "18:00"), (None, 2, "09:00", "10:00")]
    )
    grown = await service.get_schedule(campaign.mail_campaign_id)
    assert len(grown.windows) == 3

    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00")])
    shrunk = await service.get_schedule(campaign.mail_campaign_id)
    assert len(shrunk.windows) == 1


async def test_set_schedule_empty_windows_is_a_valid_intentional_save(service):
    """Saving zero windows is allowed mid-draft (an intentional all-days-off
    schedule) -- only readiness requires at least one."""
    campaign = await service.create_campaign("All Off")
    result = await service.set_schedule(campaign.mail_campaign_id, "UTC", [])
    assert result.windows == []
    assert result.source == "windows"


async def test_set_schedule_rejects_overlapping_windows_same_day(service):
    campaign = await service.create_campaign("Overlap")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(
            campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "13:00"), (None, 0, "12:00", "18:00")]
        )


async def test_set_schedule_allows_back_to_back_touching_windows(service):
    campaign = await service.create_campaign("Back To Back")
    result = await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00"), (None, 0, "12:00", "18:00")]
    )
    assert len(result.windows) == 2


async def test_set_schedule_rejects_overlap_across_more_than_two_windows(service):
    campaign = await service.create_campaign("Triple Overlap")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(
            campaign.mail_campaign_id,
            "UTC",
            [(None, 1, "08:00", "10:00"), (None, 1, "09:30", "11:00"), (None, 1, "12:00", "13:00")],
        )


async def test_set_schedule_overlap_on_one_day_does_not_block_other_days(service):
    """Overlap validation is per-weekday -- windows on DIFFERENT days must
    never be compared against each other for overlap."""
    result_campaign = await service.create_campaign("Independent Days")
    result = await service.set_schedule(
        result_campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "20:00"), (None, 1, "08:00", "20:00")]
    )
    assert len(result.windows) == 2


async def test_set_schedule_rejects_zero_duration_window(service):
    campaign = await service.create_campaign("Zero Duration")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "09:00")])


async def test_set_schedule_rejects_negative_duration_window(service):
    campaign = await service.create_campaign("Negative Duration")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "17:00", "09:00")])


async def test_set_schedule_rejects_invalid_day_of_week(service):
    campaign = await service.create_campaign("Bad Day")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 7, "09:00", "10:00")])


async def test_set_schedule_accepts_full_day_boundary_00_00_to_23_59(service):
    campaign = await service.create_campaign("Full Day")
    result = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "00:00", "23:59")])
    assert result.windows[0].start_time.isoformat() == "00:00:00"
    assert result.windows[0].end_time.isoformat() == "23:59:00"


async def test_set_schedule_rejects_invalid_timezone(service):
    campaign = await service.create_campaign("Bad TZ")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "Nowhere/Real", [(None, 0, "09:00", "10:00")])


async def test_set_schedule_a_rejected_save_leaves_the_previous_schedule_untouched(service):
    campaign = await service.create_campaign("Rejected Save")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00")])

    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "13:00", "10:00")])  # invalid

    unchanged = await service.get_schedule(campaign.mail_campaign_id)
    assert len(unchanged.windows) == 1
    assert unchanged.windows[0].start_time.isoformat() == "08:00:00"


async def test_set_schedule_rejected_on_ready_campaign(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    with pytest.raises(MailCampaignNotEditableError):
        await service.set_schedule(ready.mail_campaign_id, "UTC", [(None, 0, "09:00", "10:00")])


async def test_get_schedule_still_works_on_ready_campaign(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    schedule = await service.get_schedule(ready.mail_campaign_id)
    assert schedule.source == "legacy"  # _make_valid_schedule_campaign never saved through set_schedule()


async def test_set_schedule_rejected_on_archived_campaign(service):
    campaign = await service.create_campaign("Archive Schedule")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "10:00")])
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    with pytest.raises(MailCampaignNotEditableError):
        await service.set_schedule(archived.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])


async def test_get_schedule_still_works_on_archived_campaign(service):
    campaign = await service.create_campaign("Archive Schedule View")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "10:00")])
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    schedule = await service.get_schedule(archived.mail_campaign_id)
    assert schedule.source == "windows"
    assert len(schedule.windows) == 1


# --- Readiness with the new schedule representation ------------------------


async def test_readiness_fails_with_zero_windows(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    c1 = await crm.create_contact({"email": "a@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])
    mailbox = _make_mailbox("mbx-a")

    campaign = await service.create_campaign("No Windows")
    await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    await service.add_step(campaign.mail_campaign_id, "S", "B")
    await service.mailbox_store.create(mailbox)
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-a"])
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [])  # explicit, intentional zero windows

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert "At least one send window is required." in review.readiness_warnings
    with pytest.raises(MailCampaignNotReadyError):
        await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())


async def test_readiness_succeeds_with_legacy_schedule(service, crm):
    """A pre-existing campaign that predates the Schedule tab rewrite (never
    saved through set_schedule()) must still be able to reach Ready via its
    legacy fields, synthesized into windows for readiness purposes."""
    contact_list = await crm.create_contact_list("Audience")
    c1 = await crm.create_contact({"email": "a@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])
    mailbox = _make_mailbox("mbx-legacy")

    campaign = await service.create_campaign("Legacy Ready")
    await service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": contact_list.list_id,
            "sending_days": [0, 1, 2, 3, 4],
            "start_time": "09:00",
            "end_time": "17:00",
            "timezone": "America/Chicago",
        },
    )
    await service.add_step(campaign.mail_campaign_id, "S", "B")
    await service.mailbox_store.create(mailbox)
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-legacy"])

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.readiness_warnings == []
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.status == MailCampaignStatus.READY


async def test_readiness_succeeds_with_new_explicit_windows(service, crm):
    contact_list = await crm.create_contact_list("Audience")
    c1 = await crm.create_contact({"email": "a@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [c1.crm_contact_id])
    mailbox = _make_mailbox("mbx-new")

    campaign = await service.create_campaign("New Windows Ready")
    await service.update_campaign(campaign.mail_campaign_id, {"source_list_id": contact_list.list_id})
    await service.add_step(campaign.mail_campaign_id, "S", "B")
    await service.mailbox_store.create(mailbox)
    await service.set_channel_mailboxes(campaign.mail_campaign_id, ["mbx-new"])
    await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(None, 0, "08:00", "12:00"), (None, 2, "09:00", "17:00")]
    )

    review = await service.get_review(campaign.mail_campaign_id, suppressed_emails=set())
    assert review.readiness_warnings == []
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.status == MailCampaignStatus.READY


# --- Legacy schedule fields locked once in window mode ----------------------


async def test_legacy_schedule_patch_works_before_any_window_exists(service):
    """Backward compatibility: a campaign that has never been saved through
    the new Schedule API can still be configured entirely via the old
    generic PATCH."""
    campaign = await service.create_campaign("Still Legacy")
    updated = await service.update_campaign(
        campaign.mail_campaign_id,
        {"sending_days": [0, 1], "start_time": "09:00", "end_time": "17:00", "timezone": "UTC"},
    )
    assert updated.sending_days == [0, 1]
    assert updated.timezone == "UTC"


async def test_legacy_schedule_patch_rejected_once_windows_exist(service):
    campaign = await service.create_campaign("Migrated")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"sending_days": [0, 1, 2]})
    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"start_time": "10:00"})
    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"end_time": "18:00"})
    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"all_hours": True})


async def test_timezone_patch_rejected_once_windows_exist(service):
    """timezone is deliberately included in the lock -- the entire schedule
    configuration has exactly one authoritative write path once migrated."""
    campaign = await service.create_campaign("Migrated TZ")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"timezone": "America/Chicago"})

    # And the campaign's real timezone (set via set_schedule) is untouched.
    reloaded = await service.get_campaign(campaign.mail_campaign_id)
    assert reloaded.timezone == "UTC"


async def test_legacy_patch_rejects_the_whole_request_not_just_the_schedule_part(service):
    """Mixing a legacy schedule field with an unrelated field (name) in one
    PATCH must reject the ENTIRE call -- nothing partially applied."""
    campaign = await service.create_campaign("Mixed Patch")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    with pytest.raises(MailCampaignLegacyScheduleLockedError):
        await service.update_campaign(campaign.mail_campaign_id, {"name": "New Name", "start_time": "10:00"})

    unchanged = await service.get_campaign(campaign.mail_campaign_id)
    assert unchanged.name == "Mixed Patch"


async def test_daily_lead_start_limit_still_patchable_after_migration(service):
    """daily_lead_start_limit is explicitly NOT part of the schedule lock --
    it must keep working normally on a window-mode campaign."""
    campaign = await service.create_campaign("Independent Setting")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    updated = await service.update_campaign(campaign.mail_campaign_id, {"daily_lead_start_limit": 25})
    assert updated.daily_lead_start_limit == 25


async def test_name_and_sharing_still_patchable_after_migration(service):
    """Confirms the lock is scoped to schedule fields only, not a blanket
    freeze of the whole campaign."""
    campaign = await service.create_campaign("Still Editable")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    updated = await service.update_campaign(campaign.mail_campaign_id, {"name": "Renamed", "sharing": "only_me"})
    assert updated.name == "Renamed"
    assert updated.sharing.value == "only_me"


async def test_campaign_creation_with_legacy_schedule_shape_is_unaffected(service, crm):
    """A brand-new campaign can never already have window rows, so its
    initial legacy-shaped configuration (as sent by the Create Campaign
    modal) must never be rejected by the new lock."""
    campaign = await service.create_campaign("New Campaign")
    updated = await service.update_campaign(
        campaign.mail_campaign_id,
        {"sending_days": [0, 1, 2, 3, 4], "start_time": "08:00", "end_time": "18:00", "timezone": "America/Chicago"},
    )
    assert updated.sending_days == [0, 1, 2, 3, 4]


# --- Stable window IDs across edits -----------------------------------------


async def test_editing_an_existing_window_preserves_its_id(service):
    campaign = await service.create_campaign("Stable ID")
    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])
    original_id = saved.windows[0].window_id

    moved = await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(original_id, 0, "10:00", "18:00")]
    )
    assert len(moved.windows) == 1
    assert moved.windows[0].window_id == original_id
    assert moved.windows[0].start_time.isoformat() == "10:00:00"


async def test_editing_an_existing_window_preserves_created_at(service):
    campaign = await service.create_campaign("Preserve Created At")
    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])
    original_id = saved.windows[0].window_id
    original_created_at = saved.windows[0].created_at

    moved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(original_id, 1, "10:00", "18:00")])
    assert moved.windows[0].created_at == original_created_at
    assert moved.windows[0].updated_at >= original_created_at


async def test_unchanged_window_resubmitted_with_its_id_keeps_that_id(service):
    campaign = await service.create_campaign("Unchanged Resave")
    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])
    original_id = saved.windows[0].window_id

    resaved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(original_id, 0, "09:00", "17:00")])
    assert resaved.windows[0].window_id == original_id


async def test_new_window_added_alongside_existing_one_gets_a_new_id(service):
    campaign = await service.create_campaign("Add One More")
    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "12:00")])
    original_id = saved.windows[0].window_id

    grown = await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(original_id, 0, "09:00", "12:00"), (None, 1, "09:00", "17:00")]
    )
    ids = {w.window_id for w in grown.windows}
    assert original_id in ids
    assert len(ids) == 2  # the new window got a genuinely different id


async def test_omitting_a_window_id_removes_that_window(service):
    campaign = await service.create_campaign("Remove By Omission")
    saved = await service.set_schedule(
        campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "12:00"), (None, 1, "09:00", "17:00")]
    )
    keep_id = saved.windows[0].window_id if saved.windows[0].day_of_week == 0 else saved.windows[1].window_id

    shrunk = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(keep_id, 0, "09:00", "12:00")])
    assert len(shrunk.windows) == 1
    assert shrunk.windows[0].window_id == keep_id


async def test_set_schedule_rejects_a_window_id_belonging_to_another_campaign(service):
    campaign_a = await service.create_campaign("Campaign A")
    campaign_b = await service.create_campaign("Campaign B")
    saved_a = await service.set_schedule(campaign_a.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])
    foreign_id = saved_a.windows[0].window_id

    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign_b.mail_campaign_id, "UTC", [(foreign_id, 0, "09:00", "17:00")])

    # Campaign A's own window must be completely unaffected by the rejected
    # attempt against campaign B.
    unaffected = await service.get_schedule(campaign_a.mail_campaign_id)
    assert unaffected.windows[0].window_id == foreign_id


async def test_set_schedule_rejects_a_made_up_window_id(service):
    campaign = await service.create_campaign("Made Up ID")
    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [("does-not-exist", 0, "09:00", "17:00")])


async def test_set_schedule_rejects_a_duplicate_window_id_in_one_request(service):
    campaign = await service.create_campaign("Duplicate ID")
    saved = await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])
    real_id = saved.windows[0].window_id

    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(
            campaign.mail_campaign_id, "UTC", [(real_id, 0, "09:00", "12:00"), (real_id, 1, "13:00", "17:00")]
        )


async def test_rejected_window_id_save_leaves_the_previous_schedule_untouched(service):
    """Atomicity still holds when the rejection is an id-ownership/
    duplicate problem, not just a time/overlap problem."""
    campaign = await service.create_campaign("Atomic ID Rejection")
    await service.set_schedule(campaign.mail_campaign_id, "UTC", [(None, 0, "09:00", "17:00")])

    with pytest.raises(MailScheduleValidationError):
        await service.set_schedule(campaign.mail_campaign_id, "UTC", [("bogus-id", 1, "09:00", "17:00")])

    unchanged = await service.get_schedule(campaign.mail_campaign_id)
    assert len(unchanged.windows) == 1
    assert unchanged.windows[0].day_of_week == 0


# --- actor attribution (Phase 2, admin/service OPERATOR token) -----------
#
# `actor` is purely additive -- every call site above this section omits
# it and keeps getting actor=None (see the assertions below), matching
# ActivityEvent.actor's own "always None today" docstring for every
# caller except the ones explicitly tested here.


async def test_create_campaign_actor_defaults_to_none(service, activity_log):
    await service.create_campaign("Draft")

    events = await activity_log.store.list()
    created = next(e for e in events if e.event_type == "mail_campaign.created")
    assert created.actor is None


async def test_create_campaign_records_the_given_actor(service, activity_log):
    await service.create_campaign("Draft", actor="claude_operator")

    events = await activity_log.store.list()
    created = next(e for e in events if e.event_type == "mail_campaign.created")
    assert created.actor == "claude_operator"


async def test_update_campaign_records_the_given_actor(service, activity_log):
    campaign = await service.create_campaign("Draft")
    await service.update_campaign(campaign.mail_campaign_id, {"name": "Renamed"}, actor="claude_operator")

    events = await activity_log.store.list()
    updated = next(e for e in events if e.event_type == "mail_campaign.updated")
    assert updated.actor == "claude_operator"


async def test_update_campaign_actor_defaults_to_none(service, activity_log):
    campaign = await service.create_campaign("Draft")
    await service.update_campaign(campaign.mail_campaign_id, {"name": "Renamed"})

    events = await activity_log.store.list()
    updated = next(e for e in events if e.event_type == "mail_campaign.updated")
    assert updated.actor is None


async def test_set_schedule_records_the_given_actor(service):
    campaign = await service.create_campaign("Draft")
    await service.set_schedule(
        campaign.mail_campaign_id, "America/Chicago", [(None, 0, "09:00", "17:00")], actor="claude_operator"
    )

    events = [e for e in await service.activity_log.store.list() if e.event_type == "mail_campaign.schedule_updated"]
    assert events[0].actor == "claude_operator"


async def test_mark_ready_records_the_given_actor_on_both_emitted_events(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set(), actor="claude_operator")

    events = await service.activity_log.store.list()
    ready_event = next(e for e in events if e.event_type == "mail_campaign.ready")
    enrolled_event = next(e for e in events if e.event_type == "mail_enrollment.enrolled")
    assert ready_event.actor == "claude_operator"
    assert enrolled_event.actor == "claude_operator"


async def test_mark_ready_actor_defaults_to_none(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=1)
    await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    events = await service.activity_log.store.list()
    ready_event = next(e for e in events if e.event_type == "mail_campaign.ready")
    assert ready_event.actor is None


async def test_unlock_campaign_records_the_given_actor(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=1)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.unlock_campaign(ready.mail_campaign_id, actor="claude_operator")

    events = await service.activity_log.store.list()
    unlocked_event = next(
        e for e in events if e.event_type == "mail_campaign.updated" and "unlocked" in e.summary.lower()
    )
    assert unlocked_event.actor == "claude_operator"


# --- activate_campaign actor attribution (Phase 2, admin/service OPERATOR --
# token, approved 2026-09-03 as a safety gate separate from actual sending) -


async def test_activate_a_valid_ready_campaign_succeeds_with_the_given_actor(service, crm):
    """The operator identity CAN activate a genuinely READY campaign --
    companion to test_activate_a_valid_ready_campaign_succeeds above,
    proving the `actor` argument doesn't change the outcome, only the
    attribution."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    activated = await service.activate_campaign(ready.mail_campaign_id, actor="claude_operator")

    assert activated.status == MailCampaignStatus.ACTIVE

    events = await service.activity_log.store.list()
    activated_event = next(e for e in events if e.event_type == "mail_campaign.activated")
    assert activated_event.actor == "claude_operator"


async def test_activate_actor_defaults_to_none(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    await service.activate_campaign(ready.mail_campaign_id)

    events = await service.activity_log.store.list()
    activated_event = next(e for e in events if e.event_type == "mail_campaign.activated")
    assert activated_event.actor is None


async def test_operator_identity_cannot_activate_a_non_ready_campaign_merely_by_being_authorized(service):
    """The core guarantee behind granting Activate to the operator token at
    all: `actor` carries NO special privilege through MailCampaignService
    -- a DRAFT campaign is rejected with the EXACT SAME
    MailCampaignInvalidTransitionError regardless of who (or what) is
    calling, matching test_activate_requires_ready above byte-for-byte
    except for the added actor kwarg."""
    campaign = await service.create_campaign("Draft")

    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.activate_campaign(campaign.mail_campaign_id, actor="claude_operator")

    unchanged = await service.get_campaign(campaign.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.DRAFT


async def test_operator_identity_still_hits_the_sending_engine_gate(service, crm, monkeypatch):
    """Companion to test_activate_refused_when_sending_engine_disabled --
    the deployment-wide mail_sending_engine_enabled gate is completely
    outside MailCampaignService/the operator token's reach (see
    app/session_auth_middleware.py's own docstring: this token has no
    path to that Railway-environment-variable-only setting at all), so it
    refuses activation identically regardless of `actor`."""
    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    with pytest.raises(MailSendingEngineDisabledError):
        await service.activate_campaign(ready.mail_campaign_id, actor="claude_operator")

    unchanged = await service.get_campaign(ready.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.READY


# --- pause_campaign actor attribution (Phase 2, admin/service OPERATOR -----
# token, approved 2026-09-03 as Activate's safe inverse) --------------------


async def test_operator_can_pause_an_active_campaign(service, crm):
    """Companion to test_pause_then_resume_round_trip above -- the
    operator identity CAN pause a genuinely ACTIVE campaign."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)

    paused = await service.pause_campaign(ready.mail_campaign_id, actor="claude_operator")

    assert paused.status == MailCampaignStatus.PAUSED

    events = await service.activity_log.store.list()
    paused_event = next(e for e in events if e.event_type == "mail_campaign.paused")
    assert paused_event.actor == "claude_operator"


async def test_pause_actor_defaults_to_none(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await service.activate_campaign(ready.mail_campaign_id)

    await service.pause_campaign(ready.mail_campaign_id)

    events = await service.activity_log.store.list()
    paused_event = next(e for e in events if e.event_type == "mail_campaign.paused")
    assert paused_event.actor is None


async def test_operator_identity_cannot_pause_a_non_active_campaign_merely_by_being_authorized(service):
    """The same core guarantee as Activate's version of this test: `actor`
    carries NO special privilege -- a DRAFT campaign (never activated) is
    rejected with the EXACT SAME MailCampaignInvalidTransitionError
    regardless of who/what is calling, matching test_pause_requires_active
    above byte-for-byte except for the added actor kwarg."""
    campaign = await service.create_campaign("Draft")

    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.pause_campaign(campaign.mail_campaign_id, actor="claude_operator")

    unchanged = await service.get_campaign(campaign.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.DRAFT


async def test_operator_identity_cannot_pause_a_ready_campaign_merely_by_being_authorized(service, crm):
    """A second non-ACTIVE state, closer to what this token will actually
    encounter in practice (a campaign it just Marked Ready but hasn't
    Activated) -- still rejected identically regardless of actor."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.pause_campaign(ready.mail_campaign_id, actor="claude_operator")

    unchanged = await service.get_campaign(ready.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.READY


# --- get_workload() / list_batches() (Phase 2, 2026-09-03) -----------------


def _make_raw_enrollment(enrollment_id, campaign_id, status, batch_id=None) -> MailEnrollment:
    now = datetime.now(timezone.utc)
    return MailEnrollment(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=f"contact-{enrollment_id}",
        email_at_enrollment=f"{enrollment_id}@example.com",
        status=status,
        enrolled_at=now,
        created_at=now,
        batch_id=batch_id,
    )


async def test_get_workload_not_found_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.get_workload("does-not-exist")


async def test_get_workload_is_all_zero_for_a_campaign_with_no_enrollments(service):
    campaign = await service.create_campaign("Draft")
    workload = await service.get_workload(campaign.mail_campaign_id)
    assert workload.mail_campaign_id == campaign.mail_campaign_id
    assert (workload.total, workload.pending, workload.active, workload.paused, workload.completed, workload.suppressed, workload.failed) == (0, 0, 0, 0, 0, 0, 0)


async def test_get_workload_counts_every_status_independently(service):
    campaign = await service.create_campaign("Draft")
    cid = campaign.mail_campaign_id
    await service.enrollment_store.create(_make_raw_enrollment("e-pending", cid, MailEnrollmentStatus.PENDING))
    await service.enrollment_store.create(_make_raw_enrollment("e-active-1", cid, MailEnrollmentStatus.ACTIVE))
    await service.enrollment_store.create(_make_raw_enrollment("e-active-2", cid, MailEnrollmentStatus.ACTIVE))
    await service.enrollment_store.create(_make_raw_enrollment("e-paused", cid, MailEnrollmentStatus.PAUSED))
    await service.enrollment_store.create(_make_raw_enrollment("e-completed", cid, MailEnrollmentStatus.COMPLETED))
    await service.enrollment_store.create(_make_raw_enrollment("e-suppressed", cid, MailEnrollmentStatus.SUPPRESSED))
    await service.enrollment_store.create(_make_raw_enrollment("e-failed", cid, MailEnrollmentStatus.FAILED))
    # A different campaign's enrollments must never leak into this count.
    other = await service.create_campaign("Other")
    await service.enrollment_store.create(_make_raw_enrollment("e-other", other.mail_campaign_id, MailEnrollmentStatus.ACTIVE))

    workload = await service.get_workload(cid)
    assert workload.pending == 1
    assert workload.active == 2
    assert workload.paused == 1
    assert workload.completed == 1
    assert workload.suppressed == 1
    assert workload.failed == 1
    assert workload.total == 7
    assert workload.total == (
        workload.pending + workload.active + workload.paused + workload.completed + workload.suppressed + workload.failed
    )


async def test_list_batches_not_found_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.list_batches("does-not-exist")


async def test_list_batches_is_empty_for_a_campaign_with_no_batches(service):
    campaign = await service.create_campaign("Draft")
    assert await service.list_batches(campaign.mail_campaign_id) == []


async def test_list_batches_is_campaign_scoped_and_newest_first(service, batch_store):
    campaign = await service.create_campaign("Draft")
    cid = campaign.mail_campaign_id
    other = await service.create_campaign("Other")
    now = datetime.now(timezone.utc)

    def _batch(batch_id, campaign_id, created_at):
        return MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id="list-1", idempotency_key=f"key-{batch_id}",
            status=MailEnrollmentBatchStatus.READY, created_at=created_at,
            submitted_count=1, enrolled_count=1, already_enrolled_count=0, suppressed_count=0,
        )

    await batch_store.create(_batch("b1", cid, now))
    await batch_store.create(_batch("b2", cid, now + timedelta(hours=1)))  # newest
    await batch_store.create(_batch("b3", other.mail_campaign_id, now + timedelta(hours=2)))  # different campaign

    batches = await service.list_batches(cid)
    assert [b.batch_id for b in batches] == ["b2", "b1"]
    assert all(b.mail_campaign_id == cid for b in batches)
