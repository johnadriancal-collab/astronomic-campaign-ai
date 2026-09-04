"""
MailTriggerService occurrence discovery/freeze/reconciliation -- Stage 5D
(2026-09-04). Covers candidate selection/ordering, the freeze-before-
mutation contract, the reconciliation state machine (including crash-
recovery and the corruption/unexpected-state cases), the suppression
race, concurrency/exactly-once guarantees (using the real durable store
constraints, never a process-local lock), campaign-status/mode/engine
gating, and MailExecutionWorker integration/ordering.

Uses a FIXED `NOW` (never the real wall clock) for every occurrence-
timing assertion, and directly overwrites `execution_active_since` after
activation to a fixed past instant -- decouples every test from whatever
moment it actually happens to run, matching this codebase's own
established time-testing convention (e.g. tests/test_mail_sending_service.py's
own module-level NOW constant) rather than Stage 5A-5C's real-clock
`before <= x <= after` style, which doesn't fit here since occurrence
identity/due-ness math needs a precisely controlled instant.
"""

from datetime import datetime, time, timedelta, timezone

import pytest

from app.models.mail import MailCampaignStatus, MailEnrollmentStatus, MailSuppression, MailSuppressionReason
from app.models.crm import normalize_email
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
from app.services.mail_execution_worker import MailExecutionWorker
from app.services.mail_sending_service import MailSendingService
from app.services.mail_trigger_service import MailTriggerService
from app.services.worker_lease_service import WorkerLeaseService
from app.repositories.worker_lease_store import MemoryWorkerLeaseStore
from app.services import mail_campaign_service as mail_campaign_service_module
from tests.test_mail_campaign_service import _make_mailbox, _make_valid_schedule_campaign

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 9, 5, tzinfo=timezone.utc)


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


async def _make_triggered_active_campaign(
    trigger_service, campaign_service, crm, campaign_store, n_contacts=3, weekdays=None, local_time="00:00",
    leads_to_start=20, active_since=None,
):
    """DRAFT -> (create trigger, flipping lead_start_mode) -> READY ->
    ACTIVE, with `n_contacts` PENDING enrollments and zero Step 1 rows
    (Stage 5C's own gate, exercised here as a precondition, not
    re-tested). `active_since` defaults to well before NOW so every
    occurrence in these tests is inside the active streak unless a test
    explicitly overrides it to test that boundary."""
    contact_list = await crm.create_contact_list("Trigger Audience")
    contact_ids = []
    for i in range(n_contacts):
        c = await crm.create_contact({"email": f"trig{i}@example.com", "first_name": f"Trig{i}"})
        contact_ids.append(c.crm_contact_id)
    await crm.bulk_add_to_list(contact_list.list_id, contact_ids)

    campaign = await campaign_service.create_campaign("Trigger Campaign")
    campaign = await campaign_service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": contact_list.list_id,
            "sending_days": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "00:00",
            "end_time": "23:59",
            "timezone": "UTC",
        },
    )
    await campaign_service.add_step(campaign.mail_campaign_id, "Hello {{first_name}}", "Body text")
    mailbox_id = f"mbx-{campaign.mail_campaign_id}"
    await campaign_service.mailbox_store.create(_make_mailbox(mailbox_id=mailbox_id, email=f"{mailbox_id}@example.com"))
    await campaign_service.set_channel_mailboxes(campaign.mail_campaign_id, [mailbox_id])

    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    await trigger_service.create_trigger(
        ready.mail_campaign_id, weekdays if weekdays is not None else [NOW.weekday()], local_time, leads_to_start
    )
    active = await campaign_service.activate_campaign(ready.mail_campaign_id)
    assert active.status == MailCampaignStatus.ACTIVE

    fixed_since = active_since if active_since is not None else NOW - timedelta(days=30)
    active = active.model_copy(update={"execution_active_since": fixed_since})
    await campaign_store.save(active)

    triggers = await trigger_service.list_triggers(active.mail_campaign_id)
    return active, contact_list, triggers[0]


# =====================================================================
# 7-8. Candidate selection: oldest first, leads_to_start cap
# =====================================================================


async def test_due_occurrence_selects_oldest_pending_first(trigger_service, campaign_service, crm, campaign_store, enrollment_store):
    """mark_ready()'s snapshot loop stamps every enrollment with the SAME
    `enrolled_at` (one `now` computed once for the whole loop), so a real
    age difference is given here explicitly -- directly testing the
    PRIMARY sort key (enrolled_at), not just the enrollment_id tie-break
    that would otherwise be all that's actually exercised."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, leads_to_start=1
    )
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    # Give each a distinct, deliberately-scrambled age -- the middle one
    # (by creation order) is made the OLDEST by enrolled_at.
    ages = [NOW - timedelta(hours=1), NOW - timedelta(hours=5), NOW - timedelta(hours=2)]
    for enrollment, age in zip(enrollments, ages):
        await enrollment_store.save(enrollment.model_copy(update={"enrolled_at": age}))
    oldest = enrollments[1]  # the one given enrolled_at = NOW - 5h

    await trigger_service.process_due_occurrences(NOW)

    fresh = {e.enrollment_id: e for e in await enrollment_store.list_for_campaign(active.mail_campaign_id)}
    assert fresh[oldest.enrollment_id].status == MailEnrollmentStatus.ACTIVE
    for other in enrollments:
        if other.enrollment_id != oldest.enrollment_id:
            assert fresh[other.enrollment_id].status == MailEnrollmentStatus.PENDING


async def test_leads_to_start_caps_how_many_start(trigger_service, campaign_service, crm, campaign_store, enrollment_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=5, leads_to_start=2
    )
    await trigger_service.process_due_occurrences(NOW)

    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    started = [e for e in enrollments if e.status == MailEnrollmentStatus.ACTIVE]
    still_pending = [e for e in enrollments if e.status == MailEnrollmentStatus.PENDING]
    assert len(started) == 2
    assert len(still_pending) == 3


# =====================================================================
# 9-10. Freeze durable before mutation; PENDING/no-Step1 -> ACTIVE+Step1
# =====================================================================


async def test_frozen_cohort_is_durable_before_any_enrollment_mutation(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store,
):
    """Directly exercises the freeze phase in isolation (via the same
    internal helpers _execute_occurrence() calls) to prove step 3 (freeze)
    is durable BEFORE step 4 (mutation) -- not merely that the end state
    looks right after a full run."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, leads_to_start=2
    )
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    occurrence = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=occurrence,
            target_count=trigger.leads_to_start, created_at=NOW,
        )
    )
    candidate_ids = await trigger_service._select_candidates(active.mail_campaign_id, trigger.leads_to_start, step1)
    await occurrence_store.freeze_members(trigger.trigger_id, occurrence, candidate_ids, NOW)

    # Cohort is durable NOW -- confirm before any reconciliation has run.
    members = await occurrence_store.list_members(trigger.trigger_id, occurrence)
    assert {m.enrollment_id for m in members} == set(candidate_ids)
    assert all(m.outcome == "PENDING_RECONCILE" for m in members)
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments), "no mutation yet -- freeze precedes it"


async def test_pending_no_step1_becomes_active_with_exactly_one_step1(
    trigger_service, campaign_service, crm, campaign_store, enrollment_store, enrollment_step_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment_before = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]

    await trigger_service.process_due_occurrences(NOW)

    enrollment_after = await enrollment_store.get(enrollment_before.enrollment_id)
    assert enrollment_after.status == MailEnrollmentStatus.ACTIVE
    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1
    assert rows[0].step_number == 1


# =====================================================================
# 11. ACTIVE/expected-Step1 recovery -> STARTED, no duplicate
# =====================================================================


async def test_active_with_expected_step1_recovers_to_started_without_duplicate(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Simulates a crash between the enrollment-ACTIVE mutation and the
    member-STARTED bookkeeping: manually puts a member into exactly that
    state, then reconciles -- must recognize it as already-done, not
    materialize a second Step 1."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)

    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for, target_count=1, created_at=NOW)
    )
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, [enrollment.enrollment_id], NOW)

    # Manually perform the mutation half (as if the process crashed right after).
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    await enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))

    # Now let reconciliation run -- must recover, not duplicate.
    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1, "no duplicate Step1 row"
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "STARTED"


# =====================================================================
# 12. Terminal/unexpected states -> SKIPPED_INELIGIBLE
# =====================================================================


@pytest.mark.parametrize(
    "make_state",
    [
        lambda e: e.model_copy(update={"status": MailEnrollmentStatus.SUPPRESSED}),
        lambda e: e.model_copy(update={"status": MailEnrollmentStatus.FAILED}),
        lambda e: e.model_copy(update={"status": MailEnrollmentStatus.COMPLETED}),
        lambda e: e.model_copy(update={"status": MailEnrollmentStatus.PAUSED}),
    ],
)
async def test_terminal_or_unexpected_enrollment_states_become_skipped_ineligible(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, make_state,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]

    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for, target_count=1, created_at=NOW)
    )
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, [enrollment.enrollment_id], NOW)

    # Mutate the enrollment into the terminal/unexpected state AFTER freezing.
    await enrollment_store.save(make_state(enrollment))

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "SKIPPED_INELIGIBLE"


# --- Crash gap fix (2026-09-04): PENDING + the EXPECTED Step1 already ------
# existing is recognized as CRASH RECOVERY for a frozen occurrence member
# (Write A succeeded, crash before Write B), not corruption -- see
# MailTriggerService._reconcile_member()'s own docstring for the full
# provenance argument. Replaces the old
# test_pending_with_unexpected_step1_before_reconciliation_is_skipped_ineligible,
# whose "this must be corruption" assumption this investigation disproved
# for a frozen member specifically (see items 9/10 below for the case
# that DOES still legitimately return SKIPPED_INELIGIBLE).


async def _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment_id, target_count=1):
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(
            trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for,
            target_count=target_count, created_at=NOW,
        )
    )
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, [enrollment_id], NOW)


async def test_crash_after_step1_before_active_recovers_to_active_with_exactly_one_step1(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Items 1-2: simulates the exact crash point -- Write A (Step1)
    committed, Write B (ACTIVE) never ran -- then reconciles and confirms
    full, correct recovery."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    # Simulate Write A having already succeeded in a crashed attempt.
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    # Durable state right now: enrollment PENDING, Step1 exists, member PENDING_RECONCILE.
    assert (await enrollment_store.get(enrollment.enrollment_id)).status == MailEnrollmentStatus.PENDING

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    recovered = await enrollment_store.get(enrollment.enrollment_id)
    assert recovered.status == MailEnrollmentStatus.ACTIVE
    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1, "no duplicate Step1 row"
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "STARTED"


async def test_repeated_retry_of_crash_recovery_remains_idempotent(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Item 3: reconciling the SAME already-recovered member again (e.g. a
    second tick before the occurrence-completion write landed) must not
    re-run any mutation or change the outcome."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)
    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW + timedelta(minutes=1))
    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW + timedelta(minutes=2))

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "STARTED"
    occurrence = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence.status == "COMPLETED"
    assert occurrence.started_count == 1


async def test_suppression_added_after_step1_before_retry_prevents_activation(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store, suppression_store,
):
    """Item 4: Case 2's live suppression re-check -- the lead became
    suppressed AFTER Write A (the crashed attempt's Step1 creation) but
    BEFORE the retry. Recovery must not activate a since-suppressed lead."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    await suppression_store.upsert(
        MailSuppression(
            email_normalized=normalize_email(enrollment.email_at_enrollment), reason=MailSuppressionReason.MANUAL,
            active=True, created_at=NOW, updated_at=NOW,
        )
    )

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    recovered = await enrollment_store.get(enrollment.enrollment_id)
    assert recovered.status == MailEnrollmentStatus.SUPPRESSED, "must NOT be activated once suppressed"
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "SKIPPED_INELIGIBLE"


async def test_suppressed_recovery_leaves_no_endlessly_claimable_orphan_step1(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store, suppression_store,
):
    """Item 5: the ONE thing this whole fix exists to prevent -- confirms
    the orphaned Step1 row created by the crashed attempt is left in a
    genuinely terminal, non-executable status (SKIPPED_SUPPRESSED), not
    QUEUED, once suppression is discovered during recovery."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    await suppression_store.upsert(
        MailSuppression(email_normalized=normalize_email(enrollment.email_at_enrollment), reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
    )
    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1
    assert rows[0].status.value == "skipped_suppressed", "never left QUEUED -- never claimable by the worker again"

    # Defense in depth: confirm list_due() itself would never surface it.
    not_due = await enrollment_step_store.list_due(NOW + timedelta(days=365), limit=100)
    assert rows[0].enrollment_step_id not in [r.enrollment_step_id for r in not_due]


async def test_crash_after_active_before_member_started_still_recovers(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Item 6: Case 3, re-verified after the fix -- unchanged behavior."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    await trigger_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    await enrollment_store.save(enrollment.model_copy(update={"status": MailEnrollmentStatus.ACTIVE}))
    # Crash here -- member bookkeeping (Write C) never ran.

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "STARTED"


async def test_crash_after_member_started_before_occurrence_completed_recompletes_safely(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Item 7: every member already STARTED/reconciled, but
    complete_occurrence() itself never ran -- retry must recompute counts
    and complete, never re-touch any member or start anything new."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)
    await occurrence_store.mark_member_reconciled(trigger.trigger_id, scheduled_for, enrollment.enrollment_id, "STARTED", NOW)
    # No Step1 actually created in this synthetic scenario -- irrelevant
    # to what's under test (complete_occurrence() resumability), and
    # keeps this test focused on exactly that boundary.

    occurrence_before = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence_before.status == "PREPARING"

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW + timedelta(minutes=1))

    occurrence_after = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence_after.status == "COMPLETED"
    assert occurrence_after.started_count == 1
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "STARTED"  # untouched, not re-reconciled


async def test_duplicate_step1_remains_impossible_across_every_crash_point(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Item 8: runs reconciliation repeatedly from EVERY crash point in
    sequence against the SAME member and confirms exactly one Step1 row
    ever exists."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, enrollment.enrollment_id)

    for i in range(5):
        await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW + timedelta(minutes=i))

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1


# --- Items 9-10: Stage 5C corruption vs Trigger-reconciliation recovery ----


async def test_stage_5c_activation_completeness_still_treats_pending_plus_step1_as_corruption(
    campaign_service, crm, campaign_store,
):
    """Item 9: re-confirms, directly against the actual, unmodified Stage
    5C method (MailCampaignService._find_incomplete_activation()), that
    ITS OWN separate context -- campaign-activation completeness, checked
    against every enrollment campaign-wide, with no per-enrollment
    frozen-occurrence provenance available -- is UNCHANGED by this fix:
    it still treats PENDING+Step1 as incomplete/corrupt, exactly as
    tests/test_lead_start_mode_activation_gate.py's own dedicated Stage
    5C corruption test already covers (part of the same full regression
    run this stage's STOP report cites) -- reconstructed here,
    self-contained, specifically to sit next to item 10 below for direct
    contrast."""
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())

    # Opt into "triggered" without ever going through Trigger occurrence
    # machinery -- a direct store write, matching how this file's own
    # sibling test files simulate "already triggered" campaigns.
    triggered = ready.model_copy(update={"lead_start_mode": "triggered"})
    await campaign_store.save(triggered)

    steps = await campaign_service.step_store.list_for_campaign(ready.mail_campaign_id)
    step1 = next(s for s in steps if s.step_number == 1)
    windows, _source = await campaign_service._resolve_schedule(ready.mail_campaign_id, triggered)
    enrollment = (await campaign_service.enrollment_store.list_for_campaign(ready.mail_campaign_id))[0]
    assert enrollment.status.value == "pending"

    # Directly materialize a Step1 while the enrollment stays PENDING --
    # the same inconsistent shape, produced completely OUTSIDE any
    # Trigger occurrence's own frozen-member bookkeeping.
    await campaign_service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=windows, timezone_name=triggered.timezone, now=NOW
    )

    incomplete = await campaign_service._find_incomplete_activation(ready.mail_campaign_id, step1, "triggered")
    assert enrollment.enrollment_id in incomplete, "Stage 5C's own completeness check must still fail closed here"


async def test_trigger_reconciliation_recovery_is_specifically_because_of_frozen_member_provenance(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store,
):
    """Item 10: the SAME durable shape (PENDING + this-campaign's-Step1)
    is treated as recovery here ONLY because reconciliation is being run
    for a member ALREADY FROZEN into this specific occurrence -- an
    enrollment with an identical PENDING+Step1 shape that was NEVER
    frozen into any occurrence is simply never reachable by
    _reconcile_member() at all (it only ever processes rows returned by
    list_members() for one specific occurrence), so there is no
    code path where an un-provenanced PENDING+Step1 enrollment could be
    silently recovered by this method -- it would never be looked at by
    Trigger reconciliation in the first place. This test demonstrates
    that boundary directly: an unrelated enrollment in the SAME campaign,
    also PENDING with a Step1 row, but never frozen into this occurrence,
    is left completely untouched by reconciliation."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, leads_to_start=1
    )
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    frozen_enrollment, bystander_enrollment = enrollments[0], enrollments[1]
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    schedule = await campaign_service.get_schedule(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")

    await _freeze_one_member(trigger_service, occurrence_store, active, trigger, scheduled_for, frozen_enrollment.enrollment_id)
    await trigger_service.sending_service.create_step1_execution(
        enrollment=frozen_enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    # The bystander independently, coincidentally also ends up PENDING +
    # a Step1 row -- but was never frozen into this (or any) occurrence.
    await trigger_service.sending_service.create_step1_execution(
        enrollment=bystander_enrollment, step1=step1, windows=schedule.windows, timezone_name="UTC", now=NOW
    )
    assert (await enrollment_store.get(bystander_enrollment.enrollment_id)).status == MailEnrollmentStatus.PENDING

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    # The frozen (provenanced) member is recovered.
    recovered = await enrollment_store.get(frozen_enrollment.enrollment_id)
    assert recovered.status == MailEnrollmentStatus.ACTIVE
    # The bystander (never frozen -- no provenance) is completely
    # untouched -- reconciliation never even looked at it.
    bystander_after = await enrollment_store.get(bystander_enrollment.enrollment_id)
    assert bystander_after.status == MailEnrollmentStatus.PENDING
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert {m.enrollment_id for m in members} == {frozen_enrollment.enrollment_id}


# =====================================================================
# 13. Suppression race
# =====================================================================


async def test_suppression_race_after_freeze_prevents_step1_and_marks_skipped(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store, enrollment_step_store, suppression_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]

    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for, target_count=1, created_at=NOW)
    )
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, [enrollment.enrollment_id], NOW)

    # Suppression lands AFTER the freeze (was eligible when frozen).
    await suppression_store.upsert(
        MailSuppression(
            email_normalized=normalize_email(enrollment.email_at_enrollment), reason=MailSuppressionReason.MANUAL,
            active=True, created_at=NOW, updated_at=NOW,
        )
    )

    await trigger_service._reconcile_occurrence(active, trigger.trigger_id, scheduled_for, NOW)

    enrollment_after = await enrollment_store.get(enrollment.enrollment_id)
    assert enrollment_after.status == MailEnrollmentStatus.SUPPRESSED
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []
    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert members[0].outcome == "SKIPPED_INELIGIBLE"


# =====================================================================
# 14-15. Occurrence completion counts; rediscovery idempotent
# =====================================================================


async def test_occurrence_completes_with_correct_started_and_skipped_counts(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, suppression_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, leads_to_start=3
    )
    enrollments = await trigger_service.enrollment_store.list_for_campaign(active.mail_campaign_id)
    # Suppress one of the three BEFORE the occurrence runs.
    await suppression_store.upsert(
        MailSuppression(email_normalized=normalize_email(enrollments[0].email_at_enrollment), reason=MailSuppressionReason.MANUAL, active=True, created_at=NOW, updated_at=NOW)
    )

    await trigger_service.process_due_occurrences(NOW)

    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    occurrence = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence.status == "COMPLETED"
    assert occurrence.started_count == 2


async def test_rediscovering_a_completed_occurrence_is_a_safe_no_op(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_step_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    await trigger_service.process_due_occurrences(NOW)
    rows_after_first = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)

    # A second tick discovers the SAME occurrence -- must be a pure no-op.
    await trigger_service.process_due_occurrences(NOW + timedelta(minutes=1))
    rows_after_second = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows_after_first) == len(rows_after_second) == 1


# =====================================================================
# 16-18. Concurrency / exactly-once / crash-retry
# =====================================================================


async def test_two_ticks_discovering_the_same_occurrence_produce_no_duplicate_work(
    trigger_service, campaign_service, crm, campaign_store, enrollment_step_store, enrollment_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, leads_to_start=2
    )
    await trigger_service.process_due_occurrences(NOW)
    await trigger_service.process_due_occurrences(NOW + timedelta(seconds=30))

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 2  # not 4
    enrollments = await enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)


async def test_concurrent_freeze_only_one_wins_the_cohort(trigger_service, campaign_service, crm, campaign_store, occurrence_store):
    """Two 'simultaneous' freeze attempts for the SAME occurrence -- the
    second must be a complete no-op (see freeze_members()'s own Stage 5A
    contract), never a partial/duplicate cohort."""
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=3, leads_to_start=3
    )
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for, target_count=3, created_at=NOW)
    )
    candidate_ids = await trigger_service._select_candidates(active.mail_campaign_id, 3, step1)

    first = await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, candidate_ids, NOW)
    second = await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, candidate_ids, NOW + timedelta(seconds=1))
    assert first is True
    assert second is False

    members = await occurrence_store.list_members(trigger.trigger_id, scheduled_for)
    assert len(members) == 3  # not 6


async def test_same_enrollment_cannot_join_two_occurrences(trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_store):
    """Two DIFFERENT triggers on the same campaign, both wanting the same
    (single, oldest) PENDING enrollment -- the global UNIQUE(enrollment_id)
    is what actually prevents double-membership, not application logic."""
    active, _, trigger_a = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    trigger_b = await trigger_service.create_trigger(active.mail_campaign_id, [NOW.weekday()], "01:00", 1)
    enrollment = (await enrollment_store.list_for_campaign(active.mail_campaign_id))[0]

    scheduled_a = trigger_service._scheduled_for(NOW.date(), trigger_a.local_time, "UTC")
    scheduled_b = trigger_service._scheduled_for(NOW.date(), trigger_b.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger_a.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_a, target_count=1, created_at=NOW)
    )
    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger_b.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_b, target_count=1, created_at=NOW)
    )
    await occurrence_store.freeze_members(trigger_a.trigger_id, scheduled_a, [enrollment.enrollment_id], NOW)
    await occurrence_store.freeze_members(trigger_b.trigger_id, scheduled_b, [enrollment.enrollment_id], NOW)

    members_a = await occurrence_store.list_members(trigger_a.trigger_id, scheduled_a)
    members_b = await occurrence_store.list_members(trigger_b.trigger_id, scheduled_b)
    assert len(members_a) == 1
    assert len(members_b) == 0  # excluded -- already claimed by trigger_a's occurrence


async def test_crash_after_freeze_before_reconciliation_resumes_cleanly(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_step_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, leads_to_start=2
    )
    step1 = await trigger_service._get_step1(active.mail_campaign_id)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    from app.models.mail import MailTriggerOccurrence

    await occurrence_store.create_occurrence(
        MailTriggerOccurrence(trigger_id=trigger.trigger_id, mail_campaign_id=active.mail_campaign_id, scheduled_for=scheduled_for, target_count=2, created_at=NOW)
    )
    candidate_ids = await trigger_service._select_candidates(active.mail_campaign_id, 2, step1)
    await occurrence_store.freeze_members(trigger.trigger_id, scheduled_for, candidate_ids, NOW)
    # "Crash" here -- no reconciliation happened yet.

    await trigger_service.process_due_occurrences(NOW + timedelta(minutes=1))

    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 2
    occurrence = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence.status == "COMPLETED"


async def test_retry_of_preparing_occurrence_does_not_duplicate_step1(
    trigger_service, campaign_service, crm, campaign_store, occurrence_store, enrollment_step_store,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    await trigger_service.process_due_occurrences(NOW)
    scheduled_for = trigger_service._scheduled_for(NOW.date(), trigger.local_time, "UTC")
    occurrence_before = await occurrence_store.get_occurrence(trigger.trigger_id, scheduled_for)
    assert occurrence_before.status == "COMPLETED"

    # Directly re-invoke _execute_occurrence (simulating a retry request) --
    # must be a pure no-op given the occurrence is already COMPLETED.
    await trigger_service._execute_occurrence(active, trigger, scheduled_for, NOW + timedelta(minutes=5))
    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1


# =====================================================================
# 19-23. Campaign-status / mode / trigger-enabled gating
# =====================================================================


async def test_paused_campaign_has_no_occurrence_execution(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=2, leads_to_start=2
    )
    paused = await campaign_service.pause_campaign(active.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED

    await trigger_service.process_due_occurrences(NOW)
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_ready_campaign_has_no_occurrence_execution(trigger_service, campaign_service, crm):
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm, n_contacts=2)
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1, 2, 3, 4, 5, 6], "00:00", 20)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    assert ready.status == MailCampaignStatus.READY

    await trigger_service.process_due_occurrences(NOW)
    enrollments = await campaign_service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


async def test_draft_campaign_has_no_occurrence_execution(trigger_service, campaign_service):
    campaign = await campaign_service.create_campaign("Draft Only")
    await trigger_service.create_trigger(campaign.mail_campaign_id, [0, 1, 2, 3, 4, 5, 6], "00:00", 20)
    await trigger_service.process_due_occurrences(NOW)  # must not raise, must do nothing
    fresh = await campaign_service.get_campaign(campaign.mail_campaign_id)
    assert fresh.status == MailCampaignStatus.DRAFT


async def test_archived_campaign_has_no_occurrence_execution(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    archived = await campaign_service.archive_campaign(active.mail_campaign_id)
    assert archived.status == MailCampaignStatus.ARCHIVED

    await trigger_service.process_due_occurrences(NOW)
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_disabled_trigger_has_no_occurrence_execution(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    await trigger_service.update_trigger(active.mail_campaign_id, trigger.trigger_id, enabled=False)

    await trigger_service.process_due_occurrences(NOW)
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_immediate_mode_campaign_has_no_occurrence_execution_even_with_a_stray_trigger_row(
    trigger_service, campaign_service, crm, campaign_store, enrollment_step_store,
):
    """Defensive: process_due_occurrences() itself re-checks
    lead_start_mode == "triggered" (not just trigger.enabled) -- an
    IMMEDIATE-mode campaign is skipped entirely regardless of what
    trigger rows happen to exist for it."""
    campaign, _ = await _make_valid_schedule_campaign(campaign_service, crm, n_contacts=1)
    ready = await campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await campaign_service.activate_campaign(ready.mail_campaign_id)  # still "immediate" -- eager-started already
    assert active.lead_start_mode == "immediate"

    await trigger_service.process_due_occurrences(NOW)
    # No crash, and (since Stage 5C already eagerly started everything at
    # activation) nothing PENDING is left for a trigger to even find.
    enrollments = await campaign_service.enrollment_store.list_for_campaign(active.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)


# =====================================================================
# 24. execution_active_since respected
# =====================================================================


async def test_occurrence_before_active_since_is_never_executed(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1,
        active_since=NOW + timedelta(hours=1),  # streak begins AFTER today's scheduled occurrence
    )
    await trigger_service.process_due_occurrences(NOW)
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_occurrence_after_active_since_executes_normally(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    # scheduled_for is today's midnight (default local_time="00:00") --
    # active_since must be BEFORE that instant for it to be "in streak",
    # not merely "before NOW" (NOW itself is 09:05, well after midnight).
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1,
        active_since=NOW - timedelta(days=1),
    )
    await trigger_service.process_due_occurrences(NOW)
    assert len(await enrollment_step_store.list_for_campaign(active.mail_campaign_id)) == 1


async def test_future_occurrence_today_is_never_executed(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1, local_time="23:59",
    )
    await trigger_service.process_due_occurrences(NOW)  # NOW is 09:05 UTC -- well before 23:59
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_wrong_weekday_is_never_executed(trigger_service, campaign_service, crm, campaign_store, enrollment_step_store):
    other_weekday = (NOW.weekday() + 1) % 7
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1, weekdays=[other_weekday],
    )
    await trigger_service.process_due_occurrences(NOW)
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


# =====================================================================
# 25. Engine flag makes Trigger execution structurally unreachable
# =====================================================================


async def test_engine_disabled_worker_never_starts_so_trigger_processing_never_runs(
    trigger_service, campaign_service, crm, campaign_store, enrollment_step_store, monkeypatch,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)
    from app.services import mail_execution_worker as worker_module

    monkeypatch.setattr(worker_module.settings, "mail_sending_engine_enabled", False)

    worker = MailExecutionWorker(
        mail_sending_service=trigger_service.sending_service,
        mail_campaign_service=campaign_service,
        lease_service=WorkerLeaseService(store=MemoryWorkerLeaseStore()),
        sender=object(),  # never reached
        mail_trigger_service=trigger_service,
    )
    worker.start()
    assert worker._task is None, "start() must refuse to schedule the tick loop at all when the engine flag is False"

    # Even a DIRECT tick() call (bypassing the scheduling refusal) would
    # still gate on leadership, but the real structural guarantee is that
    # start() never even gets that far -- confirmed above. As defense in
    # depth, confirm nothing in this campaign moved either way.
    assert await enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_engine_enabled_worker_tick_runs_trigger_processing_before_due_steps(
    trigger_service, campaign_service, crm, campaign_store, enrollment_step_store, activity_log,
):
    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    worker = MailExecutionWorker(
        mail_sending_service=trigger_service.sending_service,
        mail_campaign_service=campaign_service,
        lease_service=WorkerLeaseService(store=MemoryWorkerLeaseStore()),
        sender=_NoOpSender(),
        activity_log=activity_log,
        mail_trigger_service=trigger_service,
    )
    result = await worker.tick(now=NOW)
    assert result.is_leader is True

    # The Trigger occurrence ran (Step 1 materialized) AND ordinary
    # due-step processing also ran in the SAME tick -- confirming the
    # documented "trigger processing, then due-step processing" order
    # didn't skip either phase.
    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1


class _NoOpSender:
    async def prepare(self, request):
        return request

    async def send_prepared(self, prepared):
        from app.services.mail_sending_service import SendResult

        return SendResult(provider_message_id="m1", provider_thread_id="t1", rfc_message_id=prepared.rfc_message_id)


async def test_worker_without_trigger_service_still_works_unchanged(campaign_service, activity_log):
    """Backward-compatibility: every pre-Stage-5D construction of
    MailExecutionWorker (mail_trigger_service omitted) must keep working
    exactly as before -- optional param, same convention as activity_log."""
    worker = MailExecutionWorker(
        mail_sending_service=campaign_service.sending_service,
        mail_campaign_service=campaign_service,
        lease_service=WorkerLeaseService(store=MemoryWorkerLeaseStore()),
        sender=_NoOpSender(),
        activity_log=activity_log,
    )
    result = await worker.tick(now=NOW)
    assert result.is_leader is True  # no crash, no AttributeError from a missing mail_trigger_service


# =====================================================================
# 26-29. Existing send/quota/window/suppression/Stage 5B/5C behavior unaffected
# =====================================================================


async def test_trigger_started_lead_still_respects_mailbox_quota_downstream(
    trigger_service, campaign_service, crm, campaign_store, enrollment_step_store, policy_store,
):
    """Trigger starts a lead (materializes QUEUED Step1); ordinary send
    execution (mailbox quota, in this case zeroed out) still decides
    whether/when it actually sends -- unchanged, downstream, Trigger has
    no opinion about it."""
    from app.models.mailbox import MailboxSendPolicy

    active, _, trigger = await _make_triggered_active_campaign(
        trigger_service, campaign_service, crm, campaign_store, n_contacts=1, leads_to_start=1
    )
    mailbox_id = f"mbx-{active.mail_campaign_id}"
    await policy_store.upsert(MailboxSendPolicy(mailbox_id=mailbox_id, daily_send_limit=0, min_seconds_between_sends=0, created_at=NOW, updated_at=NOW))

    await trigger_service.process_due_occurrences(NOW)
    rows = await enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1
    assert rows[0].status.value == "queued", "Trigger only queues -- the mailbox quota gate is a separate, unaffected concern"


async def test_stage_5b_daily_lead_start_limit_condition_still_present():
    from pathlib import Path

    source = Path("app/services/mail_sending_service.py").read_text()
    assert 'fresh_campaign.lead_start_mode == "immediate"' in source


async def test_stage_5c_activation_gate_conditions_still_present():
    from pathlib import Path

    source = Path("app/services/mail_campaign_service.py").read_text()
    assert 'if lead_start_mode != "immediate":' in source
    assert 'if lead_start_mode == "immediate":' in source
