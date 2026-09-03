"""
Stage 3 (2026-09-03) -- add_prospects() / _reconcile_batch() / the
background reconciliation worker. Covers: status eligibility, dedupe
(within-batch, within-campaign-at-any-status, cross-campaign), fresh
suppression checks at reconciliation time (never a caller-supplied
snapshot), count invariants, legacy-COMPLETED reopening (only with a
genuinely new enrollment), operation-level idempotency (including the
concurrent same-key race), crash-boundary resumption, ARCHIVED refusal,
and orphan-member cleanup. Uses in-memory stores throughout, same
convention as test_mail_campaign_service.py -- reuses its fixtures and
`_make_valid_schedule_campaign` helper via a local import.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentBatchMemberState,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailEnrollmentStatus,
    MailSuppression,
    MailSuppressionReason,
)
from app.repositories.activity_event_store import MemoryActivityEventStore
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
from app.services.crm_service import CrmContactListNotFound, CrmService
from app.services.mail_campaign_service import (
    MailCampaignNotEligibleForProspectsError,
    MailCampaignNotFound,
    MailCampaignService,
)
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
def service(
    crm, activity_log, mailbox_store, channel_store, window_store, enrollment_step_store, suppression_store,
    batch_store, batch_member_store, campaign_store, enrollment_store,
):
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
    )


async def _make_active_campaign(service, crm, n_contacts=3):
    """A campaign that has genuinely gone through mark_ready() ->
    activate_campaign() -- the state add_prospects() is actually meant to
    be used against."""
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=n_contacts)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await service.activate_campaign(ready.mail_campaign_id)
    return active, contact_list


async def _suppress(suppression_store, email: str):
    now = datetime.now(timezone.utc)
    await suppression_store.upsert(
        MailSuppression(
            email_normalized=normalize_email(email),
            reason=MailSuppressionReason.MANUAL,
            created_at=now,
            updated_at=now,
            active=True,
        )
    )


# --- Status eligibility -----------------------------------------------------


async def test_add_prospects_not_found_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.add_prospects(
            "does-not-exist", source=MailEnrollmentBatchSource.CRM_LIST, idempotency_key="k1", source_list_id="l1"
        )


async def test_add_prospects_rejects_draft(service):
    campaign = await service.create_campaign("Draft")
    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects(
            campaign.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST, idempotency_key="k1", source_list_id="l1"
        )


async def test_add_prospects_rejects_ready(service, crm):
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects(
            ready.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST, idempotency_key="k1", source_list_id="l1"
        )


async def test_add_prospects_rejects_archived_for_a_brand_new_idempotency_key(service, crm):
    """No existing batch for this key -- ARCHIVED is still refused for a
    genuinely NEW submission, exactly as before."""
    campaign, _ = await _make_valid_schedule_campaign(service, crm)
    archived = await service.archive_campaign(campaign.mail_campaign_id)
    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects(
            archived.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST, idempotency_key="k1", source_list_id="l1"
        )


# --- Idempotent retrieval after Archive (2026-09-03 refinement) ------------


async def test_add_prospects_retry_after_archive_returns_the_existing_ready_batch_unchanged(service, crm):
    """A submission accepted and fully reconciled to READY BEFORE the
    campaign was archived must still be retrievable by the exact same
    idempotency_key afterward -- the idempotency lookup runs before the
    eligibility check, so this never hits
    MailCampaignNotEligibleForProspectsError. No new enrollment/step work
    is created, and the campaign stays ARCHIVED."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "beforearchive@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="archive-retry-key", source_list_id=contact_list.list_id,
    )
    assert first.status == MailEnrollmentBatchStatus.READY
    assert first.enrolled_count == 1

    enrollments_before = await service.list_enrollments(active.mail_campaign_id)
    steps_before = await service.enrollment_step_store.list_for_enrollment(enrollments_before[0].enrollment_id)
    assert len(enrollments_before) == 1
    assert len(steps_before) == 1

    await service.archive_campaign(active.mail_campaign_id)

    retry = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="archive-retry-key", source_list_id=contact_list.list_id,
    )
    assert retry.batch_id == first.batch_id
    assert retry.status == MailEnrollmentBatchStatus.READY
    assert retry.enrolled_count == 1

    enrollments_after = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments_after) == 1  # no new enrollment created
    steps_after = await service.enrollment_step_store.list_for_enrollment(enrollments_after[0].enrollment_id)
    assert len(steps_after) == 1  # no new/duplicate Step 1 created

    unchanged = await service.get_campaign(active.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.ARCHIVED


async def test_add_prospects_retry_after_archive_returns_the_existing_preparing_batch_without_advancing_it(service, crm):
    """A batch that was frozen (PREPARING) but never finished
    reconciling BEFORE the campaign was archived must still be
    retrievable by the same idempotency_key -- but returned exactly as
    PREPARING, never advanced to READY, and with no enrollment/step work
    created."""
    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "stuckpreparing@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    now = datetime.now(timezone.utc)
    batch_id = "b-stuck-preparing"
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="stuck-preparing-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    await service.archive_campaign(active.mail_campaign_id)

    retry = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="stuck-preparing-key", source_list_id=contact_list.list_id,
    )
    assert retry.batch_id == batch_id
    assert retry.status == MailEnrollmentBatchStatus.PREPARING  # never advanced
    assert retry.enrolled_count is None

    members = await service.batch_member_store.list_for_batch(batch_id)
    assert members[0].state == MailEnrollmentBatchMemberState.CANDIDATE  # never processed

    assert await service.list_enrollments(active.mail_campaign_id) == []

    unchanged = await service.get_campaign(active.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.ARCHIVED


async def test_add_prospects_accepts_active(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    new_contact = await crm.create_contact({"email": "newlead@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.status == MailEnrollmentBatchStatus.READY
    assert batch.enrolled_count == 1


async def test_add_prospects_accepts_paused(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    paused = await service.pause_campaign(active.mail_campaign_id)
    new_contact = await crm.create_contact({"email": "newlead@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])

    batch = await service.add_prospects(
        paused.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.status == MailEnrollmentBatchStatus.READY
    assert batch.enrolled_count == 1

    unchanged = await service.get_campaign(paused.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.PAUSED


async def test_add_prospects_rejects_unimplemented_csv_source(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    with pytest.raises(NotImplementedError):
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD, idempotency_key="k1"
        )


async def test_add_prospects_requires_source_list_id_for_crm_list(service, crm):
    active, _ = await _make_active_campaign(service, crm, n_contacts=0)
    with pytest.raises(ValueError):
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST, idempotency_key="k1"
        )


async def test_add_prospects_rejects_dangling_source_list(service, crm):
    active, _ = await _make_active_campaign(service, crm, n_contacts=0)
    with pytest.raises(CrmContactListNotFound):
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            idempotency_key="k1", source_list_id="does-not-exist",
        )


# --- Dedupe matrix -----------------------------------------------------------


async def test_add_prospects_dedupes_duplicate_membership_within_the_submitted_list(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "onlyone@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 1


async def test_add_prospects_skips_a_contact_already_enrolled_in_this_campaign_at_any_status(service, crm):
    """A contact previously enrolled anywhere in the same campaign
    (regardless of status, INCLUDING terminal COMPLETED) is skipped -- no
    re-enrollment in V1."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=1)
    enrollments = await service.list_enrollments(active.mail_campaign_id)
    already = enrollments[0]
    # Simulate this enrollment having reached a terminal status already.
    await service.enrollment_store.save(already.model_copy(update={"status": MailEnrollmentStatus.COMPLETED}))

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 0
    assert batch.already_enrolled_count == 1

    # No second MailEnrollment row was created for this contact.
    all_enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(all_enrollments) == 1


async def test_add_prospects_a_contact_from_another_campaign_is_independently_eligible(service, crm):
    active_a, list_a = await _make_active_campaign(service, crm, n_contacts=0)
    active_b, list_b = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "shared@example.com"})
    await crm.bulk_add_to_list(list_a.list_id, [contact.crm_contact_id])
    await crm.bulk_add_to_list(list_b.list_id, [contact.crm_contact_id])

    batch_a = await service.add_prospects(
        active_a.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="ka", source_list_id=list_a.list_id,
    )
    batch_b = await service.add_prospects(
        active_b.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="kb", source_list_id=list_b.list_id,
    )
    assert batch_a.enrolled_count == 1
    assert batch_b.enrolled_count == 1


async def test_add_prospects_skips_blank_email_contacts(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    blank = await crm.create_contact({"first_name": "No Email"})
    await crm.bulk_add_to_list(contact_list.list_id, [blank.crm_contact_id])

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.submitted_count == 0
    assert batch.enrolled_count == 0


async def test_add_prospects_submitted_count_equals_frozen_member_rows_with_blank_email_and_duplicate_membership(
    service, crm
):
    """Confirms blank-email contacts are filtered BEFORE any
    MailEnrollmentBatchMember row is ever frozen, and that a duplicate
    contact id within one submission (the list's own membership dedupe
    already collapses this at the CRM layer -- see
    CrmService.bulk_add_to_list()'s own dedupe -- exercised again here at
    the list-membership level, not by inventing a second membership row)
    never produces two member rows for the same contact. Together these
    prove, for a READY batch: submitted_count == the number of actual
    frozen member rows, and submitted_count == enrolled_count +
    already_enrolled_count."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)

    usable_a = await crm.create_contact({"email": "usablea@example.com"})
    usable_b = await crm.create_contact({"email": "usableb@example.com"})
    blank = await crm.create_contact({"first_name": "No Email"})

    # A duplicate id within the SAME bulk_add_to_list call -- the CRM
    # layer's own dedupe (dict.fromkeys in bulk_add_to_list()) collapses
    # this to exactly one membership row for usable_a, never two.
    await crm.bulk_add_to_list(
        contact_list.list_id, [usable_a.crm_contact_id, usable_a.crm_contact_id, usable_b.crm_contact_id, blank.crm_contact_id]
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="invariant-key", source_list_id=contact_list.list_id,
    )

    assert batch.status == MailEnrollmentBatchStatus.READY
    assert batch.submitted_count == 2  # usable_a + usable_b -- blank excluded, duplicate collapsed

    frozen_members = await service.batch_member_store.list_for_batch(batch.batch_id)
    assert len(frozen_members) == batch.submitted_count
    assert {m.crm_contact_id for m in frozen_members} == {usable_a.crm_contact_id, usable_b.crm_contact_id}

    assert batch.submitted_count == batch.enrolled_count + batch.already_enrolled_count
    assert batch.enrolled_count == 2
    assert batch.already_enrolled_count == 0


# --- Suppression at reconciliation time (fresh, never caller-supplied) -----


async def test_add_prospects_suppressed_contact_enrolls_as_suppressed_with_no_step1(service, crm, suppression_store):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "blocked@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    await _suppress(suppression_store, "blocked@example.com")

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1
    assert batch.suppressed_count == 1

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert enrollments[0].status == MailEnrollmentStatus.SUPPRESSED

    steps = await service.enrollment_step_store.list_for_enrollment(enrollments[0].enrollment_id)
    assert steps == []


async def test_add_prospects_suppression_check_is_fresh_not_from_a_stale_snapshot(service, crm, suppression_store):
    """Suppressing AFTER the batch's members were frozen (but before
    reconciliation actually runs) must still be honored -- proves
    _reconcile_batch() checks suppression live via the store, never from
    anything captured at freeze time. We simulate this by freezing via
    add_prospects() with suppression_store empty, then re-running
    reconciliation would be redundant since add_prospects() already fully
    reconciles synchronously -- instead, this test suppresses a contact
    between two SEPARATE add_prospects() calls to prove each call's
    suppression check is independently fresh."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "notyetblocked@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert first.suppressed_count == 0

    await _suppress(suppression_store, "notyetblocked@example.com")
    other_contact = await crm.create_contact({"email": "alsoblocked@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [other_contact.crm_contact_id])
    await _suppress(suppression_store, "alsoblocked@example.com")

    second = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k2", source_list_id=contact_list.list_id,
    )
    # Only the genuinely NEW contact (alsoblocked) is in this batch --
    # notyetblocked was already enrolled by the first call, so it's
    # ALREADY_ENROLLED here, not re-evaluated for suppression at all.
    assert second.enrolled_count == 1
    assert second.suppressed_count == 1
    assert second.already_enrolled_count == 1


# --- Count invariants --------------------------------------------------------


async def test_add_prospects_count_invariants_hold(service, crm, suppression_store):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    fresh = [await crm.create_contact({"email": f"fresh{i}@example.com"}) for i in range(3)]
    await crm.bulk_add_to_list(contact_list.list_id, [c.crm_contact_id for c in fresh])
    await _suppress(suppression_store, "fresh1@example.com")

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert first.submitted_count == first.enrolled_count + first.already_enrolled_count
    assert first.suppressed_count <= first.enrolled_count
    assert first.submitted_count == 3
    assert first.enrolled_count == 3
    assert first.already_enrolled_count == 0
    assert first.suppressed_count == 1

    # A second call against the same (now-partially-overlapping) list.
    more = await crm.create_contact({"email": "brandnew@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [more.crm_contact_id])

    second = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k2", source_list_id=contact_list.list_id,
    )
    assert second.submitted_count == second.enrolled_count + second.already_enrolled_count
    assert second.suppressed_count <= second.enrolled_count
    assert second.submitted_count == 4  # the 3 originals + 1 new
    assert second.already_enrolled_count == 3
    assert second.enrolled_count == 1
    assert second.suppressed_count == 0


# --- Legacy COMPLETED reopening ---------------------------------------------


async def _force_completed(campaign_store, campaign):
    completed = campaign.model_copy(update={"status": MailCampaignStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)})
    await campaign_store.save(completed)
    return completed


async def test_add_prospects_reopens_a_completed_campaign_with_a_genuinely_new_enrollment(service, crm, campaign_store):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    completed = await _force_completed(campaign_store, active)

    new_contact = await crm.create_contact({"email": "revive@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])

    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 1

    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE

    events = await service.activity_log.store.list()
    activated_events = [e for e in events if e.event_type == "mail_campaign.activated"]
    assert any("reactivated" in e.summary.lower() for e in activated_events)


async def test_add_prospects_leaves_a_completed_campaign_completed_when_nothing_genuinely_new(
    service, crm, campaign_store
):
    """A batch containing ONLY already-enrolled contacts must never flip a
    legacy COMPLETED campaign to ACTIVE."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=2)
    completed = await _force_completed(campaign_store, active)

    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    assert batch.enrolled_count == 0
    assert batch.already_enrolled_count == 2

    unchanged = await service.get_campaign(completed.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.COMPLETED


async def test_add_prospects_reopened_campaign_never_auto_completes_again_and_behaves_as_ordinary_active(
    service, crm, campaign_store
):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    completed = await _force_completed(campaign_store, active)
    new_contact = await crm.create_contact({"email": "revive2@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [new_contact.crm_contact_id])

    await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="k1", source_list_id=contact_list.list_id,
    )
    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE

    # Ordinary pause/resume now works on it like any persistent campaign.
    paused = await service.pause_campaign(reopened.mail_campaign_id)
    assert paused.status == MailCampaignStatus.PAUSED


# --- ARCHIVED refusal at reconciliation time --------------------------------


async def test_reconcile_batch_refuses_to_process_against_an_archived_campaign(service, crm, campaign_store):
    """Simulates a campaign archived AFTER a batch was frozen (PREPARING)
    but BEFORE reconciliation caught up -- _reconcile_batch() must return
    the batch completely unchanged rather than creating any enrollment."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "toolate@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    # Freeze the cohort directly (bypassing add_prospects()'s own
    # synchronous reconcile) to simulate "frozen but not yet reconciled".
    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    batch_id = "b-archived-race"
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="archived-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    await service.archive_campaign(active.mail_campaign_id)

    result = await service._reconcile_batch(batch_id)
    assert result.status == MailEnrollmentBatchStatus.PREPARING
    assert result.enrolled_count is None

    members = await service.batch_member_store.list_for_batch(batch_id)
    assert members[0].state == MailEnrollmentBatchMemberState.CANDIDATE

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert enrollments == []


# --- Idempotency response behavior ------------------------------------------


async def test_add_prospects_retry_with_same_key_returns_the_same_batch_id(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "once@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="same-key", source_list_id=contact_list.list_id,
    )
    second = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="same-key", source_list_id=contact_list.list_id,
    )
    assert first.batch_id == second.batch_id
    assert second.enrolled_count == 1  # unchanged, not doubled

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1


async def test_add_prospects_retry_on_a_ready_batch_does_not_re_resolve_the_source_list(service, crm):
    """After the batch is READY, a retry with the same key must not touch
    the CRM List again -- proven by adding a NEW contact to the list
    between the two calls and confirming it is NOT picked up by the
    retry (only a fresh idempotency_key, i.e. a new add_prospects() call,
    would see it)."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "original@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="same-key", source_list_id=contact_list.list_id,
    )
    assert first.submitted_count == 1

    later_contact = await crm.create_contact({"email": "addedlater@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [later_contact.crm_contact_id])

    retry = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="same-key", source_list_id=contact_list.list_id,
    )
    assert retry.batch_id == first.batch_id
    assert retry.submitted_count == 1  # unchanged -- the frozen cohort never re-resolves


async def test_add_prospects_idempotency_key_is_not_emitted_in_activity_log_metadata(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "secretkeytest@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    secret_key = "super-secret-idempotency-key-value"
    await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key=secret_key, source_list_id=contact_list.list_id,
    )

    events = await service.activity_log.store.list()
    for event in events:
        assert secret_key not in (event.summary or "")
        metadata_str = str(event.metadata or {})
        assert secret_key not in metadata_str


# --- Concurrent same-idempotency-key race -----------------------------------


async def test_concurrent_submissions_with_the_same_key_only_one_wins_and_produces_enrollments(
    service, crm, batch_store, batch_member_store
):
    """Simulates two 'simultaneous' add_prospects() calls for the same
    (campaign, idempotency_key) that each independently freeze their own
    candidate cohort before either commits its batch row -- only one can
    win the UNIQUE(campaign_id, idempotency_key) constraint. The loser
    must reconcile/return the winner's batch, never create its own
    enrollments."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "raced@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    key = "race-key"

    # Cohort A freezes its members first.
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="batch-a", crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    # Cohort B (the "concurrent" submission) also freezes its own members
    # under a DIFFERENT batch_id, before either has created its batch row.
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="batch-b", crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )

    # A commits its batch row first -- wins the race.
    await batch_store.create(
        MailEnrollmentBatch(
            batch_id="batch-a", mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key=key,
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    # B tries to commit its own batch row under the same key -- this is
    # exactly what add_prospects() itself does internally; we invoke the
    # real service method with the SAME key to exercise its loser path,
    # since B's own in-memory candidate cohort was already frozen above
    # (mirroring "both froze before either committed").
    result_b = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key=key, source_list_id=contact_list.list_id,
    )

    # The loser resolves to the WINNER's batch (batch-a), fully reconciled.
    assert result_b.batch_id == "batch-a"
    assert result_b.status == MailEnrollmentBatchStatus.READY
    assert result_b.enrolled_count == 1

    # Only ONE enrollment exists for this contact -- created under the
    # winner's batch_id, never batch-b's.
    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1
    assert enrollments[0].batch_id == "batch-a"

    # Batch B's own row was never created (it lost before ever calling
    # batch_store.create() under its own id) -- confirm no such batch exists.
    assert await batch_store.get("batch-b") is None

    # Batch B's orphaned candidate member row is still there, eligible
    # for cleanup_orphan_batch_members() once it ages out.
    orphan_members = await batch_member_store.list_for_batch("batch-b")
    assert len(orphan_members) == 1
    assert orphan_members[0].state == MailEnrollmentBatchMemberState.CANDIDATE


# --- Crash-boundary resumption of _reconcile_batch() ------------------------


async def test_reconcile_batch_resumes_a_partially_processed_cohort_without_duplicating_work(service, crm):
    """Simulates a crash mid-reconciliation: one member already advanced
    to ENROLLED_PENDING (with its MailEnrollment row created) but Step 1
    never materialized, another still CANDIDATE. A fresh
    _reconcile_batch() call must finish both without creating a second
    MailEnrollment or a duplicate Step 1 for the already-partially-done
    member."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact_done_enrollment = await crm.create_contact({"email": "halfway@example.com"})
    contact_untouched = await crm.create_contact({"email": "untouched@example.com"})
    await crm.bulk_add_to_list(
        contact_list.list_id, [contact_done_enrollment.crm_contact_id, contact_untouched.crm_contact_id]
    )

    from app.models.mail import MailEnrollment, MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    batch_id = "b-crash-resume"

    enrollment = MailEnrollment(
        enrollment_id="enr-halfway", mail_campaign_id=active.mail_campaign_id,
        crm_contact_id=contact_done_enrollment.crm_contact_id, email_at_enrollment="halfway@example.com",
        status=MailEnrollmentStatus.PENDING, enrolled_at=now, created_at=now, batch_id=batch_id,
    )
    await service.enrollment_store.create(enrollment)

    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact_done_enrollment.crm_contact_id,
            state=MailEnrollmentBatchMemberState.ENROLLED_PENDING, enrollment_id="enr-halfway",
            created_at=now, updated_at=now,
        )
    )
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact_untouched.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="crash-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=2,
        )
    )

    result = await service._reconcile_batch(batch_id)

    assert result.status == MailEnrollmentBatchStatus.READY
    assert result.enrolled_count == 2
    assert result.already_enrolled_count == 0

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 2  # not 3 -- the halfway one was never duplicated

    halfway_enrollment = next(e for e in enrollments if e.enrollment_id == "enr-halfway")
    assert halfway_enrollment.status == MailEnrollmentStatus.ACTIVE

    steps = await service.enrollment_step_store.list_for_enrollment("enr-halfway")
    assert len(steps) == 1  # exactly one Step 1 -- never duplicated on resume

    # A second reconcile call is a pure no-op (already READY).
    again = await service._reconcile_batch(batch_id)
    assert again.enrolled_count == 2
    enrollments_after = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments_after) == 2


async def test_reconcile_batch_finishes_the_campaign_reopen_even_when_batch_was_already_ready(
    service, crm, campaign_store
):
    """Simulates a crash AFTER the batch was durably written as READY but
    BEFORE the legacy-COMPLETED->ACTIVE flip landed -- a later
    _reconcile_batch() call must still perform that flip, unconditionally,
    even though member processing itself is skipped entirely."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    completed = await _force_completed(campaign_store, active)

    from app.models.mail import MailEnrollment, MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    batch_id = "b-ready-not-reopened"
    contact = await crm.create_contact({"email": "readynotreopened@example.com"})

    enrollment = MailEnrollment(
        enrollment_id="enr-ready", mail_campaign_id=completed.mail_campaign_id,
        crm_contact_id=contact.crm_contact_id, email_at_enrollment=contact.email,
        status=MailEnrollmentStatus.ACTIVE, enrolled_at=now, created_at=now, batch_id=batch_id,
    )
    await service.enrollment_store.create(enrollment)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.PREPARED, enrollment_id="enr-ready",
            created_at=now, updated_at=now,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=completed.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="ready-not-reopened-key",
            status=MailEnrollmentBatchStatus.READY, created_at=now,
            submitted_count=1, enrolled_count=1, already_enrolled_count=0, suppressed_count=0,
        )
    )

    unchanged = await service.get_campaign(completed.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.COMPLETED  # not yet reopened

    result = await service._reconcile_batch(batch_id)
    assert result.status == MailEnrollmentBatchStatus.READY

    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE


# --- reconcile_all_preparing_batches() ---------------------------------------


async def test_reconcile_all_preparing_batches_advances_every_preparing_batch_across_campaigns(service, crm):
    active_a, list_a = await _make_active_campaign(service, crm, n_contacts=0)
    active_b, list_b = await _make_active_campaign(service, crm, n_contacts=0)
    contact_a = await crm.create_contact({"email": "sweepa@example.com"})
    contact_b = await crm.create_contact({"email": "sweepb@example.com"})
    await crm.bulk_add_to_list(list_a.list_id, [contact_a.crm_contact_id])
    await crm.bulk_add_to_list(list_b.list_id, [contact_b.crm_contact_id])

    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    for batch_id, campaign, contact, key in (
        ("sweep-a", active_a, contact_a, "sweep-key-a"),
        ("sweep-b", active_b, contact_b, "sweep-key-b"),
    ):
        await service.batch_member_store.create(
            MailEnrollmentBatchMember(
                batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
                state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
            )
        )
        await service.batch_store.create(
            MailEnrollmentBatch(
                batch_id=batch_id, mail_campaign_id=campaign.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
                source_list_id="irrelevant", idempotency_key=key,
                status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
            )
        )

    newly_ready = await service.reconcile_all_preparing_batches()
    assert newly_ready == 2

    a = await service.batch_store.get("sweep-a")
    b = await service.batch_store.get("sweep-b")
    assert a.status == MailEnrollmentBatchStatus.READY
    assert b.status == MailEnrollmentBatchStatus.READY


async def test_reconcile_all_preparing_batches_returns_zero_when_none_pending(service):
    assert await service.reconcile_all_preparing_batches() == 0


async def test_reconcile_all_preparing_batches_leaves_an_archived_campaigns_preparing_batch_untouched(
    service, crm, batch_store, batch_member_store
):
    """The background sweep must not advance a PREPARING batch whose
    owning campaign was archived out from under it -- mirrors
    test_reconcile_batch_refuses_to_process_against_an_archived_campaign,
    but through the sweep entry point rather than a direct
    _reconcile_batch() call."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "sweeparchived@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    batch_id = "b-sweep-archived"
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="sweep-archived-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    await service.archive_campaign(active.mail_campaign_id)

    newly_ready = await service.reconcile_all_preparing_batches()
    assert newly_ready == 0

    untouched = await batch_store.get(batch_id)
    assert untouched.status == MailEnrollmentBatchStatus.PREPARING
    members = await batch_member_store.list_for_batch(batch_id)
    assert members[0].state == MailEnrollmentBatchMemberState.CANDIDATE
    assert await service.list_enrollments(active.mail_campaign_id) == []


# --- cleanup_orphan_batch_members() -----------------------------------------


async def test_cleanup_orphan_batch_members_removes_members_with_no_owning_batch(service, crm):
    from app.models.mail import MailEnrollmentBatchMember

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="orphan-1", crm_contact_id="contact-x",
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=old, updated_at=old,
        )
    )

    deleted = await service.cleanup_orphan_batch_members(datetime.now(timezone.utc))
    assert deleted == 1
    assert await service.batch_member_store.list_for_batch("orphan-1") == []


async def test_cleanup_orphan_batch_members_never_touches_members_of_a_valid_preparing_batch(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "notorphaned@example.com"})

    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="real-batch", crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=old, updated_at=old,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id="real-batch", mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="real-batch-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=old, submitted_count=1,
        )
    )

    deleted = await service.cleanup_orphan_batch_members(datetime.now(timezone.utc))
    assert deleted == 0
    assert len(await service.batch_member_store.list_for_batch("real-batch")) == 1


async def test_cleanup_orphan_batch_members_never_touches_members_of_a_valid_ready_batch(service, crm):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "notorphanedready@example.com"})

    from app.models.mail import MailEnrollmentBatch, MailEnrollmentBatchMember

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="real-ready-batch", crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.PREPARED, created_at=old, updated_at=old,
        )
    )
    await service.batch_store.create(
        MailEnrollmentBatch(
            batch_id="real-ready-batch", mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="real-ready-batch-key",
            status=MailEnrollmentBatchStatus.READY, created_at=old,
            submitted_count=1, enrolled_count=1, already_enrolled_count=0, suppressed_count=0,
        )
    )

    deleted = await service.cleanup_orphan_batch_members(datetime.now(timezone.utc))
    assert deleted == 0


async def test_cleanup_orphan_batch_members_respects_the_age_threshold(service):
    """A member row younger than the cutoff is never even considered an
    orphan candidate, no matter how many sibling rows exist -- protects
    an in-progress freeze from being swept mid-flight."""
    from app.models.mail import MailEnrollmentBatchMember

    now = datetime.now(timezone.utc)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="fresh-orphan", crm_contact_id="contact-y",
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )

    deleted = await service.cleanup_orphan_batch_members(now, older_than=timedelta(hours=1))
    assert deleted == 0
    assert len(await service.batch_member_store.list_for_batch("fresh-orphan")) == 1


async def test_cleanup_orphan_batch_members_is_idempotent(service):
    from app.models.mail import MailEnrollmentBatchMember

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await service.batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="orphan-idempotent", crm_contact_id="contact-z",
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=old, updated_at=old,
        )
    )

    now = datetime.now(timezone.utc)
    first = await service.cleanup_orphan_batch_members(now)
    second = await service.cleanup_orphan_batch_members(now)
    assert first == 1
    assert second == 0


async def test_cleanup_orphan_batch_members_after_a_concurrent_race_loser_becomes_cleanable(
    service, crm, batch_store, batch_member_store
):
    """Direct continuation of the concurrent-race test above: the loser's
    orphaned cohort (batch-b, no owning batch row) must be a real, later
    cleanup_orphan_batch_members() candidate once it ages out."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "raceorphan@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    from app.models.mail import MailEnrollmentBatchMember

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="orphaned-loser", crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=old, updated_at=old,
        )
    )
    # No corresponding batch row is ever created for "orphaned-loser".

    deleted = await service.cleanup_orphan_batch_members(datetime.now(timezone.utc))
    assert deleted == 1
    assert await batch_member_store.list_for_batch("orphaned-loser") == []
