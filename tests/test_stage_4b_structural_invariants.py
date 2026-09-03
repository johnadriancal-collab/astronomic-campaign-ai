"""
Stage 4B (2026-09-03) structural invariants -- proven directly, not just
asserted in docstrings:

1. MailCampaignService receives only the read-only CrmImportResolutionReader
   Protocol, never the full writable CrmImportService.
2. MailCampaignCsvProspectService is the only NEW component that calls
   CrmImportService.commit() -- the pre-existing standalone CRM Import
   route (app/api/crm.py) is the only other real call site anywhere in
   app/.
3. _reconcile_batch() remains fully source-agnostic (CRM_LIST and
   CSV_UPLOAD go through the identical Stage 3 machinery).
4. No Gmail/provider call occurs anywhere in a CSV Add Prospects flow --
   the whole call chain completes with zero MailSenderPort implementation
   wired into the object graph at all, mirroring
   test_mail_batch_reconciliation_worker.py's own proof style.
5. Adding a batch never itself triggers a send (no sent/queued step, no
   provider-facing activity event).
6. Activity Log events produced by a CSV Add Prospects flow carry no PII
   -- only ids/counts, matching this codebase's existing "IDs only, never
   raw values" convention (see mail_sending_service.py's own privacy
   note, and CrmImportService.commit()'s own "created/updated/skipped/
   errors, filename -- never per-contact PII" event).
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.models.crm import normalize_email
from app.models.mail import MailEnrollmentBatchSource
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.mail_campaign_csv_prospect_link_store import MemoryMailCampaignCsvProspectLinkStore
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
from app.services.mail_campaign_service import CrmImportResolutionReader, MailCampaignService
from app.services.mail_sending_service import MailSendingService
from app.services import mail_campaign_service as mail_campaign_service_module
from tests.test_mail_campaign_service import _make_mailbox, _make_valid_schedule_campaign

pytestmark = pytest.mark.asyncio


# --- 1. MailCampaignService receives only the read-only reader Protocol ----


def test_mail_campaign_service_constructor_types_crm_import_reader_as_the_narrow_protocol():
    sig = inspect.signature(MailCampaignService.__init__)
    annotation = sig.parameters["crm_import_reader"].annotation
    assert annotation is CrmImportResolutionReader


def test_crm_import_resolution_reader_protocol_exposes_only_the_one_read_method():
    public_members = [m for m in dir(CrmImportResolutionReader) if not m.startswith("_")]
    assert public_members == ["list_resolved_contact_ids"]
    for write_method in ("commit", "preview", "upload", "get_batch"):
        assert not hasattr(CrmImportResolutionReader, write_method) or write_method not in public_members


def test_mail_campaign_service_module_source_never_calls_commit_preview_or_upload():
    """Source-level proof, not just a type-annotation proof: even though
    Python's structural typing can't PREVENT calling .commit() on an
    object typed as the Protocol at runtime, this file's own source never
    attempts to."""
    source = Path("app/services/mail_campaign_service.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("commit", "preview", "upload"), (
                f"mail_campaign_service.py unexpectedly calls .{node.func.attr}(...) -- "
                "this file must only ever read via CrmImportResolutionReader.list_resolved_contact_ids()"
            )


# --- 2. The orchestration service is the only NEW commit() call site -------


def test_crm_import_service_commit_is_called_from_exactly_the_two_expected_files():
    """Scans every app/ source file for a real `.commit(...)` CALL
    (never a bare mention in a docstring/comment, and never SQLite's own
    unrelated `self._conn.commit()`) and confirms the only two call sites
    anywhere in this codebase are: the pre-existing standalone CRM Import
    commit route (app/api/crm.py, unrelated to Stage 4B) and the ONE new
    orchestration service Stage 4B adds. If a future change adds a THIRD
    call site, this test starts failing -- a deliberate, narrow tripwire
    against a second, undocumented CRM-import-triggering code path ever
    appearing."""
    def _is_raw_db_connection_commit(value: ast.expr) -> bool:
        # Excludes every shape this codebase's stores use for their own
        # raw aiosqlite connection commit -- conn.commit(),
        # self._conn.commit(), self._connection.commit() -- by checking
        # whether the object reference's own name/attr contains "conn"
        # at all, which a business-object commit() call (crm_import_
        # service.commit(), service.commit()) never does.
        name = value.id if isinstance(value, ast.Name) else value.attr if isinstance(value, ast.Attribute) else ""
        return "conn" in name.lower()

    call_site_files: set[str] = set()
    for path in Path("app").rglob("*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit"
                and not _is_raw_db_connection_commit(node.func.value)
            ):
                call_site_files.add(str(path))

    assert call_site_files == {"app/api/crm.py", "app/services/mail_campaign_csv_prospect_service.py"}


# --- 3. _reconcile_batch() remains fully source-agnostic --------------------


def test_reconcile_batch_source_never_branches_on_batch_source():
    """CRM_LIST and CSV_UPLOAD must enter the IDENTICAL reconciliation
    code path -- proven by confirming _reconcile_batch()'s own source
    never references MailEnrollmentBatchSource or `.source` at all."""
    source = inspect.getsource(MailCampaignService._reconcile_batch)
    assert "MailEnrollmentBatchSource" not in source
    assert "batch.source" not in source
    assert ".source ==" not in source
    assert ".source !=" not in source


# --- 4/5. No Gmail/provider call, no send triggered by adding a batch ------


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
def mail_campaign_service(activity_log, crm, crm_import_service):
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    window_store = MemoryMailSendWindowStore()
    enrollment_step_store = MemoryMailEnrollmentStepStore()
    campaign_store = MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    suppression_store = MemoryMailSuppressionStore()
    # Note: MailSendingService itself is never constructed with (and has
    # no constructor parameter for) a MailSenderPort -- the sender is
    # only ever supplied per-call to process_one_due_step(sender=...), a
    # method no part of the Add Prospects/reconciliation call chain ever
    # calls (see test_mail_batch_reconciliation_worker.py's identical
    # reasoning for Stage 3's own equivalent proof).
    sending_service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=enrollment_step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=suppression_store, activity_log=activity_log,
    )
    return MailCampaignService(
        campaign_store=campaign_store, step_store=MemoryMailSequenceStepStore(), enrollment_store=enrollment_store,
        crm_service=crm, activity_log=activity_log, mailbox_store=mailbox_store, channel_store=channel_store,
        window_store=window_store, enrollment_step_store=enrollment_step_store, sending_service=sending_service,
        batch_store=MemoryMailEnrollmentBatchStore(), batch_member_store=MemoryMailEnrollmentBatchMemberStore(),
        suppression_store=suppression_store, crm_import_reader=crm_import_service,
    )


@pytest.fixture
def csv_prospect_service(crm_import_service, mail_campaign_service):
    return MailCampaignCsvProspectService(
        crm_import_service=crm_import_service, mail_campaign_service=mail_campaign_service,
        link_store=MemoryMailCampaignCsvProspectLinkStore(),
    )


async def test_full_csv_add_prospects_flow_completes_with_no_sender_anywhere_in_the_graph(
    mail_campaign_service, csv_prospect_service, crm, crm_import_service
):
    """The strongest possible proof: this fixture graph never constructs
    a MailSenderPort/GmailSender/RecordingSender at all, anywhere. If any
    code path this flow reaches actually needed one, this test would
    crash with an AttributeError/TypeError, not silently pass."""
    campaign, _ = await _make_valid_schedule_campaign(mail_campaign_service, crm, n_contacts=0)
    ready = await mail_campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await mail_campaign_service.activate_campaign(ready.mail_campaign_id)

    batch = await crm_import_service.upload("p.csv", b"Email\nnosender@example.com\n")
    await crm_import_service.preview(batch.import_batch_id, {"Email": "email"})

    result = await csv_prospect_service.add_prospects_from_csv(
        active.mail_campaign_id, "k1", batch.import_batch_id
    )

    assert result.status.value == "ready"
    assert result.enrolled_count == 1

    # Step 1 was MATERIALIZED (scheduled), never SENT -- MailEnrollmentStep
    # has no gmail_message_id-shaped field at all, and no send-outcome
    # status exists on a freshly-materialized step.
    enrollments = await mail_campaign_service.list_enrollments(active.mail_campaign_id)
    steps = await mail_campaign_service.enrollment_step_store.list_for_enrollment(enrollments[0].enrollment_id)
    assert len(steps) == 1
    assert steps[0].next_send_at is not None  # scheduled, not sent
    assert steps[0].status.value not in ("sent", "sending")


async def test_no_sent_or_provider_facing_activity_event_from_adding_a_batch(
    mail_campaign_service, csv_prospect_service, crm, crm_import_service, activity_log
):
    campaign, _ = await _make_valid_schedule_campaign(mail_campaign_service, crm, n_contacts=0)
    ready = await mail_campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await mail_campaign_service.activate_campaign(ready.mail_campaign_id)

    batch = await crm_import_service.upload("p.csv", b"Email\nnosend@example.com\n")
    await crm_import_service.preview(batch.import_batch_id, {"Email": "email"})
    await csv_prospect_service.add_prospects_from_csv(active.mail_campaign_id, "k1", batch.import_batch_id)

    events = await activity_log.store.list()
    forbidden_event_types = {"mail_enrollment_step.sent", "mail_enrollment.completed"}
    assert not any(e.event_type in forbidden_event_types for e in events)


# --- 6. Activity Log metadata carries no PII --------------------------------


async def test_csv_add_prospects_activity_events_carry_no_pii(
    mail_campaign_service, csv_prospect_service, crm, crm_import_service, activity_log
):
    email = "sensitive-name-in-email@example.com"
    campaign, _ = await _make_valid_schedule_campaign(mail_campaign_service, crm, n_contacts=0)
    ready = await mail_campaign_service.mark_ready(campaign.mail_campaign_id, suppressed_emails=set())
    active = await mail_campaign_service.activate_campaign(ready.mail_campaign_id)

    batch = await crm_import_service.upload("p.csv", f"Email\n{email}\n".encode())
    await crm_import_service.preview(batch.import_batch_id, {"Email": "email"})
    await csv_prospect_service.add_prospects_from_csv(active.mail_campaign_id, "k1", batch.import_batch_id)

    events = await activity_log.store.list()
    normalized_email = normalize_email(email)
    for event in events:
        assert email not in (event.summary or "")
        assert normalized_email not in (event.summary or "")
        metadata_str = str(event.metadata or {})
        assert email not in metadata_str
        assert normalized_email not in metadata_str
