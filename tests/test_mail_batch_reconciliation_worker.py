"""
MailBatchReconciliationWorker -- Stage 3 (2026-09-03). This worker is pure
campaign/enrollment bookkeeping/recovery (reconcile_all_preparing_batches()
+ cleanup_orphan_batch_members()), deliberately independent of
settings.mail_sending_engine_enabled, and MUST NEVER make a Gmail/provider
call -- see that module's own docstring. This file proves that both
structurally (the module never imports anything sender/provider-shaped,
and its constructor accepts nothing sender-shaped) and behaviorally (a
full reconciliation sweep that materializes real Step 1 rows completes
successfully with NO MailSenderPort implementation wired ANYWHERE in the
object graph at all -- if any code path this worker reaches actually
needed to invoke a sender, constructing this fixture graph without one
would be impossible or run_once() would crash, not silently succeed).
"""

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.crm import normalize_email
from app.models.mail import (
    MailEnrollmentBatch,
    MailEnrollmentBatchMember,
    MailEnrollmentBatchMemberState,
    MailEnrollmentBatchSource,
    MailEnrollmentBatchStatus,
    MailEnrollmentStatus,
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
from app.services.mail_batch_reconciliation_worker import MailBatchReconciliationWorker
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService
from tests.test_mail_campaign_service import _make_mailbox, _make_valid_schedule_campaign

pytestmark = pytest.mark.asyncio

_MODULE_PATH = "app/services/mail_batch_reconciliation_worker.py"

# Anything importing one of these would be capable of reaching a real
# sender/provider -- none of them may ever appear in this worker's own
# import list, at any level (module or symbol).
_FORBIDDEN_IMPORT_SUBSTRINGS = ("sender", "gmail", "google", "smtp", "mail_execution_worker")


def test_worker_module_imports_nothing_sender_or_provider_shaped():
    source = Path(_MODULE_PATH).read_text()
    tree = ast.parse(source)
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names.append(module)
            imported_names.extend(f"{module}.{alias.name}" for alias in node.names)

    for name in imported_names:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS:
            assert forbidden not in lowered, f"{_MODULE_PATH} imports {name!r} (matches forbidden {forbidden!r})"


def test_worker_constructor_accepts_nothing_sender_shaped():
    """The ONLY two constructor parameters are mail_campaign_service and
    poll_interval_seconds -- if a sender were ever wired into this worker,
    it would have to show up here."""
    signature = inspect.signature(MailBatchReconciliationWorker.__init__)
    param_names = set(signature.parameters) - {"self"}
    assert param_names == {"mail_campaign_service", "poll_interval_seconds"}


def test_worker_instance_has_no_sender_shaped_attribute():
    service_double = object()  # doesn't even need to be a real MailCampaignService for this check
    worker = MailBatchReconciliationWorker(mail_campaign_service=service_double)  # type: ignore[arg-type]
    for attr_name in vars(worker):
        lowered = attr_name.lower()
        assert "sender" not in lowered and "gmail" not in lowered, (
            f"MailBatchReconciliationWorker instance unexpectedly has a sender-shaped attribute: {attr_name!r}"
        )


# --- Behavioral: a real sweep succeeds with NO sender anywhere in the graph -


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
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def service(
    crm, activity_log, mailbox_store, channel_store, window_store, enrollment_step_store, suppression_store,
    batch_store, batch_member_store, campaign_store, enrollment_store,
):
    # Note: MailSendingService itself is never constructed with (and has no
    # constructor parameter for) a MailSenderPort -- see mail_sending_service.py,
    # where the sender is only ever supplied per-call to
    # process_one_due_step(sender=...), a method this worker's entire call
    # chain (reconcile_all_preparing_batches -> _reconcile_batch ->
    # create_step1_execution) never calls.
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
        crm_import_reader=CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore()),
    )


@pytest.fixture(autouse=True)
def mail_sending_engine_enabled(monkeypatch):
    import app.services.mail_campaign_service as mail_campaign_service_module

    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", True)


async def _make_active_campaign(service, crm, n_contacts=0):
    campaign, contact_list = await _make_valid_schedule_campaign(service, crm, n_contacts=n_contacts)
    ready = await service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await service.activate_campaign(ready.mail_campaign_id)
    return active, contact_list


async def test_run_once_reconciles_a_preparing_batch_and_materializes_step1_with_no_sender_anywhere(
    service, crm, batch_store, batch_member_store,
):
    """The strongest possible proof: this fixture graph never constructs a
    MailSenderPort/GmailSender/RecordingSender at all -- not in the
    worker, not in MailSendingService, not anywhere. If run_once() somehow
    needed one to materialize a Step 1 execution, this test would crash
    with an AttributeError/TypeError, not silently pass."""
    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    contact = await crm.create_contact({"email": "sweepcandidate@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    now = datetime.now(timezone.utc)
    batch_id = "worker-sweep-batch"
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="worker-sweep-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    worker = MailBatchReconciliationWorker(mail_campaign_service=service)
    reconciled, deleted = await worker.run_once()

    assert reconciled == 1
    assert deleted == 0

    finished = await batch_store.get(batch_id)
    assert finished.status == MailEnrollmentBatchStatus.READY
    assert finished.enrolled_count == 1

    enrollments = await service.list_enrollments(active.mail_campaign_id)
    assert len(enrollments) == 1
    assert enrollments[0].status == MailEnrollmentStatus.ACTIVE  # Step 1 materialized, never actually sent

    steps = await service.enrollment_step_store.list_for_enrollment(enrollments[0].enrollment_id)
    assert len(steps) == 1
    assert steps[0].next_send_at is not None  # scheduled, not sent -- no gmail_message_id-shaped field exists at all


async def test_run_once_also_cleans_up_orphan_members_in_the_same_sweep(service, batch_member_store):
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id="worker-orphan", crm_contact_id="contact-orphan",
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=old, updated_at=old,
        )
    )

    worker = MailBatchReconciliationWorker(mail_campaign_service=service)
    reconciled, deleted = await worker.run_once()

    assert reconciled == 0
    assert deleted == 1
    assert await batch_member_store.list_for_batch("worker-orphan") == []


async def test_run_once_is_independent_of_the_sending_engine_flag(service, crm, batch_store, batch_member_store, monkeypatch):
    """Deliberately DISABLES the sending engine flag (the opposite of the
    autouse fixture above) and confirms reconciliation still runs fully --
    unlike MailExecutionWorker.start(), this worker has no such gate at
    all."""
    import app.services.mail_campaign_service as mail_campaign_service_module

    active, contact_list = await _make_active_campaign(service, crm, n_contacts=0)
    monkeypatch.setattr(mail_campaign_service_module.settings, "mail_sending_engine_enabled", False)

    contact = await crm.create_contact({"email": "disabledengine@example.com"})
    await crm.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    now = datetime.now(timezone.utc)
    batch_id = "worker-disabled-engine-batch"
    await batch_member_store.create(
        MailEnrollmentBatchMember(
            batch_id=batch_id, crm_contact_id=contact.crm_contact_id,
            state=MailEnrollmentBatchMemberState.CANDIDATE, created_at=now, updated_at=now,
        )
    )
    await batch_store.create(
        MailEnrollmentBatch(
            batch_id=batch_id, mail_campaign_id=active.mail_campaign_id, source=MailEnrollmentBatchSource.CRM_LIST,
            source_list_id=contact_list.list_id, idempotency_key="disabled-engine-key",
            status=MailEnrollmentBatchStatus.PREPARING, created_at=now, submitted_count=1,
        )
    )

    worker = MailBatchReconciliationWorker(mail_campaign_service=service)
    reconciled, _deleted = await worker.run_once()

    assert reconciled == 1
    finished = await batch_store.get(batch_id)
    assert finished.status == MailEnrollmentBatchStatus.READY


async def test_start_and_stop_round_trip_without_a_sender(service):
    """Confirms the actual asyncio loop wrapper (start()/stop()) also
    works end-to-end with zero sender anywhere -- not just run_once()
    called directly."""
    worker = MailBatchReconciliationWorker(mail_campaign_service=service, poll_interval_seconds=3600)
    worker.start()
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None


async def test_starting_twice_is_a_no_op(service):
    worker = MailBatchReconciliationWorker(mail_campaign_service=service, poll_interval_seconds=3600)
    worker.start()
    first_task = worker._task
    worker.start()
    assert worker._task is first_task
    await worker.stop()
