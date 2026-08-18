"""
Tests for MailCampaignService -- Astronomic Mail Phase 1's campaign/
sequence/audience/review orchestration. Uses in-memory stores throughout,
same convention as test_itf_ingestion_service.py's fixtures. A real
CrmService() (in-memory) provides the audience (CrmContactList/CrmContact)
this service reads from.
"""

import pytest

from app.models.mail import MailCampaignStatus, MailEnrollmentStatus, MailScheduleValidationError
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_sequence_step_store import (
    DuplicateMailSequenceStepNumberError,
    MemoryMailSequenceStepStore,
)
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import (
    InvalidMailTemplateVariableError,
    MailCampaignInvalidTransitionError,
    MailCampaignNotEditableError,
    MailCampaignNotFound,
    MailCampaignNotReadyError,
    MailCampaignService,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def crm():
    return CrmService()


@pytest.fixture
def service(crm, activity_log):
    return MailCampaignService(
        campaign_store=MemoryMailCampaignStore(),
        step_store=MemoryMailSequenceStepStore(),
        enrollment_store=MemoryMailEnrollmentStore(),
        crm_service=crm,
        activity_log=activity_log,
    )


async def _make_valid_schedule_campaign(service, crm, name="Q1 Outreach", n_contacts=3):
    """Helper: a DRAFT campaign with a real audience list, one step, and a
    valid complete schedule -- everything mark_ready() requires."""
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
    return campaign, contact_list


# --- Campaign CRUD -----------------------------------------------------


async def test_create_draft_campaign(service):
    campaign = await service.create_campaign("My Campaign")
    assert campaign.status == MailCampaignStatus.DRAFT
    assert campaign.name == "My Campaign"
    assert campaign.source_list_id is None


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


async def test_active_is_not_even_a_valid_enum_value():
    """Structurally, not just behaviorally, unreachable -- see
    MailCampaignStatus's docstring."""
    with pytest.raises(ValueError):
        MailCampaignStatus("active")
    with pytest.raises(ValueError):
        MailCampaignStatus("paused")
    with pytest.raises(ValueError):
        MailCampaignStatus("completed")


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


async def test_impossible_active_transition_no_method_exists(service):
    """There is no activate()/pause()/complete() method on this service at
    all -- confirmed by introspection rather than trying to call one."""
    assert not hasattr(service, "activate_campaign")
    assert not hasattr(service, "pause_campaign")
    assert not hasattr(service, "launch_campaign")
    assert not hasattr(service, "send_campaign")


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
