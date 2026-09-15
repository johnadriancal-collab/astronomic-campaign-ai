"""
P0-2 -- minimal failed/unknown send visibility (2026-09-15).

GET /mail/campaigns/{id}/execution-steps -- MailCampaignService.
list_execution_steps() and its thin route wrapper. Reuses tests/
test_mail_api.py's exact client/campaign_service fixture wiring (see that
file's own docstring on why there's no auth here).
"""

from datetime import datetime, time, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail import router as mail_router
from app.dependencies import get_mail_campaign_csv_prospect_service, get_mail_campaign_service, get_mail_suppression_service
from app.models.crm import CrmContact
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
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
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import MailSendingService
from app.services.mail_suppression_service import MailSuppressionService

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
TZ = "America/Chicago"


def all_day_windows(campaign_id: str) -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{campaign_id}-{d}", mail_campaign_id=campaign_id, day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


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
def crm_import_service(crm):
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def campaign_service(crm, mailbox_store, channel_store, window_store, crm_import_service):
    campaign_store = MemoryMailCampaignStore()
    enrollment_store = MemoryMailEnrollmentStore()
    enrollment_step_store = MemoryMailEnrollmentStepStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    sending_service = MailSendingService(
        campaign_store=campaign_store,
        enrollment_store=enrollment_store,
        step_store=enrollment_step_store,
        mailbox_store=mailbox_store,
        channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(),
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
        batch_store=MemoryMailEnrollmentBatchStore(),
        batch_member_store=MemoryMailEnrollmentBatchMemberStore(),
        suppression_store=MemoryMailSuppressionStore(),
        crm_import_reader=crm_import_service,
    )


@pytest.fixture
def suppression_service():
    return MailSuppressionService(store=MemoryMailSuppressionStore(), activity_log=ActivityLogService(MemoryActivityEventStore()))


@pytest.fixture
def csv_prospect_service(crm_import_service, campaign_service):
    return MailCampaignCsvProspectService(
        crm_import_service=crm_import_service,
        mail_campaign_service=campaign_service,
        link_store=MemoryMailCampaignCsvProspectLinkStore(),
    )


@pytest.fixture
def client(campaign_service, suppression_service, csv_prospect_service):
    app = FastAPI()
    app.include_router(mail_router)
    app.dependency_overrides[get_mail_campaign_service] = lambda: campaign_service
    app.dependency_overrides[get_mail_suppression_service] = lambda: suppression_service
    app.dependency_overrides[get_mail_campaign_csv_prospect_service] = lambda: csv_prospect_service
    with TestClient(app) as c:
        yield c


async def _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store) -> str:
    """Directly constructs one ACTIVE campaign, one connected mailbox, one
    CrmContact, and three MailEnrollmentStep rows in FAILED/UNKNOWN/SENT
    states -- bypassing activation (the engine stays disabled; nothing
    here goes through prepare_and_send_step()/the worker)."""
    campaign_id = "c1"
    await campaign_service.campaign_store.create(
        MailCampaign(mail_campaign_id=campaign_id, name="Visibility Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await mailbox_store.create(
        Mailbox(mailbox_id="mbx-1", provider=MailboxProvider.GOOGLE, email="mbx-1@astronomic.com", display_name=None,
                status=MailboxStatus.CONNECTED, google_user_id="g-1", granted_scopes=["https://www.googleapis.com/auth/gmail.send"],
                connected_at=NOW, updated_at=NOW)
    )
    await channel_store.replace_for_campaign(campaign_id, ["mbx-1"])
    await campaign_service.crm_service.contact_store.create(
        CrmContact(crm_contact_id="contact-1", created_at=NOW, updated_at=NOW, first_name="Ada", last_name="Lovelace")
    )

    step1 = MailSequenceStep(step_id="s1", mail_campaign_id=campaign_id, step_number=1, subject="Subj", body="Body.", delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW)
    await campaign_service.step_store.create(step1)

    rows = []
    for enrollment_id, crm_contact_id, email in [
        # MemoryMailEnrollmentStore is keyed by (mail_campaign_id,
        # crm_contact_id) -- see that store's own docstring -- so each
        # row here needs a DISTINCT crm_contact_id or the second create()
        # for a repeated contact silently no-ops (a real dedup rule, not
        # a bug: one CrmContact enrolls in a given campaign at most once).
        ("e-failed", "contact-1", "failed@example.com"),
        ("e-unknown", "does-not-exist", "unknown@example.com"),
        ("e-sent", "contact-2", "sent@example.com"),
    ]:
        enrollment = MailEnrollment(
            enrollment_id=enrollment_id, mail_campaign_id=campaign_id, crm_contact_id=crm_contact_id,
            email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
        )
        await campaign_service.enrollment_store.create(enrollment)
        row = await campaign_service.sending_service.create_step1_execution(
            enrollment=enrollment, step1=step1, windows=all_day_windows(campaign_id), timezone_name=TZ, now=NOW
        )
        rows.append(row)

    failed_row, unknown_row, sent_row = rows
    await campaign_service.enrollment_step_store.try_transition(
        failed_row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED,
        failed_row.model_copy(update={"status": MailEnrollmentStepStatus.FAILED, "last_error": "MailPersonalizationError: unresolved variable", "updated_at": NOW}),
    )
    await campaign_service.enrollment_step_store.try_transition(
        unknown_row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED,
        unknown_row.model_copy(update={"status": MailEnrollmentStepStatus.UNKNOWN, "gmail_message_id": "gmail-123", "updated_at": NOW}),
    )
    await campaign_service.enrollment_step_store.try_transition(
        sent_row.enrollment_step_id, MailEnrollmentStepStatus.QUEUED,
        sent_row.model_copy(update={"status": MailEnrollmentStepStatus.SENT, "sent_at": NOW, "gmail_message_id": "gmail-456", "updated_at": NOW}),
    )
    return campaign_id


@pytest.mark.asyncio
async def test_list_execution_steps_returns_every_status_by_default(campaign_service, mailbox_store, channel_store, client):
    campaign_id = await _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store)

    resp = client.get(f"/mail/campaigns/{campaign_id}/execution-steps")

    assert resp.status_code == 200
    body = resp.json()
    assert {row["status"] for row in body} == {"failed", "unknown", "sent"}
    assert len(body) == 3


@pytest.mark.asyncio
async def test_list_execution_steps_filters_to_failed_and_unknown(campaign_service, mailbox_store, channel_store, client):
    campaign_id = await _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store)

    resp = client.get(f"/mail/campaigns/{campaign_id}/execution-steps", params={"status": ["failed", "unknown"]})

    assert resp.status_code == 200
    body = resp.json()
    assert {row["status"] for row in body} == {"failed", "unknown"}
    assert len(body) == 2


@pytest.mark.asyncio
async def test_failed_row_shows_prospect_email_campaign_step_timestamp_status_and_safe_error(
    campaign_service, mailbox_store, channel_store, client
):
    campaign_id = await _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store)

    resp = client.get(f"/mail/campaigns/{campaign_id}/execution-steps", params={"status": "failed"})
    row = resp.json()[0]

    assert row["prospect_email"] == "failed@example.com"
    assert row["prospect_name"] == "Ada Lovelace"
    assert row["mail_campaign_id"] == campaign_id
    assert row["step_number"] == 1
    assert row["status"] == "failed"
    assert row["updated_at"] is not None
    assert row["last_error"] == "MailPersonalizationError: unresolved variable"


@pytest.mark.asyncio
async def test_unknown_row_shows_provider_message_id_and_no_prospect_name_when_contact_missing(
    campaign_service, mailbox_store, channel_store, client
):
    campaign_id = await _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store)

    resp = client.get(f"/mail/campaigns/{campaign_id}/execution-steps", params={"status": "unknown"})
    row = resp.json()[0]

    assert row["status"] == "unknown"
    assert row["gmail_message_id"] == "gmail-123"
    assert row["prospect_name"] is None  # crm_contact_id points at a contact that was never created


@pytest.mark.asyncio
async def test_execution_steps_never_expose_mailbox_oauth_fields(campaign_service, mailbox_store, channel_store, client):
    """No field on MailExecutionStepView is anything from Mailbox beyond
    `mailbox_id` -- no OAuth token, refresh token, or scope list should be
    reachable through this route no matter what's on the row."""
    campaign_id = await _seed_campaign_with_steps(campaign_service, mailbox_store, channel_store)

    resp = client.get(f"/mail/campaigns/{campaign_id}/execution-steps")
    body = resp.json()

    for row in body:
        assert set(row.keys()) == {
            "enrollment_step_id", "mail_campaign_id", "enrollment_id", "step_number", "status",
            "prospect_email", "prospect_name", "sent_at", "last_attempt_at", "last_error",
            "mailbox_id", "gmail_message_id", "rfc_message_id", "updated_at",
        }


def test_list_execution_steps_for_missing_campaign_returns_404(client):
    resp = client.get("/mail/campaigns/does-not-exist/execution-steps")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_execution_steps_for_fresh_campaign_is_empty(client):
    created = client.post("/mail/campaigns", json={"name": "Fresh"}).json()

    resp = client.get(f"/mail/campaigns/{created['mail_campaign_id']}/execution-steps")

    assert resp.status_code == 200
    assert resp.json() == []
