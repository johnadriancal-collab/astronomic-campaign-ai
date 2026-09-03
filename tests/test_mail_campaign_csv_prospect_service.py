"""
MailCampaignCsvProspectService -- Stage 4B (2026-09-03). Covers the full
approved crash/idempotency matrix: durable link resolution/creation
(including the concurrent-race and "different supplied import_batch_id"
cases), the approved ordering (link -> preflight -> commit -> add_prospects),
the documented eligibility race, and every named retry/crash boundary.
Deliberately does NOT re-test Stage 3's own PREPARING/READY reconciliation
internals from scratch (already exhaustively covered by
test_mail_add_prospects.py/test_mail_add_prospects_csv.py) -- these tests
prove the ORCHESTRATION layer resolves to the right state at each
boundary, trusting Stage 3/4A's own already-proven machinery underneath.
"""

from datetime import datetime, timezone

import pytest

from app.models.crm import normalize_email
from app.models.mail import (
    MailCampaignStatus,
    MailEnrollmentBatch,
    MailEnrollmentBatchMember,
    MailEnrollmentBatchMemberState,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailSuppression,
    MailSuppressionReason,
)
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_csv_prospect_link_store import (
    DuplicateCsvProspectLinkError,
    MemoryMailCampaignCsvProspectLinkStore,
)
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
from app.services.mail_campaign_csv_prospect_service import MailCampaignCsvProspectService
from app.services.mail_campaign_service import MailCampaignNotEligibleForProspectsError, MailCampaignService
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
def link_store():
    return MemoryMailCampaignCsvProspectLinkStore()


@pytest.fixture
def mail_campaign_service(
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


@pytest.fixture
def service(crm_import_service, mail_campaign_service, link_store):
    return MailCampaignCsvProspectService(
        crm_import_service=crm_import_service, mail_campaign_service=mail_campaign_service, link_store=link_store,
    )


async def _make_active_campaign(mail_campaign_service, crm, n_contacts=0):
    campaign, contact_list = await _make_valid_schedule_campaign(mail_campaign_service, crm, n_contacts=n_contacts)
    ready = await mail_campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await mail_campaign_service.activate_campaign(ready.mail_campaign_id)
    return active, contact_list


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


async def _upload_and_preview(crm_import_service, csv_text: str, mapping: dict[str, str]) -> str:
    """Upload+preview only (NOT committed) -- exactly the state the
    frontend hands off to the orchestration endpoint after its own
    steps 1-5 (see MailCampaignCsvProspectService's module docstring)."""
    batch = await crm_import_service.upload("prospects.csv", _csv_bytes(csv_text))
    await crm_import_service.preview(batch.import_batch_id, mapping)
    return batch.import_batch_id


async def _suppress(suppression_store, email: str):
    now = datetime.now(timezone.utc)
    await suppression_store.upsert(
        MailSuppression(
            email_normalized=normalize_email(email), reason=MailSuppressionReason.MANUAL,
            created_at=now, updated_at=now, active=True,
        )
    )


# --- 1. Same campaign/key + same import -> same operation ------------------


async def test_retry_with_same_key_and_same_import_returns_the_same_batch(service, mail_campaign_service, crm, crm_import_service):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    first = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    second = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    assert first.batch_id == second.batch_id
    assert second.enrolled_count == 1
    assert len(await mail_campaign_service.list_enrollments(active.mail_campaign_id)) == 1


# --- 2. Same campaign/key + DIFFERENT supplied import -> original wins -----


async def test_retry_with_a_different_supplied_import_batch_id_is_ignored(service, mail_campaign_service, crm, crm_import_service):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    original_import = await _upload_and_preview(crm_import_service, "Email\noriginal@example.com\n", {"Email": "email"})
    different_import = await _upload_and_preview(crm_import_service, "Email\ndifferent@example.com\n", {"Email": "email"})

    first = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", original_import)
    second = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", different_import)  # different id!

    assert first.batch_id == second.batch_id
    assert second.source_import_batch_id == original_import  # NOT different_import

    link = await service.link_store.get_by_idempotency_key(active.mail_campaign_id, "key-1")
    assert link.import_batch_id == original_import

    enrollments = await mail_campaign_service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1
    assert enrollments[0].email_at_enrollment == "original@example.com"  # never the different CSV's contact

    # The "different" import was never committed by this operation at all.
    different_batch = await crm_import_service.get_batch(different_import)
    assert different_batch.status.value == "mapped"  # still just previewed, never committed


# --- 3. Concurrent link creation ---------------------------------------------


async def test_concurrent_link_creation_only_one_wins_and_both_calls_converge(
    service, mail_campaign_service, crm, crm_import_service, link_store
):
    """Simulates two 'simultaneous' first-ever calls for the same
    (campaign, key) -- mirrors Stage 3's own concurrent-batch-creation
    race test pattern. Only one link_store.create() call can win; the
    loser must look up and use the winner's own linked import_batch_id."""
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_a = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})
    import_b = await _upload_and_preview(crm_import_service, "Email\nb@example.com\n", {"Email": "email"})

    # Winner creates the link first (as if it "arrived" a moment earlier).
    from app.models.mail import MailCampaignCsvProspectLink

    await link_store.create(
        MailCampaignCsvProspectLink(
            mail_campaign_id=active.mail_campaign_id, idempotency_key="race-key",
            import_batch_id=import_a, created_at=datetime.now(timezone.utc),
        )
    )

    # The "loser" call proceeds with its own (different) import_batch_id --
    # add_prospects_from_csv() must detect the existing link and use ITS
    # import_batch_id (import_a), never import_b.
    result = await service.add_prospects_from_csv(active.mail_campaign_id, "race-key", import_b)

    assert result.source_import_batch_id == import_a
    enrollments = await mail_campaign_service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1
    assert enrollments[0].email_at_enrollment == "a@example.com"

    losing_batch = await crm_import_service.get_batch(import_b)
    assert losing_batch.status.value == "mapped"  # never committed by the losing call


async def test_link_store_itself_rejects_a_genuine_duplicate_create(link_store):
    """Direct store-level proof backing the service-level race test above."""
    from app.models.mail import MailCampaignCsvProspectLink

    now = datetime.now(timezone.utc)
    await link_store.create(
        MailCampaignCsvProspectLink(mail_campaign_id="c1", idempotency_key="k1", import_batch_id="b1", created_at=now)
    )
    with pytest.raises(DuplicateCsvProspectLinkError):
        await link_store.create(
            MailCampaignCsvProspectLink(mail_campaign_id="c1", idempotency_key="k1", import_batch_id="b2", created_at=now)
        )


# --- 4. Ineligible campaign before commit -> no CRM commit ------------------


async def test_ineligible_campaign_is_rejected_before_any_crm_commit(service, mail_campaign_service, crm, crm_import_service):
    draft_campaign, _ = await _make_valid_schedule_campaign(mail_campaign_service, crm)  # never marked ready/activated
    contacts_before = (await crm.list_contacts()).total  # _make_valid_schedule_campaign itself seeds some contacts
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects_from_csv(draft_campaign.mail_campaign_id, "key-1", import_batch_id)

    # The preflight ran BEFORE commit() -- the CSV import was never committed.
    batch = await crm_import_service.get_batch(import_batch_id)
    assert batch.status.value == "mapped"
    assert (await crm.list_contacts()).total == contacts_before  # no new contact from the never-committed CSV


# --- 5. Eligible preflight -> archived before add -> CRM committed, no batch -


async def test_archived_between_preflight_and_add_prospects_commits_crm_but_creates_no_campaign_batch(
    service, mail_campaign_service, crm, crm_import_service, monkeypatch
):
    """The documented, accepted race: preflight sees ACTIVE, but the
    campaign is archived (by 'someone else') during the commit() window,
    before add_prospects()'s own authoritative check runs."""
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    original_commit = crm_import_service.commit

    async def commit_then_archive(*args, **kwargs):
        result = await original_commit(*args, **kwargs)
        await mail_campaign_service.archive_campaign(active.mail_campaign_id)
        return result

    monkeypatch.setattr(crm_import_service, "commit", commit_then_archive)

    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    # The CRM commit DID land -- a real contact exists.
    batch = await crm_import_service.get_batch(import_batch_id)
    assert batch.status.value == "committed"
    assert (await crm.list_contacts()).total == 1

    # But no campaign batch/enrollment was ever created.
    archived_campaign = await mail_campaign_service.get_campaign(active.mail_campaign_id)
    assert archived_campaign.status == MailCampaignStatus.ARCHIVED
    assert await mail_campaign_service.list_batches(active.mail_campaign_id) == []
    assert await mail_campaign_service.list_enrollments(active.mail_campaign_id) == []

    # A retry of the SAME (campaign, key) reuses the same link/import --
    # and, since the campaign is now permanently ARCHIVED, permanently
    # re-rejects, exactly as documented (never a distributed-transaction
    # "fix", never a second CRM commit).
    with pytest.raises(MailCampaignNotEligibleForProspectsError):
        await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert (await crm.list_contacts()).total == 1  # still exactly one -- no re-commit


# --- 6. Retry after interrupted COMMITTING ----------------------------------


async def test_retry_resumes_an_interrupted_committing_import(service, mail_campaign_service, crm, crm_import_service):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(
        crm_import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"}
    )

    # Simulate row 0 already durably "created" by an interrupted first
    # attempt -- status COMMITTING, one row resolved, one not (mirrors
    # Stage 4A's own crash-boundary test convention exactly).
    batch = await crm_import_service.get_batch(import_batch_id)
    contact = await crm.create_contact_from_import(batch.preview[0].mapped_fields)
    batch.preview[0] = batch.preview[0].model_copy(update={"commit_outcome": "created", "resolved_contact_id": contact.crm_contact_id})
    from app.models.crm import CrmImportBatchStatus

    batch.status = CrmImportBatchStatus.COMMITTING
    await crm_import_service.batch_store.save(batch)

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    assert result.status == MailEnrollmentBatchStatus.READY
    assert result.submitted_count == 2
    assert result.enrolled_count == 2
    finished_import = await crm_import_service.get_batch(import_batch_id)
    assert finished_import.status == CrmImportBatchStatus.COMMITTED
    assert (await crm.list_contacts()).total == 2  # row 0's contact never duplicated


# --- 7. Crash/loss after COMMITTED but before MailEnrollmentBatch creation -


async def test_retry_after_committed_but_before_campaign_batch_creates_it_cleanly(
    service, mail_campaign_service, crm, crm_import_service, link_store
):
    """The critical gap this whole design exists to close: the CRM import
    is ALREADY fully committed and the link ALREADY exists (as if a prior
    orchestration call got this far and then the process died), but
    add_prospects() was never reached."""
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})
    await crm_import_service.commit(import_batch_id)  # already fully committed

    from app.models.mail import MailCampaignCsvProspectLink

    await link_store.create(
        MailCampaignCsvProspectLink(
            mail_campaign_id=active.mail_campaign_id, idempotency_key="key-1",
            import_batch_id=import_batch_id, created_at=datetime.now(timezone.utc),
        )
    )
    assert await mail_campaign_service.list_batches(active.mail_campaign_id) == []  # not created yet

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    assert result.status == MailEnrollmentBatchStatus.READY
    assert result.enrolled_count == 1
    assert (await crm.list_contacts()).total == 1  # commit() was a no-op the second time through


# --- 8. Retry with a PREPARING campaign batch -------------------------------


async def test_retry_resumes_a_preparing_campaign_batch(service, mail_campaign_service, crm, crm_import_service, batch_store, batch_member_store, link_store):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(
        crm_import_service, "Email\na@example.com\nb@example.com\n", {"Email": "email"}
    )
    await crm_import_service.commit(import_batch_id)
    resolved_ids = await crm_import_service.list_resolved_contact_ids(import_batch_id)

    from app.models.mail import MailCampaignCsvProspectLink

    await link_store.create(
        MailCampaignCsvProspectLink(
            mail_campaign_id=active.mail_campaign_id, idempotency_key="key-1",
            import_batch_id=import_batch_id, created_at=datetime.now(timezone.utc),
        )
    )

    # Simulate a campaign batch already frozen in PREPARING (one member
    # untouched) -- mirrors Stage 3's own crash-boundary test convention.
    now = datetime.now(timezone.utc)
    campaign_batch_id = "b-preparing"
    for contact_id in resolved_ids:
        await batch_member_store.create(
            MailEnrollmentBatchMember(
                batch_id=campaign_batch_id, crm_contact_id=contact_id,
                state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
            )
        )
    await batch_store.create(
        MailEnrollmentBatch(
            batch_id=campaign_batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CSV_UPLOAD,
            source_import_batch_id=import_batch_id, idempotency_key="key-1",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=len(resolved_ids),
        )
    )

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    assert result.batch_id == campaign_batch_id
    assert result.status == MailEnrollmentBatchStatus.READY
    assert result.enrolled_count == 2


# --- 9. Retry after READY ----------------------------------------------------


async def test_retry_after_ready_is_a_pure_no_op(service, mail_campaign_service, crm, crm_import_service):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    first = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert first.status == MailEnrollmentBatchStatus.READY

    second = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert second == first
    assert len(await mail_campaign_service.list_enrollments(active.mail_campaign_id)) == 1


# --- 10. Lost-response equivalent retry (client never saw the response) ----


async def test_lost_response_retry_is_indistinguishable_from_a_normal_retry(service, mail_campaign_service, crm, crm_import_service):
    """A 'lost response' is, from the server's perspective, just another
    call with the same idempotency_key -- no special handling exists or
    is needed beyond what the same-key tests above already prove."""
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    real_result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    # The client never received `real_result` (simulated lost response) and retries.
    retried_result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)

    assert retried_result == real_result


# --- 11-14: counted correctly / filtered correctly / suppression / provenance -


async def test_existing_and_new_contacts_are_counted_correctly_through_orchestration(
    service, mail_campaign_service, crm, crm_import_service
):
    active, contact_list = await _make_active_campaign(mail_campaign_service, crm, n_contacts=1)
    enrollments = await mail_campaign_service.list_enrollments(active.mail_campaign_id)
    already_email = enrollments[0].email_at_enrollment

    import_batch_id = await _upload_and_preview(
        crm_import_service, f"Email\n{already_email}\nbrandnew@example.com\n", {"Email": "email"}
    )

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert result.submitted_count == 2
    assert result.enrolled_count == 1
    assert result.already_enrolled_count == 1


async def test_blank_and_unusable_contacts_are_filtered_correctly_through_orchestration(
    service, mail_campaign_service, crm, crm_import_service
):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(
        crm_import_service,
        "Email,First Name,Last Name\nusable@example.com,,\n,NoEmail,Person\n",
        {"Email": "email", "First Name": "first_name", "Last Name": "last_name"},
    )

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert result.submitted_count == 1
    assert result.enrolled_count == 1


async def test_suppression_semantics_preserved_through_orchestration(
    service, mail_campaign_service, crm, crm_import_service, suppression_store
):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    await _suppress(suppression_store, "blocked@example.com")
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\nblocked@example.com\n", {"Email": "email"})

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert result.enrolled_count == 1
    assert result.suppressed_count == 1
    enrollments = await mail_campaign_service.list_enrollments(active.mail_campaign_id)
    assert enrollments[0].status.value == "suppressed"


async def test_source_import_batch_id_is_persisted_through_orchestration(service, mail_campaign_service, crm, crm_import_service):
    active, _ = await _make_active_campaign(mail_campaign_service, crm)
    import_batch_id = await _upload_and_preview(crm_import_service, "Email\na@example.com\n", {"Email": "email"})

    result = await service.add_prospects_from_csv(active.mail_campaign_id, "key-1", import_batch_id)
    assert result.source == MailEnrollmentBatchSource.CSV_UPLOAD
    assert result.source_import_batch_id == import_batch_id


# --- 15. CRM List path is completely unaffected -----------------------------


async def test_crm_list_add_prospects_is_unaffected_by_the_csv_orchestration_service(mail_campaign_service, crm):
    """Regression proof: MailCampaignService.add_prospects(source=crm_list)
    behaves exactly as Stage 3 shipped it -- the CSV orchestration service
    is never involved and no shared state leaks between the two paths."""
    active, contact_list = await _make_active_campaign(mail_campaign_service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "listcontact@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    result = await mail_campaign_service.add_prospects(
        active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
        idempotency_key="crm-list-key", source_list_id=contact_list.list_id,
    )
    assert result.source == MailEnrollmentBatchSource.CRM_LIST
    assert result.enrolled_count == 1
