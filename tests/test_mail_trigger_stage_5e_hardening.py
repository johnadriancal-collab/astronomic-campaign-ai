"""
MailTriggerService missed-occurrence / catch-up / pause-resume hardening --
Stage 5E (2026-09-04).

Builds directly on Stage 5D's occurrence-execution suite
(tests/test_mail_trigger_occurrence_execution.py) -- reuses its
`_make_triggered_active_campaign` fixture helper and fixed `NOW` constant
(same file-redeclares-its-own-fixtures convention as every other Trigger
test file in this codebase; no conftest.py exists).

Covers: the "no prior-day debt" catch-up policy, the campaign-wide
"latest-due-only" policy and its durable superseded-occurrence mechanism,
existing-PREPARING-occurrence priority (including crash-mid-supersede
re-derivation), pause semantics, the resume + stale-PREPARING-occurrence
interaction (the one behavior explicitly flagged for user confirmation --
see this stage's STOP report), restart/no-in-memory-state, duplicate-
schedule validation, trigger-edit/occurrence-immutability, and DST.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.mail import MailCampaignStatus, MailEnrollmentStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MemoryMailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_lead_start_trigger_store import MemoryMailLeadStartTriggerStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mail_trigger_occurrence_store import MemoryMailTriggerOccurrenceStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService
from app.services.mail_trigger_service import MailTriggerService
from app.models.mail import MailTriggerOccurrence
from app.services import mail_campaign_service as mail_campaign_service_module
from tests.test_mail_campaign_service import _make_mailbox
from tests.test_mail_trigger_occurrence_execution import NOW, _make_triggered_active_campaign

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
def policy_store():
    return MemoryMailboxSendPolicyStore()


@pytest.fixture
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def sending_service(campaign_store, enrollment_store, enrollment_step_store, mailbox_store, channel_store, policy_store, suppression_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=enrollment_step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=policy_store,
        suppression_store=suppression_store, activity_log=activity_log,
    )


@pytest.fixture
def campaign_service(
    crm, activity_log, mailbox_store, channel_store, window_store, enrollment_step_store, suppression_store,
    batch_store, batch_member_store, campaign_store, enrollment_store, crm_import_service, sending_service,
):
    return MailCampaignService(
        campaign_store=campaign_store, step_store=MemoryMailSequenceStepStore(), enrollment_store=enrollment_store,
        crm_service=crm, activity_log=activity_log, mailbox_store=mailbox_store, channel_store=channel_store,
        window_store=window_store, enrollment_step_store=enrollment_step_store, sending_service=sending_service,
        batch_store=batch_store, batch_member_store=batch_member_store, suppression_store=suppression_store,
        crm_import_reader=crm_import_service,
    )


def _make_trigger_service(
    trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
    sending_service, campaign_service, activity_log,
):
    return MailTriggerService(
        trigger_store=trigger_store, occurrence_store=occurrence_store, campaign_store=campaign_store,
        enrollment_store=enrollment_store, enrollment_step_store=enrollment_step_store,
        suppression_store=suppression_store, sending_service=sending_service, mail_campaign_service=campaign_service,
        activity_log=activity_log,
    )


@pytest.fixture
def trigger_service(
    trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
    sending_service, campaign_service, activity_log,
):
    return _make_trigger_service(
        trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
        sending_service, campaign_service, activity_log,
    )


# =====================================================================
# No prior-day debt (test list item 12)
# =====================================================================


async def test_a_missed_prior_day_occurrence_is_never_created_or_backfilled(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store,
):
    """A trigger whose weekday matches YESTERDAY (campaign-local), not
    today, must produce no occurrence at all when today's tick runs --
    Stage 5E's explicit "no catch-up" policy. Uses UTC as the campaign
    timezone (matching the shared fixture helper), so campaign-local date
    == NOW's own UTC date."""
    yesterday_weekday = (NOW.weekday() - 1) % 7
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, weekdays=[yesterday_weekday],
    )
    await trigger_service.process_due_occurrences(NOW)

    triggers = await trigger_service.list_triggers(active.mail_campaign_id)
    assert await occurrence_store.get_occurrence(triggers[0].trigger_id, NOW) is None
    enrollments = await trigger_service.enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


# =====================================================================
# Campaign-wide latest-selected-today, derived from durable history,
# never a synthetic row for a never-selected loser (test list items 1-2)
# =====================================================================


async def test_first_tick_at_3pm_with_9am_and_2pm_due_only_2pm_gets_a_row(
    trigger_service, campaign_service, crm, campaign_store, enrollment_store, activity_log,
):
    """Test list item 1. Both triggers are ALREADY due by the time the
    first tick of the day runs -- only the latest (2pm) is selected, and
    the earlier (9am) NEVER gets an occurrence row, member rows, or an
    Activity Log event of any kind. This is the core behavior change from
    the (rejected) synthetic-supersede design."""
    active, _, morning_trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, local_time="09:00", leads_to_start=20,
    )
    afternoon_trigger = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 20)

    late_now = NOW.replace(hour=15)
    await trigger_service.process_due_occurrences(late_now)

    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    afternoon_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(14, 0), "UTC")

    assert await trigger_service.occurrence_store.get_occurrence(morning_trigger.trigger_id, morning_scheduled_for) is None

    afternoon_occ = await trigger_service.occurrence_store.get_occurrence(afternoon_trigger.trigger_id, afternoon_scheduled_for)
    assert afternoon_occ.status == "COMPLETED"
    assert afternoon_occ.started_count == 3

    page = await activity_log.list_events(page_size=50)
    event_types = [e.event_type for e in page.items]
    assert "mail_trigger_occurrence.superseded" not in event_types  # never-selected loser -- no event at all
    assert "mail_trigger_occurrence.completed" in event_types


async def test_restart_at_305_does_not_resurrect_9am(
    trigger_service, campaign_service, crm, campaign_store,
):
    """Test list item 2. After the first tick already picked 2pm as the
    winner, a "restart" (a brand-new tick, simulating the worker coming
    back) must re-derive the exact same exclusion from durable history
    alone -- 9am must never be created on any later tick either."""
    active, _, morning_trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, local_time="09:00",
    )
    afternoon_trigger = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 20)

    await trigger_service.process_due_occurrences(NOW.replace(hour=15))
    await trigger_service.process_due_occurrences(NOW.replace(hour=15, minute=5))  # "restart at 3:05"

    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    assert await trigger_service.occurrence_store.get_occurrence(morning_trigger.trigger_id, morning_scheduled_for) is None


async def test_905_executes_9am_then_205_executes_2pm_both_legitimate(
    trigger_service, campaign_service, crm, campaign_store, enrollment_store,
):
    """Test list item 3. Sequential, on-time discovery is NOT "one
    trigger per day" -- it is "discard accumulated missed debt". Both
    fire when the worker is actually alive to see each become due."""
    active, _, morning_trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=5, local_time="09:00", leads_to_start=1,
    )
    afternoon_trigger = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 1)

    await trigger_service.process_due_occurrences(NOW)  # 9:05 -- only 9am due
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert sum(1 for e in enrollments if e.status == MailEnrollmentStatus.ACTIVE) == 1

    await trigger_service.process_due_occurrences(NOW.replace(hour=14, minute=5))  # 2:05 -- 2pm now due
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert sum(1 for e in enrollments if e.status == MailEnrollmentStatus.ACTIVE) == 2

    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    afternoon_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(14, 0), "UTC")
    morning_occ = await trigger_service.occurrence_store.get_occurrence(morning_trigger.trigger_id, morning_scheduled_for)
    afternoon_occ = await trigger_service.occurrence_store.get_occurrence(afternoon_trigger.trigger_id, afternoon_scheduled_for)
    assert morning_occ.status == "COMPLETED" and morning_occ.started_count == 1
    assert afternoon_occ.status == "COMPLETED" and afternoon_occ.started_count == 1


async def test_restart_between_legitimate_sequential_executions_still_permits_2pm(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
    enrollment_step_store, suppression_store, sending_service, activity_log,
):
    """Test list item 4. A fresh MailTriggerService instance (simulating a
    process restart) sharing the same durable stores, invoked only AFTER
    9am already legitimately completed, must still allow 2pm to execute
    once it becomes due -- restart introduces no in-memory state loss
    that would matter here."""
    active, _, morning_trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, local_time="09:00", leads_to_start=1,
    )
    afternoon_trigger = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 1)
    await trigger_service.process_due_occurrences(NOW)  # 9am executes legitimately

    restarted_service = _make_trigger_service(
        trigger_service.trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store,
        suppression_store, sending_service, campaign_service, activity_log,
    )
    await restarted_service.process_due_occurrences(NOW.replace(hour=14, minute=5))

    afternoon_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(14, 0), "UTC")
    afternoon_occ = await occurrence_store.get_occurrence(afternoon_trigger.trigger_id, afternoon_scheduled_for)
    assert afternoon_occ is not None and afternoon_occ.status == "COMPLETED" and afternoon_occ.started_count == 1


async def test_latest_selected_occurrence_with_zero_eligible_leads_is_completed_not_superseded(
    trigger_service, campaign_service, crm, campaign_store, enrollment_store,
):
    """Test list item 5. The genuinely-selected winner finding zero
    eligible PENDING candidates is COMPLETED/started_count=0 -- NEVER
    SUPERSEDED. Every enrollment is pre-activated so nothing is eligible."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3,
    )
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    for e in enrollments:
        await enrollment_store.save(e.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))

    await trigger_service.process_due_occurrences(NOW)

    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    occ = await trigger_service.occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "COMPLETED"
    assert occ.started_count == 0
    assert occ.frozen_at is not None  # a real freeze attempt happened, just selected nobody


async def test_superseded_occurrence_is_never_reevaluated_on_a_later_tick(
    trigger_service, campaign_service, crm, campaign_store,
):
    """A never-selected loser stays nonexistent forever, and a genuinely
    executed occurrence stays untouched on later ticks -- both checked
    together as the steady-state idempotency contrast to the above."""
    active, _, morning = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, local_time="09:00",
    )
    afternoon = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 20)
    await trigger_service.process_due_occurrences(NOW)  # only 9am due -- executes for real

    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    before = await trigger_service.occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)

    await trigger_service.process_due_occurrences(NOW.replace(hour=18))
    after = await trigger_service.occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)
    assert after == before

    afternoon_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(14, 0), "UTC")
    afternoon_occ = await trigger_service.occurrence_store.get_occurrence(afternoon.trigger_id, afternoon_scheduled_for)
    assert afternoon_occ is not None and afternoon_occ.status == "COMPLETED"  # 2pm legitimately ran once due


# =====================================================================
# PREPARING recovery: frozen vs. unfrozen re-derivation, SUPERSEDED CAS
# (test list items 6-9)
# =====================================================================


async def test_crash_after_winner_creation_before_freeze_still_latest_resumes_normally(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store,
):
    """Test list item 6. Only ONE trigger is due (no contention) -- an
    occurrence row was created but the process crashed before freezing.
    Resuming must re-derive that it is still the correct (only) winner
    and execute it for real, never superseding it."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=scheduled_for, target_count=20, created_at=NOW,
        )
    )  # PREPARING, frozen_at=None -- crash between create_occurrence and freeze_members

    await trigger_service.process_due_occurrences(NOW)

    occ = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "COMPLETED"
    assert occ.started_count == 3


async def test_crash_after_9am_creation_before_freeze_then_2pm_becomes_due_9am_superseded_2pm_executes(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, activity_log,
):
    """Test list item 7. 9am's row was created (crash before freeze), and
    by the time recovery runs, 2pm has ALSO become due. 9am must be
    re-derived as no longer the winner and become SUPERSEDED (a real
    PREPARING->SUPERSEDED transition, WITH its own Activity Log event --
    unlike a never-created loser); 2pm then executes on a later tick."""
    active, _, morning = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=4, local_time="09:00", leads_to_start=20,
    )
    afternoon = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 20)

    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=morning.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=morning_scheduled_for, target_count=20, created_at=NOW,
        )
    )

    late_now = NOW.replace(hour=15)
    await trigger_service.process_due_occurrences(late_now)

    morning_occ = await occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)
    assert morning_occ.status == "SUPERSEDED"
    assert morning_occ.frozen_at is None
    assert await occurrence_store.list_members(morning.trigger_id, morning_scheduled_for) == []
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)

    page = await activity_log.list_events(page_size=50)
    assert "mail_trigger_occurrence.superseded" in [e.event_type for e in page.items]

    # A later tick discovers 2pm fresh and actually starts leads.
    await trigger_service.process_due_occurrences(late_now)
    afternoon_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(14, 0), "UTC")
    afternoon_occ = await occurrence_store.get_occurrence(afternoon.trigger_id, afternoon_scheduled_for)
    assert afternoon_occ.status == "COMPLETED" and afternoon_occ.started_count == 4


async def test_superseded_is_terminal_never_freezes_or_starts_leads_later(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
):
    """Test list item 8. Once SUPERSEDED, an occurrence never freezes
    members or starts leads on any subsequent tick, no matter how many
    more ticks run -- and process_due_occurrences() itself never revisits
    it (it isn't PREPARING, so list_preparing_occurrences_for_campaign
    never surfaces it again, and fresh discovery's own floor permanently
    excludes its trigger's identical schedule)."""
    active, _, morning = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, local_time="09:00", leads_to_start=20,
    )
    afternoon = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "14:00", 20)
    morning_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=morning.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=morning_scheduled_for, target_count=20, created_at=NOW,
        )
    )
    late_now = NOW.replace(hour=15)
    await trigger_service.process_due_occurrences(late_now)  # supersedes 9am, nothing else yet
    assert (await occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)).status == "SUPERSEDED"

    # Directly attempt to freeze/execute the SUPERSEDED row via the same
    # entry point a fresh winner would use -- must be a total no-op.
    await trigger_service._execute_occurrence(active, morning, morning_scheduled_for, late_now.replace(hour=20))
    occ = await occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)
    assert occ.status == "SUPERSEDED"
    assert occ.frozen_at is None
    assert await occurrence_store.list_members(morning.trigger_id, morning_scheduled_for) == []
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)

    # Many more ticks across the rest of the day: still never touched again.
    for hour in (16, 17, 18, 19, 20, 21, 22, 23):
        await trigger_service.process_due_occurrences(NOW.replace(hour=hour))
    assert (await occurrence_store.get_occurrence(morning.trigger_id, morning_scheduled_for)).status == "SUPERSEDED"


# =====================================================================
# Pause semantics (item 6)
# =====================================================================


async def test_paused_campaign_does_nothing_even_with_a_preparing_occurrence_outstanding(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=scheduled_for, target_count=20, created_at=NOW,
        )
    )
    paused = await campaign_service.pause_campaign(active.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED

    await trigger_service.process_due_occurrences(NOW)

    occ = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "PREPARING" and occ.frozen_at is None  # completely untouched
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


# =====================================================================
# Resume semantics + stale-PREPARING-occurrence interaction (item 7)
# =====================================================================


async def test_resume_finishes_a_stale_frozen_occurrence_even_though_it_predates_the_new_active_since(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
):
    """The behavior explicitly flagged in this stage's STOP report as a
    decision pending user confirmation, implemented per the reasoned
    recommendation: an occurrence that was already frozen (durably
    committed real candidates) before a pause must still be finished on
    resume, even though its own scheduled_for now predates the freshly-
    reset execution_active_since -- abandoning it would permanently
    strand its already-frozen enrollments (the global UNIQUE(enrollment_id)
    constraint means they can never join a different occurrence)."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    enrollment_ids = [e.enrollment_id for e in enrollments]

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=scheduled_for, target_count=20, created_at=NOW,
        )
    )
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, enrollment_ids, NOW)
    # Still PREPARING -- unreconciled -- simulating a crash after freeze, before reconciliation completed.

    # Pause, then resume with execution_active_since reset to well AFTER scheduled_for.
    await campaign_service.pause_campaign(active.mail_campaign_id)
    resumed = await campaign_service.resume_campaign(active.mail_campaign_id)
    new_active_since = scheduled_for + timedelta(days=1)
    resumed = resumed.model_copy(update={"execution_active_since": new_active_since})
    await campaign_store.save(resumed)
    assert new_active_since > scheduled_for

    later_now = new_active_since + timedelta(hours=1)
    await trigger_service.process_due_occurrences(later_now)

    occ = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "COMPLETED"
    assert occ.started_count == 3
    fresh = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in fresh)


async def test_resume_does_not_invent_a_brand_new_occurrence_for_a_time_before_the_new_active_since(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store,
):
    """Contrast case for the above: if NOTHING was ever frozen (no
    PREPARING row exists at all), resume must NOT retroactively create
    and run an occurrence whose scheduled_for predates the new
    execution_active_since -- only an already-durably-committed occurrence
    gets finished; a merely-missed one still follows the ordinary
    active-since boundary."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await campaign_service.pause_campaign(active.mail_campaign_id)
    resumed = await campaign_service.resume_campaign(active.mail_campaign_id)
    new_active_since = scheduled_for + timedelta(days=1)
    resumed = resumed.model_copy(update={"execution_active_since": new_active_since})
    await campaign_store.save(resumed)

    await trigger_service.process_due_occurrences(new_active_since + timedelta(hours=1))

    assert await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for) is None


async def test_unfrozen_occurrence_predating_new_active_streak_does_not_execute_after_resume(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
):
    """Test list item 11. An UNFROZEN PREPARING occurrence (nothing
    committed) does NOT get the frozen-recovery privilege: after a pause
    resets execution_active_since forward past it, resuming must
    re-derive it as no longer a valid current candidate (its own
    scheduled_for belongs to a day before "today") and supersede it,
    never freezing real candidates for a pre-streak day."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=scheduled_for, target_count=20, created_at=NOW,
        )
    )  # PREPARING, frozen_at=None -- crash before freeze, before the pause below

    await campaign_service.pause_campaign(active.mail_campaign_id)
    resumed = await campaign_service.resume_campaign(active.mail_campaign_id)
    new_active_since = scheduled_for + timedelta(days=1)
    resumed = resumed.model_copy(update={"execution_active_since": new_active_since})
    await campaign_store.save(resumed)

    await trigger_service.process_due_occurrences(new_active_since + timedelta(hours=1))

    occ = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "SUPERSEDED"
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


# =====================================================================
# Restart semantics -- no in-memory correctness dependency (item 9)
# =====================================================================


async def test_a_freshly_constructed_service_instance_correctly_resumes_state_left_by_another(
    trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
    sending_service, campaign_service, activity_log, crm,
):
    """No process-restart-unsafe state lives on MailTriggerService itself
    -- a brand-new instance sharing the same durable stores must resume
    exactly as the original instance would have, proving correctness
    depends only on store contents, never on any Python object identity
    or in-memory cache."""
    service_a = _make_trigger_service(
        trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
        sending_service, campaign_service, activity_log,
    )
    active, _, trigger = await _make_triggered_active_campaign(
        service_a, campaign_service, crm, campaign_store, n_contacts=2,
    )
    scheduled_for = MailTriggerService._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id,
            scheduled_for=scheduled_for, target_count=20, created_at=NOW,
        )
    )  # "service_a" leaves a PREPARING, unfrozen occurrence behind -- like a crash right before restart.

    service_b = _make_trigger_service(
        trigger_store, occurrence_store, campaign_store, enrollment_store, enrollment_step_store, suppression_store,
        sending_service, campaign_service, activity_log,
    )
    await service_b.process_due_occurrences(NOW)

    occ = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occ.status == "COMPLETED" and occ.started_count == 2


# =====================================================================
# Duplicate-schedule validation (item 10)
# =====================================================================


async def test_create_trigger_rejects_same_local_time_and_overlapping_weekday_when_both_enabled(
    trigger_service, campaign_service, crm,
):
    campaign = await campaign_service.create_campaign("Collision Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1, 2], "09:00", 10)
    with pytest.raises(ValueError):
        await trigger_service.create_trigger(campaign.mail_campaign_id, [2, 3], "09:00", 5)


async def test_create_trigger_allows_same_local_time_with_disjoint_weekdays(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("No Collision Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10)
    second = await trigger_service.create_trigger(campaign.mail_campaign_id, [2, 3], "09:00", 5)
    assert second is not None


async def test_create_trigger_allows_overlapping_weekday_with_a_different_local_time(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Different Time Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10)
    second = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "14:00", 5)
    assert second is not None


async def test_create_trigger_allows_collision_when_the_existing_trigger_is_disabled(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Disabled Sibling Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10, enabled=False)
    second = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 5)
    assert second is not None


async def test_creating_a_disabled_trigger_never_collides_with_anything(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Disabled New Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10)
    second = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 5, enabled=False)
    assert second is not None


async def test_update_trigger_rejects_collision_with_another_enabled_trigger(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Update Collision Campaign")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10)
    other = await trigger_service.create_trigger(campaign.mail_campaign_id, [2, 3], "14:00", 5)
    with pytest.raises(ValueError):
        await trigger_service.update_trigger(campaign.mail_campaign_id, other.trigger_id, weekdays=[0], local_time="09:00")


async def test_update_trigger_allows_a_no_op_update_of_its_own_existing_schedule(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Self Update Campaign")
    trigger = await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1], "09:00", 10)
    updated = await trigger_service.update_trigger(campaign.mail_campaign_id, trigger.trigger_id, leads_to_start=15)
    assert updated.leads_to_start == 15
    assert updated.local_time == trigger.local_time


# =====================================================================
# Trigger-edit / occurrence-immutability (item 11)
# =====================================================================


async def test_editing_a_triggers_schedule_does_not_mutate_an_already_created_occurrence(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, local_time="09:00",
    )
    old_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(9, 0), "UTC")
    await trigger_service.process_due_occurrences(NOW)
    old_occ_before_edit = await occurrence_store.get_occurrence(trigger.trigger_id, old_scheduled_for)
    assert old_occ_before_edit is not None and old_occ_before_edit.status == "COMPLETED"

    await trigger_service.update_trigger(active.mail_campaign_id, trigger.trigger_id, local_time="16:00")

    old_occ_after_edit = await occurrence_store.get_occurrence(trigger.trigger_id, old_scheduled_for)
    assert old_occ_after_edit == old_occ_before_edit  # untouched historical row

    new_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(16, 0), "UTC")
    assert await occurrence_store.get_occurrence(trigger.trigger_id, new_scheduled_for) is None  # not retroactively invented


# =====================================================================
# DST -- explicit V1 product policy (test list items 15-17)
# =====================================================================


def test_spring_forward_nonexistent_local_time_resolves_to_the_post_gap_real_instant():
    """Test list item 15. America/New_York, 2027-03-14 (clocks jump
    02:00->03:00): a Trigger configured for the nonexistent 02:30 must
    become due at the UTC instant that reads back as 03:30 EDT -- the
    real wall-clock moment "02:30" would have become if the clock had
    kept counting through the gap. This is the explicitly adopted V1
    product policy (see _scheduled_for's own docstring), verified here
    against exact expected values, not just "doesn't raise"."""
    nonexistent = MailTriggerService._scheduled_for(datetime(2027, 3, 14).date(), time(2, 30), "America/New_York")
    assert nonexistent == datetime(2027, 3, 14, 7, 30, tzinfo=timezone.utc)
    relocalized = nonexistent.astimezone(ZoneInfo("America/New_York"))
    assert relocalized == datetime(2027, 3, 14, 3, 30, tzinfo=ZoneInfo("America/New_York"))
    assert relocalized.utcoffset() == timedelta(hours=-4)  # EDT, confirming it landed AFTER the transition

    # Never skipped: a real, due-able occurrence is produced, not None/an exception.
    assert nonexistent is not None


def test_fall_back_ambiguous_local_time_selects_the_first_occurrence():
    """Test list item 16. America/New_York, 2027-11-07 (01:30 occurs
    twice): a Trigger configured for 01:30 must fire on the FIRST
    01:30 (still EDT, fold=0), not the repeated second one an hour later."""
    ambiguous = MailTriggerService._scheduled_for(datetime(2027, 11, 7).date(), time(1, 30), "America/New_York")
    assert ambiguous == datetime(2027, 11, 7, 5, 30, tzinfo=timezone.utc)
    relocalized = ambiguous.astimezone(ZoneInfo("America/New_York"))
    assert relocalized.utcoffset() == timedelta(hours=-4)  # EDT -- the FIRST 01:30, not the EST repeat


def test_scheduled_for_respects_dst_offset_change_across_the_transition():
    """A local_time before vs after the spring-forward transition must
    convert to UTC instants one hour apart, proving zoneinfo's own DST
    rules are actually in effect for ordinary (non-boundary) dates too."""
    before_dst = MailTriggerService._scheduled_for(datetime(2027, 3, 13).date(), time(9, 0), "America/New_York")
    after_dst = MailTriggerService._scheduled_for(datetime(2027, 3, 15).date(), time(9, 0), "America/New_York")
    assert (before_dst.hour - after_dst.hour) % 24 == 1  # EST (UTC-5) -> EDT (UTC-4)


# =====================================================================
# UTC / campaign-local-date boundary for the durable-history floor
# (test list item 17)
# =====================================================================


def test_local_day_bounds_utc_scopes_exactly_one_campaign_local_calendar_day():
    start_utc, end_utc = MailTriggerService._local_day_bounds_utc(datetime(2027, 3, 10).date(), "America/New_York")
    assert start_utc == datetime(2027, 3, 10, 5, 0, tzinfo=timezone.utc)  # midnight EST = 05:00 UTC
    assert end_utc == datetime(2027, 3, 11, 5, 0, tzinfo=timezone.utc)
    assert end_utc - start_utc == timedelta(hours=24)  # non-DST-transition day: exactly 24h


async def test_a_late_night_occurrence_does_not_block_the_next_calendar_days_trigger(
    trigger_service, campaign_service, crm, campaign_store, enrollment_store,
):
    """A trigger firing at 23:00 on day 1 must not affect a DIFFERENT
    trigger's 09:00 occurrence on day 2 -- the durable-history floor is
    scoped to exactly one campaign-local calendar day, never bleeding
    into the next."""
    active, _, late_trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, local_time="23:00", leads_to_start=1,
    )
    next_day_weekday = (NOW.weekday() + 1) % 7
    early_next_day_trigger = await trigger_service.create_trigger(
        active.mail_campaign_id, [next_day_weekday], "09:00", 1,
    )

    day1_late_now = NOW.replace(hour=23, minute=5)
    await trigger_service.process_due_occurrences(day1_late_now)
    late_scheduled_for = MailTriggerService._scheduled_for(NOW.date(), time(23, 0), "UTC")
    late_occ = await trigger_service.occurrence_store.get_occurrence(late_trigger.trigger_id, late_scheduled_for)
    assert late_occ.status == "COMPLETED" and late_occ.started_count == 1

    day2_now = day1_late_now + timedelta(hours=10)  # 09:05 the next UTC day
    await trigger_service.process_due_occurrences(day2_now)
    early_scheduled_for = MailTriggerService._scheduled_for(day2_now.date(), time(9, 0), "UTC")
    early_occ = await trigger_service.occurrence_store.get_occurrence(early_next_day_trigger.trigger_id, early_scheduled_for)
    assert early_occ is not None and early_occ.status == "COMPLETED" and early_occ.started_count == 1
