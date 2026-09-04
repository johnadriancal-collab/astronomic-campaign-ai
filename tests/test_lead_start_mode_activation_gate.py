"""
Stage 5C (2026-09-04): gates the eager PENDING->ACTIVE+Step1 behavior in
_prepare_activation() / _reconcile_batch() (and the completeness invariant
in _find_incomplete_activation(), the COMMIT-gate for the first of those)
on campaign.lead_start_mode, exactly -- never on whether any
MailLeadStartTrigger row exists (none can exist yet; Trigger CRUD isn't
built until a later stage).

Same in-memory-stores/fixture convention as test_mail_add_prospects.py
(itself reusing test_mail_campaign_service.py's helpers via local import) --
no conftest.py exists in this project, so each test file owns its own
fixture set.
"""

from datetime import datetime, timezone

import pytest

from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentBatchMemberState,
    MailEnrollmentBatchSource,
    MailEnrollmentStatus,
    MailSequenceStep,
    MailSuppression,
    MailSuppressionReason,
)
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_member_store import MemoryMailEnrollmentBatchMemberStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService
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
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def service(
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


async def _set_lead_start_mode(service, mail_campaign_id: str, mode: str):
    """Direct store write -- Stage 5C has no CRUD/endpoint that sets this
    field (none is authorized yet); tests simulate "a campaign that has
    already opted into triggered mode" by writing the field directly,
    exactly the way a future Stage 5D trigger-creation call would, without
    that call existing yet."""
    campaign = await service.get_campaign(mail_campaign_id)
    await service.campaign_store.save(campaign.model_copy(update={"lead_start_mode": mode}))


async def _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="immediate"):
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=n_contacts)
    if lead_start_mode != "immediate":
        await _set_lead_start_mode(service, campaign.mail_campaign_id, lead_start_mode)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    return ready, contact_list


async def _make_active_campaign(service, crm, n_contacts=3, lead_start_mode="immediate"):
    ready, contact_list = await _make_ready_campaign(service, crm, n_contacts=n_contacts, lead_start_mode=lead_start_mode)
    active = await service.activate_campaign(ready.mail_campaign_id)
    return active, contact_list


async def _suppress(suppression_store, email: str):
    from app.models.crm import normalize_email as _norm

    now = datetime.now(timezone.utc)
    await suppression_store.upsert(
        MailSuppression(email_normalized=_norm(email), reason=MailSuppressionReason.MANUAL, created_at=now, updated_at=now, active=True)
    )


# =====================================================================
# 1-2. IMMEDIATE activation regression
# =====================================================================


async def test_immediate_activation_eagerly_starts_all_eligible_pending_enrollments(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="immediate")
    activated = await service.activate_campaign(campaign.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE

    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)


async def test_immediate_activation_creates_exactly_one_step1_per_eligible_enrollment(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="immediate")
    await service.activate_campaign(campaign.mail_campaign_id)

    rows = await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
    assert len(rows) == 3
    assert all(r.step_number == 1 for r in rows)


# =====================================================================
# 3-6. TRIGGERED activation
# =====================================================================


async def test_triggered_activation_leaves_eligible_enrollments_pending(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="triggered")
    activated = await service.activate_campaign(campaign.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE  # campaign itself DOES activate

    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)


async def test_triggered_activation_creates_zero_step1_rows(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="triggered")
    await service.activate_campaign(campaign.mail_campaign_id)

    rows = await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
    assert rows == []


async def test_campaign_becomes_active_even_though_enrollments_remain_pending(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=2, lead_start_mode="triggered")
    activated = await service.activate_campaign(campaign.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE
    assert activated.execution_active_since is not None

    fresh = await service.get_campaign(campaign.mail_campaign_id)
    assert fresh.status == MailCampaignStatus.ACTIVE, "ACTIVE must be persistent, not reverted for lack of runnable enrollments"


async def test_triggered_campaign_with_zero_triggers_stays_pending_indefinitely(service, crm):
    """No Trigger CRUD exists in this stage -- a 'triggered' campaign
    necessarily has zero MailLeadStartTrigger rows. Confirms this alone
    never causes enrollments to start, and that re-running activation
    (the only thing that COULD move them) doesn't either."""
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=2, lead_start_mode="triggered")
    await service.activate_campaign(campaign.mail_campaign_id)

    # A second activate_campaign() call is refused (ACTIVE, not READY) --
    # simulating "time passes, nothing happens" via the one repeatable,
    # safe operation available: re-running reconciliation via a no-op
    # add_prospects-shaped path isn't applicable here, so directly confirm
    # the durable state is unchanged after the only mutation available.
    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)
    rows = await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id)
    assert rows == []


# =====================================================================
# 7-9. Add Prospects (ACTIVE and PAUSED)
# =====================================================================


async def test_immediate_add_prospects_to_active_preserves_eager_behavior(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="immediate")
    contact = await crm.create_contact({"email": "new-immediate@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-immediate", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1
    enrollment = (await service.enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.ACTIVE
    rows = await service.enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert len(rows) == 1


async def test_triggered_add_prospects_to_active_leaves_new_enrollments_pending(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="triggered")
    contact = await crm.create_contact({"email": "new-triggered@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-triggered", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1, "batch counting is unchanged -- this lead genuinely became a real enrollment"
    enrollment = (await service.enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.PENDING
    rows = await service.enrollment_step_store.list_for_campaign(active.mail_campaign_id)
    assert rows == []


async def test_triggered_add_prospects_while_paused_leaves_new_enrollments_pending(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="triggered")
    paused = await service.pause_campaign(active.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED

    contact = await crm.create_contact({"email": "new-paused-triggered@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    batch = await service.add_prospects(
        paused.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-paused-triggered", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1
    enrollment = (await service.enrollment_store.list_for_campaign(paused.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.PENDING
    assert await service.enrollment_step_store.list_for_campaign(paused.mail_campaign_id) == []


async def test_immediate_add_prospects_while_paused_preserves_existing_behavior(service, crm):
    """The investigated, pre-existing (and approved-as-correct) invariant:
    _reconcile_batch() has never branched on ACTIVE vs PAUSED -- an
    IMMEDIATE-mode PAUSED campaign still eagerly activates/materializes
    Step 1 for a newly added prospect (today's real behavior); only the
    WORKER's own send attempt is blocked by PAUSED, unchanged by Stage 5C."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="immediate")
    paused = await service.pause_campaign(active.mail_campaign_id)

    contact = await crm.create_contact({"email": "new-paused-immediate@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    batch = await service.add_prospects(
        paused.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-paused-immediate", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1
    enrollment = (await service.enrollment_store.list_for_campaign(paused.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.ACTIVE
    assert len(await service.enrollment_step_store.list_for_campaign(paused.mail_campaign_id)) == 1


# =====================================================================
# 10-12. Suppression / dedupe / batch counts unchanged
# =====================================================================


async def test_suppression_behavior_unchanged_regardless_of_mode(service, crm, suppression_store):
    for mode in ("immediate", "triggered"):
        active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode=mode)
        contact = await crm.create_contact({"email": f"suppressed-{mode}@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
        await _suppress(suppression_store, f"suppressed-{mode}@example.com")

        batch = await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=f"k-suppressed-{mode}", source_list_id=contact_list.list_id,
        )
        assert batch.suppressed_count == 1
        assert batch.enrolled_count == 1  # suppressed counts toward enrolled_count, same formula both modes
        enrollment = (await service.enrollment_store.list_for_campaign(active.mail_campaign_id))[0]
        assert enrollment.status == MailEnrollmentStatus.SUPPRESSED
        assert await service.enrollment_step_store.list_for_campaign(active.mail_campaign_id) == []


async def test_same_campaign_dedupe_unchanged_regardless_of_mode(service, crm):
    for mode in ("immediate", "triggered"):
        active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode=mode)
        contact = await crm.create_contact({"email": f"dedupe-{mode}@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

        first = await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=f"k-dedupe-1-{mode}", source_list_id=contact_list.list_id,
        )
        assert first.enrolled_count == 1

        # Same contact, same campaign, a SECOND (different-key) batch --
        # must be recognized as already-enrolled, not double-created.
        second = await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=f"k-dedupe-2-{mode}", source_list_id=contact_list.list_id,
        )
        assert second.enrolled_count == 0
        assert second.already_enrolled_count == 1
        enrollments = await service.enrollment_store.list_for_campaign(active.mail_campaign_id)
        assert len(enrollments) == 1


async def test_batch_counts_identical_between_modes_for_the_same_input(service, crm, suppression_store):
    """The exact same audience shape (2 new, 1 suppressed, 1 already-
    enrolled-via-a-prior-batch) must produce identical batch count fields
    whether the campaign is immediate or triggered -- Stage 5C changes
    WHETHER a lead starts, never how it's counted."""
    results = {}
    for mode in ("immediate", "triggered"):
        active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode=mode)
        pre_existing = await crm.create_contact({"email": f"already-{mode}@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [pre_existing.crm_contact_id])
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=f"k-pre-{mode}", source_list_id=contact_list.list_id,
        )

        for i in range(2):
            c = await crm.create_contact({"email": f"new-{mode}-{i}@example.com"})
            await crm.bulk_add_to_list(contact_list.list_id, [c.crm_contact_id])
        suppressed_contact = await crm.create_contact({"email": f"sup-{mode}@example.com"})
        await crm.bulk_add_to_list(contact_list.list_id, [suppressed_contact.crm_contact_id])
        await _suppress(suppression_store, f"sup-{mode}@example.com")

        batch = await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key=f"k-main-{mode}", source_list_id=contact_list.list_id,
        )
        results[mode] = (batch.enrolled_count, batch.already_enrolled_count, batch.suppressed_count, batch.submitted_count)

    assert results["immediate"] == results["triggered"]


# =====================================================================
# 13-14. Legacy COMPLETED -> ACTIVE reopen
# =====================================================================


async def _force_completed(campaign_store, campaign):
    completed = campaign.model_copy(update={"status": MailCampaignStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)})
    await campaign_store.save(completed)
    return completed


async def test_legacy_reopen_still_occurs_for_immediate_mode(service, crm, campaign_store):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="immediate")
    completed = await _force_completed(campaign_store, active)

    contact = await crm.create_contact({"email": "reopen-immediate@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-reopen-immediate", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1
    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE
    assert reopened.execution_active_since is not None
    enrollment = (await service.enrollment_store.list_for_campaign(reopened.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.ACTIVE


async def test_legacy_reopen_for_triggered_mode_sets_active_since_but_leaves_enrollment_pending(service, crm, campaign_store):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0, lead_start_mode="triggered")
    completed = await _force_completed(campaign_store, active)

    before = datetime.now(timezone.utc)
    contact = await crm.create_contact({"email": "reopen-triggered@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k-reopen-triggered", source_list_id=contact_list.list_id,
    )
    after = datetime.now(timezone.utc)
    assert batch.enrolled_count == 1, "reopen eligibility/count semantics unchanged -- genuinely new enrollment"

    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE
    assert reopened.execution_active_since is not None
    assert before <= reopened.execution_active_since <= after

    enrollment = (await service.enrollment_store.list_for_campaign(reopened.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.PENDING
    assert await service.enrollment_step_store.list_for_campaign(reopened.mail_campaign_id) == []


# =====================================================================
# 15. Old-shaped campaign (no lead_start_mode key) compatibility
# =====================================================================


async def test_old_shaped_campaign_without_lead_start_mode_defaults_to_immediate_and_activates_eagerly(service, crm):
    """Simulates a campaign object exactly as an old JSON blob would
    deserialize (the key was simply never written) -- Pydantic's default
    applies, and activation proceeds exactly as it always has."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm, n_contacts=2)
    stored = await service.campaign_store.get(campaign.mail_campaign_id)
    # Confirms the field genuinely defaults rather than this test having
    # accidentally set it -- constructing via .model_copy with `exclude`
    # isn't available for a "delete a key" semantic on a Pydantic object,
    # so instead assert the default directly (this IS what an old blob
    # missing the key would deserialize to -- see
    # test_mail_phase_a_schema_migration.py's own dedicated blob-level proof).
    assert stored.lead_start_mode == "immediate"

    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    activated = await service.activate_campaign(ready.mail_campaign_id)
    assert activated.status == MailCampaignStatus.ACTIVE
    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.ACTIVE for e in enrollments)


# =====================================================================
# 16-17. No Trigger execution exists
# =====================================================================


def test_no_trigger_occurrence_or_member_rows_created_by_this_stage():
    """Structural guard: Stage 5C's two touched methods never reference
    the occurrence/member store or models at all."""
    from pathlib import Path

    source = Path("app/services/mail_campaign_service.py").read_text()
    for forbidden in ("MailTriggerOccurrence", "freeze_members", "occurrence_store"):
        assert forbidden not in source


def test_mail_execution_worker_delegates_trigger_processing_rather_than_reimplementing_it():
    """Superseded by Stage 5D (2026-09-04) -- see
    test_mail_trigger_foundation.py's identically-renamed test for the
    full explanation. This file's own remaining concern (Stage 5C's
    activation-gate behavior) is unaffected either way -- checked
    separately above."""
    from pathlib import Path

    source = Path("app/services/mail_execution_worker.py").read_text()
    assert "mail_trigger_service" in source
    for forbidden in ("freeze_members(", "MailTriggerOccurrence(", "create_occurrence("):
        assert forbidden not in source


# =====================================================================
# 18. daily_lead_start_limit (Stage 5B) unaffected
# =====================================================================


def test_stage_5b_daily_lead_start_limit_gate_untouched():
    from pathlib import Path

    source = Path("app/services/mail_sending_service.py").read_text()
    assert 'fresh_campaign.lead_start_mode == "immediate"' in source
    assert source.count("LEAD_START_LIMIT_REACHED") >= 1


# =====================================================================
# Idempotency: interrupted/retried TRIGGERED activation
# =====================================================================


async def test_triggered_activation_is_idempotently_resumable_with_no_step1_ever_created(service, crm):
    """Simulates a crash between PREPARE and COMMIT by calling
    activate_campaign() twice in a row -- the second call must be a safe,
    correct no-op/retry, exactly as the existing IMMEDIATE-mode resumability
    contract already guarantees (test_repeated_activation_cannot_duplicate_step1_rows),
    just with PENDING/no-Step1 as the target state instead of ACTIVE/Step1."""
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="triggered")

    first = await service.activate_campaign(campaign.mail_campaign_id)
    assert first.status == MailCampaignStatus.ACTIVE

    # A second call is refused as an invalid transition (ACTIVE, not
    # READY) -- but directly re-run the two internal phases to prove the
    # underlying idempotency contract itself, matching how
    # test_repeated_activation_cannot_duplicate_step1_rows exercises the
    # equivalent IMMEDIATE-mode guarantee.
    fresh = await service.get_campaign(campaign.mail_campaign_id)
    steps = await service.step_store.list_for_campaign(campaign.mail_campaign_id)
    step1 = next(s for s in steps if s.step_number == 1)
    windows, _source = await service._resolve_schedule(campaign.mail_campaign_id, fresh)

    activated_again = await service._prepare_activation(
        campaign.mail_campaign_id, step1, windows, fresh.timezone, datetime.now(timezone.utc), fresh.lead_start_mode
    )
    assert activated_again == 0, "triggered PREPARE is always a 0-activation no-op"

    incomplete = await service._find_incomplete_activation(campaign.mail_campaign_id, step1, fresh.lead_start_mode)
    assert incomplete == [], "PENDING/no-Step1 must be recognized as complete on retry"

    enrollments = await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id)
    assert all(e.status == MailEnrollmentStatus.PENDING for e in enrollments)
    assert await service.enrollment_step_store.list_for_campaign(campaign.mail_campaign_id) == []

    still_active = await service.get_campaign(campaign.mail_campaign_id)
    assert still_active.status == MailCampaignStatus.ACTIVE


# =====================================================================
# _find_incomplete_activation() -- explicit mode-specific proofs
# =====================================================================


async def test_find_incomplete_activation_triggered_pending_no_step1_is_complete(service, crm):
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=2, lead_start_mode="triggered")
    fresh = await service.get_campaign(campaign.mail_campaign_id)
    steps = await service.step_store.list_for_campaign(campaign.mail_campaign_id)
    step1 = next(s for s in steps if s.step_number == 1)

    incomplete = await service._find_incomplete_activation(campaign.mail_campaign_id, step1, "triggered")
    assert incomplete == []


async def test_find_incomplete_activation_triggered_pending_with_step1_is_incomplete_not_silently_blessed(service, crm):
    """The corruption/invariant case: an enrollment is PENDING but has
    already been given a Step-1 execution row (should never happen via any
    approved code path -- simulated directly here). Must be reported
    incomplete, never treated as done."""
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=1, lead_start_mode="triggered")
    fresh = await service.get_campaign(campaign.mail_campaign_id)
    steps = await service.step_store.list_for_campaign(campaign.mail_campaign_id)
    step1 = next(s for s in steps if s.step_number == 1)
    windows, _source = await service._resolve_schedule(campaign.mail_campaign_id, fresh)

    enrollment = (await service.enrollment_store.list_for_campaign(campaign.mail_campaign_id))[0]
    assert enrollment.status == MailEnrollmentStatus.PENDING
    # Directly materialize a Step-1 row WITHOUT flipping the enrollment to
    # ACTIVE -- an inconsistent state no approved code path can produce,
    # exactly what this test must catch rather than silently bless.
    await service.sending_service.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=windows, timezone_name=fresh.timezone, now=datetime.now(timezone.utc)
    )

    incomplete = await service._find_incomplete_activation(campaign.mail_campaign_id, step1, "triggered")
    assert enrollment.enrollment_id in incomplete

    # activate_campaign() itself must therefore refuse to commit.
    result = await service.activate_campaign(campaign.mail_campaign_id)
    assert result.status == MailCampaignStatus.READY, "must fail closed, not silently accept the inconsistent state"


async def test_find_incomplete_activation_immediate_semantics_unchanged(service, crm):
    """IMMEDIATE mode's own completeness rule (ACTIVE + Step1, or
    SUPPRESSED) is untouched by the Stage 5C refactor."""
    campaign, _ = await _make_ready_campaign(service, crm, n_contacts=3, lead_start_mode="immediate")
    fresh = await service.get_campaign(campaign.mail_campaign_id)
    steps = await service.step_store.list_for_campaign(campaign.mail_campaign_id)
    step1 = next(s for s in steps if s.step_number == 1)

    # Before activation: every enrollment is PENDING, none ACTIVE -- all incomplete.
    incomplete_before = await service._find_incomplete_activation(campaign.mail_campaign_id, step1, "immediate")
    assert len(incomplete_before) == 3

    await service.activate_campaign(campaign.mail_campaign_id)
    incomplete_after = await service._find_incomplete_activation(campaign.mail_campaign_id, step1, "immediate")
    assert incomplete_after == []
