"""
Stage 4B (2026-09-03) -- MailCampaignService.add_prospects(source=CSV_UPLOAD).
Covers: candidate resolution via CrmImportResolutionReader.
list_resolved_contact_ids(), the live-email blank filter, that this enters
the EXACT existing Stage 3 pipeline (frozen MailEnrollmentBatchMember
cohort, same-campaign dedupe, fresh suppression checks, Step 1
materialization, PREPARING -> READY, batch idempotency, legacy COMPLETED
reopening) with source_import_batch_id persisted, and the two new
guard rails (source_import_batch_id required; the referenced
CrmImportBatch must be COMMITTED). Mirrors test_mail_add_prospects.py's
own fixture/helper conventions exactly -- CSV-specific setup only differs
in HOW the candidate contacts are resolved (a committed CrmImportBatch
instead of a CrmContactList), not in any Stage 3 machinery, which this
file deliberately never re-tests from scratch (already covered by
test_mail_add_prospects.py for the CRM_LIST source).
"""

from datetime import datetime, timezone

import pytest

from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailEnrollmentStatus,
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
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


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
    batch_store, batch_member_store, campaign_store, enrollment_store, crm_import_service,
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
        crm_import_reader=crm_import_service,
    )


async def _make_active_campaign(service, crm, n_contacts=0):
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=n_contacts)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await service.activate_campaign(ready.mail_campaign_id)
    return active, contact_list


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


async def _commit_csv(crm_import_service, csv_text: str, mapping: dict[str, str], decisions=None) -> str:
    """Upload -> preview -> commit a small CSV through the real
    CrmImportService, returning the resulting (fully COMMITTED)
    import_batch_id -- exactly what MailCampaignCsvProspectService's own
    orchestration produces, just invoked directly for test setup."""
    batch = await crm_import_service.upload("prospects.csv", _csv_bytes(csv_text))
    await crm_import_service.preview(batch.import_batch_id, mapping)
    await crm_import_service.commit(batch.import_batch_id, decisions or {})
    return batch.import_batch_id


async def _suppress(suppression_store, email: str):
    now = datetime.now(timezone.utc)
    await suppression_store.upsert(
        MailSuppression(
            email_normalized=normalize_email(email), reason=MailSuppressionReason.MANUAL,
            created_at=now, updated_at=now, active=True,
        )
    )


# --- Candidate resolution / source requirements -----------------------------


async def test_add_prospects_csv_resolves_committed_contacts(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(
        crm_import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"}
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )

    assert batch.status == MailEnrollmentBatchStatus.READY
    assert batch.source == MailEnrollmentBatchSource.CSV_UPLOAD
    assert batch.source_import_batch_id == import_batch_id
    assert batch.submitted_count == 2
    assert batch.enrolled_count == 2


async def test_add_prospects_csv_requires_source_import_batch_id(service, crm):
    active, _ = await _make_active_campaign(service, crm)
    with pytest.raises(ValueError):
        await service.add_prospects(active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD, idempotency_key="k1")


async def test_add_prospects_csv_requires_the_batch_to_be_committed(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    batch = await crm_import_service.upload("p.csv", _csv_bytes("Email\na@example.com\n"))
    await crm_import_service.preview(batch.import_batch_id, {"Email": "email"})
    # deliberately never commit()'d

    with pytest.raises(ValueError):
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
            idempotency_key="k1", source_import_batch_id=batch.import_batch_id,
        )


async def test_add_prospects_csv_rejects_unknown_import_batch_id(service, crm):
    from app.services.crm_import_service import CrmImportBatchNotFound

    active, _ = await _make_active_campaign(service, crm)
    with pytest.raises(CrmImportBatchNotFound):
        await service.add_prospects(
            active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
            idempotency_key="k1", source_import_batch_id="does-not-exist",
        )


# --- Blank-email filtering uses LIVE contact state, not the CSV row --------


async def test_add_prospects_csv_filters_contacts_with_no_usable_live_email(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(
        crm_import_service, "First Name,Last Name\nGhost,NoEmail\n", {"First Name": "first_name", "Last Name": "last_name"}
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.submitted_count == 0
    assert batch.enrolled_count == 0


async def test_add_prospects_csv_duplicate_rows_resolving_to_one_contact_count_once(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(
        crm_import_service, "Email\nsame@example.com\nsame@example.com\n", {"Email": "email"}
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 1


async def test_add_prospects_csv_three_rows_resolving_to_one_contact_freeze_exactly_one_member(
    service, crm, crm_import_service
):
    """Explicit row-level proof, not just the aggregate submitted_count
    check above: THREE distinct CSV rows (one creating a contact, two
    more independently matching it via email and via apollo_contact_id)
    resolve to the SAME single CrmContact -- verified directly against
    MailEnrollmentBatchMember and MailEnrollment row counts, confirming
    list_resolved_contact_ids()'s dedup guarantee survives all the way
    through candidate-freezing into exactly one campaign candidate, never
    three."""
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(
        crm_import_service,
        "Email,Apollo ID\n"
        "shared@example.com,apollo-shared-1\n"  # row 0: NEW -- creates the contact with both identifiers set
        "shared@example.com,\n"  # row 1: matches row 0's contact via email
        ",apollo-shared-1\n",  # row 2: matches row 0's contact via apollo_contact_id
        {"Email": "email", "Apollo ID": "apollo_contact_id"},
    )

    # Confirm the CRM side really did resolve to exactly one contact.
    resolved_ids = await crm_import_service.list_resolved_contact_ids(import_batch_id)
    assert len(resolved_ids) == 1

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 1

    members = await service.batch_member_store.list_for_batch(batch.batch_id)
    assert len(members) == 1
    assert members[0].crm_contact_id == resolved_ids[0]

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1


# --- Same-campaign dedupe / cross-campaign independence (Stage 3 pipeline) -


async def test_add_prospects_csv_skips_a_contact_already_enrolled_in_this_campaign(service, crm, crm_import_service):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=1)
    enrollments = await service.list_enrollments(active.mail_campaign_id)
    already = enrollments[0]

    import_batch_id = await _commit_csv(
        crm_import_service, f"Email\n{already.email_at_enrollment}\n", {"Email": "email"}
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 0
    assert batch.already_enrolled_count == 1
    assert len(await service.list_enrollments(active.mail_campaign_id)) == 1  # no duplicate row


# --- Fresh suppression checks (never a caller-supplied snapshot) -----------


async def test_add_prospects_csv_suppressed_contact_enrolls_as_suppressed_with_no_step1(
    service, crm, crm_import_service, suppression_store
):
    active, _ = await _make_active_campaign(service, crm)
    await _suppress(suppression_store, "blocked@example.com")
    import_batch_id = await _commit_csv(crm_import_service, "Email\nblocked@example.com\n", {"Email": "email"})

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.enrolled_count == 1
    assert batch.suppressed_count == 1

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert enrollments[0].status == MailEnrollmentStatus.SUPPRESSED
    steps = await service.enrollment_step_store.list_for_enrollment(enrollments[0].enrollment_id)
    assert steps == []


# --- Step 1 materialization / PREPARING -> READY ----------------------------


async def test_add_prospects_csv_materializes_step1_for_a_normal_contact(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(crm_import_service, "Email\nnormal@example.com\n", {"Email": "email"})

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.status == MailEnrollmentBatchStatus.READY

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert enrollments[0].status == MailEnrollmentStatus.ACTIVE
    steps = await service.enrollment_step_store.list_for_enrollment(enrollments[0].enrollment_id)
    assert len(steps) == 1


# --- Batch idempotency (Stage 3's own, unchanged, exercised for CSV) -------


async def test_add_prospects_csv_retry_with_same_key_returns_the_same_batch(service, crm, crm_import_service):
    active, _ = await _make_active_campaign(service, crm)
    import_batch_id = await _commit_csv(crm_import_service, "Email\nonce@example.com\n", {"Email": "email"})

    first = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="same-key", source_import_batch_id=import_batch_id,
    )
    second = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="same-key", source_import_batch_id=import_batch_id,
    )
    assert first.batch_id == second.batch_id
    assert second.enrolled_count == 1
    assert len(await service.list_enrollments(active.mail_campaign_id)) == 1


# --- Legacy COMPLETED reopening (Stage 3's own, unchanged, exercised for CSV) -


async def test_add_prospects_csv_reopens_a_completed_campaign_with_a_genuinely_new_enrollment(
    service, crm, crm_import_service, campaign_store
):
    active, _ = await _make_active_campaign(service, crm)
    completed = active.model_copy(update={"status": MailCampaignStatus.COMPLETED})
    await campaign_store.save(completed)

    import_batch_id = await _commit_csv(crm_import_service, "Email\nrevive@example.com\n", {"Email": "email"})

    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.enrolled_count == 1

    reopened = await service.get_campaign(completed.mail_campaign_id)
    assert reopened.status == MailCampaignStatus.ACTIVE


async def test_add_prospects_csv_leaves_a_completed_campaign_completed_when_nothing_genuinely_new(
    service, crm, crm_import_service, campaign_store
):
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=1)
    enrollments = await service.list_enrollments(active.mail_campaign_id)
    already_email = enrollments[0].email_at_enrollment
    completed = active.model_copy(update={"status": MailCampaignStatus.COMPLETED})
    await campaign_store.save(completed)

    import_batch_id = await _commit_csv(crm_import_service, f"Email\n{already_email}\n", {"Email": "email"})

    batch = await service.add_prospects(
        completed.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.enrolled_count == 0

    unchanged = await service.get_campaign(completed.mail_campaign_id)
    assert unchanged.status == MailCampaignStatus.COMPLETED


# --- Human-approved POSSIBLE_DUPLICATE override still enters the pipeline --


async def test_add_prospects_csv_includes_a_human_approved_possible_duplicate_override(
    service, crm, crm_import_service
):
    active, _ = await _make_active_campaign(service, crm)
    await crm.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    import_batch_id = await _commit_csv(
        crm_import_service, "First Name,Last Name,Company,Email\nAda,Lovelace,Acme,newada@example.com\n",
        {"First Name": "first_name", "Last Name": "last_name", "Company": "company", "Email": "email"},
        decisions={0: "create"},
    )

    batch = await service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
        idempotency_key="k1", source_import_batch_id=import_batch_id,
    )
    assert batch.submitted_count == 1
    assert batch.enrolled_count == 1
