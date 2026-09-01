"""
Fault-injection tests for Astronomic Mail Phase A's two multi-write
operations that this codebase cannot make truly atomic at the DB level
(there is no cross-store transaction anywhere in this app -- see
sqlite_txn.py's docstring): MailCampaignService.activate_campaign() and
MailSendingService.record_send_success().

Each test deliberately makes a wrapped store raise partway through a
multi-enrollment/multi-write operation, then asserts on the EXACT
intermediate state left behind, and (where a fix was needed) that recovery
is real and idempotent -- not just asserted, exercised.
"""

from datetime import datetime, time, timezone

import pytest

from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentStatus,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
)
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService, SendResult

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


class BoomError(Exception):
    """Deliberately distinct from any real exception this app raises, so a
    test assertion on `pytest.raises(BoomError)` can never accidentally
    pass because of an unrelated bug."""


class FlakyEnrollmentStepStore(MemoryMailEnrollmentStepStore):
    """Wraps the real in-memory store, raising BoomError on the Nth call
    to create() -- used to simulate a crash partway through a multi-
    enrollment loop (activate_campaign()) or partway through materializing
    a follow-up step (record_send_success())."""

    def __init__(self, fail_on_call_number: int):
        super().__init__()
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    async def create(self, step):
        self._call_count += 1
        if self._call_count == self._fail_on_call_number:
            raise BoomError(f"simulated failure on create() call #{self._call_count}")
        return await super().create(step)


class FlakyEnrollmentStore(MemoryMailEnrollmentStore):
    """Wraps the real in-memory store, raising BoomError on the Nth call to
    save() -- used to simulate activate_campaign()'s enrollment-flip write
    failing partway through the loop."""

    def __init__(self, fail_on_call_number: int):
        super().__init__()
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    async def save(self, enrollment):
        self._call_count += 1
        if self._call_count == self._fail_on_call_number:
            raise BoomError(f"simulated failure on save() call #{self._call_count}")
        return await super().save(enrollment)


class FlakyCampaignStore(MemoryMailCampaignStore):
    """Wraps the real in-memory store, raising BoomError on the Nth call to
    save() -- used to simulate the COMMIT step itself failing (the
    READY->ACTIVE campaign_store.save() call) AFTER every enrollment in
    PREPARE has already succeeded -- the "100/100, fails before the status
    transition" scenario."""

    def __init__(self, fail_on_call_number: int):
        super().__init__()
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    async def save(self, campaign):
        self._call_count += 1
        if self._call_count == self._fail_on_call_number:
            raise BoomError(f"simulated failure on save() call #{self._call_count}")
        return await super().save(campaign)


def all_day_windows(mail_campaign_id="c1") -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{d}", mail_campaign_id=mail_campaign_id, day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


# --- Item 4: activate_campaign() partial-failure resumability --------------


@pytest.fixture(autouse=True)
def mail_sending_engine_enabled(monkeypatch):
    """These tests exercise activate_campaign()'s own multi-write
    resumability, not the deployment-wide mail_sending_engine_enabled gate
    (see app/config.py) -- that gate is tested separately in
    tests/test_mail_campaign_service.py."""
    from app.services import mail_campaign_service as mail_campaign_service_module

    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", True)


@pytest.fixture
def crm():
    return CrmService()


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


async def _build_ready_campaign_with_flaky_step_store(
    crm, activity_log, n_contacts=5, fail_on_call_number=3, campaign_store=None
):
    """Mirrors _make_valid_schedule_campaign()'s shape (tests/
    test_mail_campaign_service.py) but wires a FlakyEnrollmentStepStore in
    place of the normal one, so the Nth create_step1_execution() call
    inside activate_campaign()'s PREPARE loop raises. An optional
    `campaign_store` override (e.g. FlakyCampaignStore) lets a test inject
    a failure at the COMMIT step instead -- passing None uses a normal,
    non-flaky MemoryMailCampaignStore."""
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    window_store = MemoryMailSendWindowStore_stub()
    campaign_store = campaign_store if campaign_store is not None else MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailSequenceStepStore()
    flaky_enrollment_step_store = FlakyEnrollmentStepStore(fail_on_call_number=fail_on_call_number)

    sending_service = MailSendingService(
        campaign_store=campaign_store,
        enrollment_store=enrollment_store,
        step_store=flaky_enrollment_step_store,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )
    service = MailCampaignService(
        campaign_store=campaign_store,
        step_store=step_store,
        enrollment_store=enrollment_store,
        crm_service=crm,
        activity_log=activity_log,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        window_store=window_store,
        enrollment_step_store=flaky_enrollment_step_store,
        sending_service=sending_service,
    )

    contact_list = await crm.create_contact_list("Fault Injection Audience")
    contact_ids = []
    for i in range(n_contacts):
        c = await crm.create_contact({"email": f"person{i}@example.com", "first_name": f"Person{i}"})
        contact_ids.append(c.crm_contact_id)
    await crm.bulk_add_to_list(contact_list.list_id, contact_ids)

    campaign = await service.create_campaign("Fault Injection Campaign")
    campaign = await service.update_campaign(
        campaign.mail_campaign_id,
        {
            "source_list_id": contact_list.list_id,
            "sending_days": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "00:00",
            "end_time": "23:59",
            "timezone": TZ,
        },
    )
    await service.add_step(campaign.mail_campaign_id, "Hello {{first_name}}", "Body text")

    mailbox_id = "mbx-fault-injection"
    await mailbox_store.create(_make_mailbox(mailbox_id))
    await service.set_channel_mailboxes(campaign.mail_campaign_id, [mailbox_id])

    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    return service, ready, flaky_enrollment_step_store


def _make_mailbox(mailbox_id):
    from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus

    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=MailboxStatus.CONNECTED, google_user_id=f"g-{mailbox_id}",
        connected_at=NOW, updated_at=NOW,
    )


class MemoryMailSendWindowStore_stub:
    """A minimal in-memory MailSendWindowStore -- avoids importing the real
    one just to keep this file self-contained; matches its two-method ABC
    exactly."""

    def __init__(self):
        self._rows: dict[str, list] = {}

    async def list_for_campaign(self, mail_campaign_id: str):
        return self._rows.get(mail_campaign_id, [])

    async def replace_for_campaign(self, mail_campaign_id: str, windows) -> None:
        self._rows[mail_campaign_id] = list(windows)


async def test_activation_partial_failure_leaves_campaign_ready_not_active(crm, activity_log):
    """5 enrollments, injected failure on the 3rd Step-1 row creation.
    Activation must raise, the campaign must remain READY (never flips to
    ACTIVE with only partial execution rows), and exactly the enrollments
    processed before the failure are ACTIVE with a materialized Step 1 --
    the rest are untouched, still PENDING."""
    service, ready, flaky_store = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=5, fail_on_call_number=3
    )

    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    campaign_after = await service.get_campaign(ready.mail_campaign_id)
    assert campaign_after.status == MailCampaignStatus.READY, "must NOT be ACTIVE after a partial failure"

    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    active = [e for e in enrollments if e.status == MailEnrollmentStatus.ACTIVE]
    pending = [e for e in enrollments if e.status == MailEnrollmentStatus.PENDING]
    assert len(active) == 2, "exactly the 2 enrollments processed before the 3rd (failing) call"
    assert len(pending) == 3, "the rest must remain untouched, still PENDING"

    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 2, "no orphan/partial Step-1 row for the failed enrollment or any after it"


async def test_activation_retry_after_partial_failure_fully_and_correctly_heals(crm, activity_log):
    """The exact same scenario as above, but this time the SECOND
    activate_campaign() call (after the fault is cleared) must reach a
    fully correct final state: every enrollment ACTIVE, exactly one Step-1
    row each (no duplicates for the 2 that already succeeded), campaign
    ACTIVE. This is the concrete proof that "no true DB transaction, but
    retry-idempotent by construction" is actually true, not just claimed."""
    service, ready, flaky_store = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=5, fail_on_call_number=3
    )

    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    # "Fix the outage" -- the store no longer fails (call count already
    # passed the trigger point, so this call number will never fire again).
    activated = await service.activate_campaign(ready.mail_campaign_id)

    assert activated.status == MailCampaignStatus.ACTIVE
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)
    assert len(enrollments) == 5

    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 5, "exactly one Step-1 row per enrollment -- no duplicates from the retried loop"
    per_enrollment = {}
    for r in rows:
        per_enrollment.setdefault(r.enrollment_id, 0)
        per_enrollment[r.enrollment_id] += 1
    assert all(count == 1 for count in per_enrollment.values()), "no enrollment got a duplicate Step-1 row"


async def test_activation_partial_failure_campaign_cannot_send_while_stuck_in_limbo(crm, activity_log):
    """Even in the partial-failure intermediate state (campaign READY, but
    some enrollments already ACTIVE with a QUEUED Step-1 row), nothing can
    process those rows: MailSendingService.process_one_due_step()'s very
    first check is campaign.status == ACTIVE, which a READY campaign never
    satisfies. This is what makes the intermediate state SAFE, not just
    resumable."""
    service, ready, flaky_store = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=5, fail_on_call_number=3
    )
    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 2
    due_row = rows[0]
    assert due_row.status == MailEnrollmentStepStatus.QUEUED

    from tests.test_mail_sending_service import FakeMailSender

    outcome = await service.sending_service.process_one_due_step(
        due_row, sender=FakeMailSender(), claimed_by="w1",
        sequence_steps=await service.step_store.list_for_campaign(ready.mail_campaign_id),
        windows=all_day_windows(), timezone_name=TZ, now=NOW,
    )
    assert not outcome.sent
    assert outcome.blocked_reason.value == "campaign_not_active"


# --- Formal PREPARE -> COMMIT contract test matrix (n=100) ------------------


async def test_activation_failure_at_1_of_100(crm, activity_log):
    """The very first enrollment's Step-1 materialization fails -- zero
    enrollments activated, campaign stays READY, zero Step-1 rows."""
    service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=100, fail_on_call_number=1
    )
    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    campaign_after = await service.get_campaign(ready.mail_campaign_id)
    assert campaign_after.status == MailCampaignStatus.READY
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)
    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert rows == []


async def test_activation_failure_at_50_of_100(crm, activity_log):
    """Fails partway through a 100-enrollment PREPARE loop -- exactly the
    enrollments processed before the failure are ACTIVE with one Step-1
    row each; campaign stays READY; nothing about the transition to
    ACTIVE is even attempted."""
    service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=100, fail_on_call_number=50
    )
    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    campaign_after = await service.get_campaign(ready.mail_campaign_id)
    assert campaign_after.status == MailCampaignStatus.READY
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    active = [e for e in enrollments if e.status == MailEnrollmentStatus.ACTIVE]
    pending = [e for e in enrollments if e.status == MailEnrollmentStatus.PENDING]
    assert len(active) == 49
    assert len(pending) == 51
    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 49


async def test_activation_failure_at_100_of_100_before_status_transition(crm, activity_log):
    """The PREPARE loop fully succeeds for all 100 enrollments -- every one
    is ACTIVE with a materialized Step-1 row -- but the COMMIT step's own
    campaign_store.save() (the READY->ACTIVE write) is what fails. This is
    the exact scenario the explicit completeness check exists for: proves
    the campaign is NOT flipped to ACTIVE merely because the PREPARE loop
    itself finished without raising."""
    flaky_campaign_store = FlakyCampaignStore(fail_on_call_number=3)  # 1: update_campaign, 2: mark_ready, 3: activate's COMMIT
    service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=100, fail_on_call_number=100_000, campaign_store=flaky_campaign_store
    )
    with pytest.raises(BoomError):
        await service.activate_campaign(ready.mail_campaign_id)

    campaign_after = await service.get_campaign(ready.mail_campaign_id)
    assert campaign_after.status == MailCampaignStatus.READY, "COMMIT's own failure must never leave the campaign ACTIVE"
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments), "PREPARE itself fully succeeded"
    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 100


async def test_activation_retries_after_1_of_100_50_of_100_and_100_of_100_all_fully_heal(activity_log):
    """Retry after each of the three failure points above reaches the
    exact same fully-correct end state: 100 enrollments ACTIVE, exactly
    100 Step-1 rows (no duplicates), campaign ACTIVE. A FRESH CrmService
    per iteration -- reusing one across iterations would collide on the
    same 100 contact emails the second time around."""
    for fail_at, campaign_store_factory in [
        (1, lambda: None),
        (50, lambda: None),
        (100_000, lambda: FlakyCampaignStore(fail_on_call_number=3)),  # forces the COMMIT-failure path instead
    ]:
        service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
            CrmService(), activity_log, n_contacts=100, fail_on_call_number=fail_at, campaign_store=campaign_store_factory()
        )
        with pytest.raises(BoomError):
            await service.activate_campaign(ready.mail_campaign_id)

        activated = await service.activate_campaign(ready.mail_campaign_id)
        assert activated.status == MailCampaignStatus.ACTIVE

        enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
        assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)
        assert len(enrollments) == 100

        rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
        assert len(rows) == 100, f"no duplicate Step-1 rows after retry (fail_at={fail_at})"
        per_enrollment: dict[str, int] = {}
        for r in rows:
            per_enrollment[r.enrollment_id] = per_enrollment.get(r.enrollment_id, 0) + 1
        assert all(count == 1 for count in per_enrollment.values()), f"no duplicate enrollment activation (fail_at={fail_at})"


async def test_repeated_activation_after_full_success_is_a_safe_idempotent_no_op(crm, activity_log):
    """Calling activate_campaign() again on an already-fully-ACTIVE
    campaign raises MailCampaignInvalidTransitionError (the campaign is no
    longer READY) -- it never silently re-processes, never duplicates
    anything, and never regresses status. This is the documented API
    contract for a repeat call on a campaign that needed no resumption at
    all."""
    from app.services.mail_campaign_service import MailCampaignInvalidTransitionError

    service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=10, fail_on_call_number=100_000  # never fails
    )
    activated = await service.activate_campaign(ready.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE

    with pytest.raises(MailCampaignInvalidTransitionError):
        await service.activate_campaign(ready.mail_campaign_id)

    rows = await service.enrollment_step_store.list_for_campaign(ready.mail_campaign_id)
    assert len(rows) == 10, "the rejected repeat call must not have touched anything"


async def test_commit_gate_refuses_to_activate_when_completeness_check_finds_a_gap(crm, activity_log):
    """Direct proof that the COMMIT gate is a REAL, independent check, not
    a rubber stamp: manually engineers a state PREPARE would never
    normally produce (an enrollment marked ACTIVE with no Step-1 row at
    all, simulating some future bug in the PREPARE loop) and confirms
    activate_campaign() refuses to transition the campaign to ACTIVE
    despite the PREPARE loop itself having nothing left to do."""
    service, ready, _ = await _build_ready_campaign_with_flaky_step_store(
        crm, activity_log, n_contacts=3, fail_on_call_number=100_000
    )
    enrollments = await service.enrollment_store.list_for_campaign(ready.mail_campaign_id)
    # Simulate the gap directly: flip one enrollment to ACTIVE without
    # ever materializing its Step-1 row (bypassing the normal
    # create_step1_execution() + save() pairing PREPARE always uses
    # together).
    tampered = enrollments[0].model_copy(update={"status": MailEnrollmentStatus.ACTIVE})
    await service.enrollment_store.save(tampered)

    result = await service.activate_campaign(ready.mail_campaign_id)
    assert result.status == MailCampaignStatus.READY, "COMMIT must refuse to transition while the invariant doesn't hold"

    steps = await service.step_store.list_for_campaign(ready.mail_campaign_id)
    incomplete = await service._find_incomplete_activation(ready.mail_campaign_id, steps[0])
    assert tampered.enrollment_id in incomplete


# --- Item 5: record_send_success() partial-failure + reconciliation --------


async def test_record_send_success_partial_failure_leaves_step_sent_but_next_step_missing():
    """3-step campaign, Step 1 execution exists and is SENDING. Inject a
    failure specifically in materializing Step 2 (the tail write after the
    SENDING->SENT transition already committed). Verify the exact stalled
    state: Step 1 is durably SENT, but Step 2 was never created, and the
    enrollment is still ACTIVE (not silently lost, not silently
    completed)."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep

    campaign_store = MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    flaky_step_store = FlakyEnrollmentStepStore(fail_on_call_number=1)  # the very next create() call (Step 2) fails
    activity_log = ActivityLogService(MemoryActivityEventStore())

    svc = MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=flaky_step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )

    steps = [
        MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        MailSequenceStep(step_id="s3", mail_campaign_id="c1", step_number=3, subject="s3", body="b3", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
    ]
    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="lead@example.com", status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )
    await enrollment_store.create(enrollment)

    sending_step = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1",
        step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENDING, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW,
    )
    # Note: NOT created via flaky_step_store.create() -- pre-seed it directly
    # into the underlying dict so the injected failure counts only the
    # NEXT create() call (Step 2's), matching the scenario precisely.
    flaky_step_store._rows[sending_step.enrollment_step_id] = sending_step

    send_result = SendResult(provider_message_id="msg-1", provider_thread_id="thr-1", rfc_message_id="<rfc-1>")
    with pytest.raises(BoomError):
        await svc.record_send_success(
            step=sending_step, send_result=send_result, sequence_steps=steps, enrollment=enrollment,
            windows=all_day_windows(), timezone_name=TZ, now=NOW,
        )

    persisted_step1 = await flaky_step_store.get("es1")
    assert persisted_step1.status == MailEnrollmentStepStatus.SENT, "the SENDING->SENT write must have already committed"
    all_rows = await flaky_step_store.list_for_enrollment("e1")
    assert len(all_rows) == 1, "Step 2 must NOT have been created -- this is the exact stalled state"
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.ACTIVE, "must not be silently lost or silently completed"


async def test_reconcile_stalled_progression_heals_the_missing_next_step():
    """Same stalled state as above, reproduced directly (no fault
    injection needed here) -- verifies reconcile_stalled_progression()
    finds and finishes exactly this gap: materializes the missing Step 2
    row, using the same idempotent logic record_send_success() itself
    uses."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep

    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailEnrollmentStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    svc = MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )
    steps = [
        MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
    ]
    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="lead@example.com", status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )
    await enrollment_store.create(enrollment)
    sent_step1 = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1",
        step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW,
    )
    await step_store.create(sent_step1)

    healed = await svc.reconcile_stalled_progression(
        enrollment=enrollment, sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    assert healed is True

    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 2
    step2_row = next(r for r in rows if r.step_number == 2)
    assert step2_row.status == MailEnrollmentStepStatus.QUEUED

    # Idempotent: calling it again once already healed is a safe no-op.
    healed_again = await svc.reconcile_stalled_progression(
        enrollment=enrollment, sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    assert healed_again is False
    rows_after = await step_store.list_for_enrollment("e1")
    assert len(rows_after) == 2, "must never create a duplicate Step 2 row"


async def test_reconcile_stalled_progression_completes_enrollment_when_last_step_was_sent():
    """A single-step campaign whose only step is SENT but the enrollment
    never got flipped to COMPLETED (the other half of the same stalled-
    tail-write class of bug) -- reconcile_stalled_progression() must
    complete it."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep

    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailEnrollmentStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    svc = MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )
    steps = [MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW)]
    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1",
        email_at_enrollment="lead@example.com", status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )
    await enrollment_store.create(enrollment)
    sent_step1 = MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1",
        step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW,
    )
    await step_store.create(sent_step1)

    healed = await svc.reconcile_stalled_progression(
        enrollment=enrollment, sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    assert healed is True
    enrollment_after = await enrollment_store.get("e1")
    assert enrollment_after.status == MailEnrollmentStatus.COMPLETED


# --- reconcile_stalled_progressions() (plural, batch/global-recovery) ------


async def test_reconcile_stalled_progressions_scans_the_whole_campaign_not_one_known_enrollment():
    """Three enrollments in the same campaign: one genuinely stalled (SENT
    Step 1, missing Step 2), one already fully healthy (Step 1 SENT, Step 2
    already QUEUED), one not ACTIVE at all (SUPPRESSED). A single batch
    call must reconcile exactly the one that needed it, leave the healthy
    one untouched, and skip the suppressed one entirely -- without the
    caller telling it in advance which enrollment_id was the problem."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep

    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailEnrollmentStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    svc = MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )
    steps = [
        MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
    ]

    # e1: genuinely stalled.
    e1 = MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1")
    await enrollment_store.create(e1)
    await step_store.create(MailEnrollmentStep(enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1", step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW))

    # e2: already healthy -- Step 2 already exists.
    e2 = MailEnrollment(enrollment_id="e2", mail_campaign_id="c1", crm_contact_id="contact-2", email_at_enrollment="b@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1")
    await enrollment_store.create(e2)
    await step_store.create(MailEnrollmentStep(enrollment_step_id="es2a", mail_campaign_id="c1", enrollment_id="e2", crm_contact_id="contact-2", step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW))
    await step_store.create(MailEnrollmentStep(enrollment_step_id="es2b", mail_campaign_id="c1", enrollment_id="e2", crm_contact_id="contact-2", step_id="s2", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, status=MailEnrollmentStepStatus.QUEUED, created_at=NOW, updated_at=NOW))

    # e3: suppressed -- not applicable at all.
    e3 = MailEnrollment(enrollment_id="e3", mail_campaign_id="c1", crm_contact_id="contact-3", email_at_enrollment="c@example.com", status=MailEnrollmentStatus.SUPPRESSED, enrolled_at=NOW, created_at=NOW)
    await enrollment_store.create(e3)

    result = await svc.reconcile_stalled_progressions(
        mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    assert result.scanned == 3
    assert result.reconciled == 1

    e1_rows = await step_store.list_for_enrollment("e1")
    assert len(e1_rows) == 2, "the stalled enrollment must have been healed"
    e2_rows = await step_store.list_for_enrollment("e2")
    assert len(e2_rows) == 2, "the already-healthy enrollment must be untouched (no duplicate)"


async def test_reconcile_stalled_progressions_is_harmless_when_called_repeatedly():
    """Same stalled scenario as above, but the batch method is called
    THREE times in a row -- the first call heals it, the second and third
    are pure no-ops (reconciled=0 each time), and the row count never
    grows past the correct total."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep

    enrollment_store = MemoryMailEnrollmentStore()
    step_store = MemoryMailEnrollmentStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    svc = MailSendingService(
        campaign_store=MemoryMailCampaignStore(), enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )
    steps = [
        MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
    ]
    e1 = MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1")
    await enrollment_store.create(e1)
    await step_store.create(MailEnrollmentStep(enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1", step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW))

    first = await svc.reconcile_stalled_progressions(mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    second = await svc.reconcile_stalled_progressions(mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
    third = await svc.reconcile_stalled_progressions(mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)

    assert first.reconciled == 1
    assert second.reconciled == 0
    assert third.reconciled == 0

    rows = await step_store.list_for_enrollment("e1")
    assert len(rows) == 2, "must never grow past the one correct Step 2 row across repeated calls"


async def test_reconcile_stalled_progressions_cannot_duplicate_the_next_step_even_under_a_real_race():
    """Direct proof of the UNIQUE(enrollment_id, step_id) backstop: two
    'concurrent' reconciliation sweeps (simulating a misconfigured Phase C
    worker somehow running twice) both try to materialize the SAME missing
    Step 2 row for the SAME enrollment. Uses the REAL SQLite-backed step
    store so the DB-level UNIQUE constraint is actually exercised, not
    just the in-memory store's own check-then-set."""
    from app.models.mail import MailEnrollment, MailEnrollmentStep
    from app.repositories.sqlite_mail_enrollment_step_store import SQLiteMailEnrollmentStepStore

    step_store = SQLiteMailEnrollmentStepStore(":memory:")
    await step_store.connect()
    try:
        enrollment_store = MemoryMailEnrollmentStore()
        activity_log = ActivityLogService(MemoryActivityEventStore())
        svc = MailSendingService(
            campaign_store=MemoryMailCampaignStore(), enrollment_store=enrollment_store, step_store=step_store,
            mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
            policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
            activity_log=activity_log,
        )
        steps = [
            MailSequenceStep(step_id="s1", mail_campaign_id="c1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, created_at=NOW, updated_at=NOW),
            MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="s2", body="b2", delay_days=1, reply_in_thread=True, created_at=NOW, updated_at=NOW),
        ]
        enrollment = MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="a@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1")
        await enrollment_store.create(enrollment)
        await step_store.create(MailEnrollmentStep(enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1", step_id="s1", step_number=1, subject="s1", body="b1", delay_days=0, reply_in_thread=True, status=MailEnrollmentStepStatus.SENT, sent_at=NOW, mailbox_id="mbx-1", created_at=NOW, updated_at=NOW))

        # Two "concurrent" sweeps, back to back (SQLite serializes the
        # actual writes; this proves the SECOND one is correctly rejected
        # by the UNIQUE constraint rather than blindly inserting a duplicate).
        first = await svc.reconcile_stalled_progressions(mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)
        second = await svc.reconcile_stalled_progressions(mail_campaign_id="c1", sequence_steps=steps, windows=all_day_windows(), timezone_name=TZ, now=NOW)

        assert first.reconciled == 1
        assert second.reconciled == 0

        rows = await step_store.list_for_enrollment("e1")
        assert len(rows) == 2
        step2_rows = [r for r in rows if r.step_number == 2]
        assert len(step2_rows) == 1, "UNIQUE(enrollment_id, step_id) must make a duplicate Step 2 impossible"
    finally:
        await step_store.close()
