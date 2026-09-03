"""
MailSendingService.resolve_unknown_step_confirmed_sent()/
_confirmed_not_sent() -- the minimum backend capability for manually
resolving an UNKNOWN row. No automatic inference, no automatic resend --
every test here drives an EXPLICIT human assertion, matching the real
contract. Also covers the two admin API routes (session-gated, not
public).
"""

from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail import router as mail_router
from app.dependencies import get_mail_campaign_service, get_mail_sending_service
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailSendWindow,
    MailSequenceStep,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_batch_store import MemoryMailEnrollmentBatchStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_send_window_store import MemoryMailSendWindowStore
from app.repositories.mail_sequence_step_store import MemoryMailSequenceStepStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.crm_service import CrmService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_sending_service import (
    MailSendingService,
    UnknownStepNotFoundError,
    UnknownStepWrongStatusError,
)

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


def all_day_windows() -> list[MailSendWindow]:
    return [
        MailSendWindow(window_id=f"w-{d}", mail_campaign_id="c1", day_of_week=d, start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW)
        for d in range(7)
    ]


@pytest.fixture
def step_store():
    return MemoryMailEnrollmentStepStore()


@pytest.fixture
def enrollment_store():
    return MemoryMailEnrollmentStore()


@pytest.fixture
def campaign_store():
    return MemoryMailCampaignStore()


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def svc(campaign_store, enrollment_store, step_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=MemoryMailboxStore(), channel_store=MemoryMailCampaignMailboxStore(),
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=activity_log,
    )


def make_unknown_step(rfc_message_id="original-id@astronomic.com") -> MailEnrollmentStep:
    return MailEnrollmentStep(
        enrollment_step_id="step-1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1",
        step_id="s1", step_number=1, subject="Subj", body="Body", delay_days=0, reply_in_thread=False,
        status=MailEnrollmentStepStatus.UNKNOWN, rfc_message_id=rfc_message_id, mailbox_id="mbx-1",
        created_at=NOW, updated_at=NOW,
    )


def make_step2() -> MailSequenceStep:
    return MailSequenceStep(step_id="s2", mail_campaign_id="c1", step_number=2, subject="Follow-up", body="Body 2", delay_days=1, reply_in_thread=False, created_at=NOW, updated_at=NOW)


async def seed(step_store, enrollment_store, step: MailEnrollmentStep) -> None:
    await step_store.create(step)
    await enrollment_store.create(
        MailEnrollment(enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="lead@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1")
    )


# --- confirmed_sent -----------------------------------------------------------


async def test_confirmed_sent_transitions_to_sent_and_materializes_next_step(svc, step_store, enrollment_store):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    applied = await svc.resolve_unknown_step_confirmed_sent(
        "step-1", provider_message_id="gmail-msg-1", provider_thread_id="gmail-thr-1",
        sequence_steps=[make_step2()], windows=all_day_windows(), timezone_name=TZ, now=NOW,
    )
    assert applied is True
    resolved = await step_store.get("step-1")
    assert resolved.status == MailEnrollmentStepStatus.SENT
    assert resolved.gmail_message_id == "gmail-msg-1"
    assert resolved.gmail_thread_id == "gmail-thr-1"
    assert resolved.rfc_message_id == "original-id@astronomic.com"  # never regenerated
    all_rows = await step_store.list_for_enrollment("e1")
    assert len(all_rows) == 2  # Step 2 materialized


async def test_confirmed_sent_completes_the_enrollment_when_no_next_step(svc, step_store, enrollment_store):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    await svc.resolve_unknown_step_confirmed_sent(
        "step-1", provider_message_id="gmail-msg-1", provider_thread_id="gmail-thr-1",
        sequence_steps=[], windows=all_day_windows(), timezone_name=TZ, now=NOW,
    )
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.COMPLETED


async def test_confirmed_sent_on_a_non_unknown_row_raises(svc, step_store, enrollment_store):
    step = make_unknown_step()
    sent_already = step.model_copy(update={"status": MailEnrollmentStepStatus.SENT})
    await seed(step_store, enrollment_store, sent_already)
    with pytest.raises(UnknownStepWrongStatusError):
        await svc.resolve_unknown_step_confirmed_sent(
            "step-1", provider_message_id="x", provider_thread_id="y",
            sequence_steps=[], windows=all_day_windows(), timezone_name=TZ, now=NOW,
        )


async def test_confirmed_sent_on_a_missing_row_raises(svc):
    with pytest.raises(UnknownStepNotFoundError):
        await svc.resolve_unknown_step_confirmed_sent(
            "nonexistent", provider_message_id="x", provider_thread_id="y",
            sequence_steps=[], windows=all_day_windows(), timezone_name=TZ, now=NOW,
        )


async def test_confirmed_sent_logs_a_structural_activity_event(svc, step_store, enrollment_store, activity_log):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    await svc.resolve_unknown_step_confirmed_sent(
        "step-1", provider_message_id="x", provider_thread_id="y",
        sequence_steps=[], windows=all_day_windows(), timezone_name=TZ, now=NOW,
    )
    events = await activity_log.store.list()
    assert any(e.event_type == "mail_enrollment_step.manually_resolved_sent" for e in events)


# --- confirmed_not_sent ---------------------------------------------------------


async def test_confirmed_not_sent_requeues_preserving_message_id(svc, step_store, enrollment_store):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    applied = await svc.resolve_unknown_step_confirmed_not_sent("step-1", now=NOW)
    assert applied is True
    requeued = await step_store.get("step-1")
    assert requeued.status == MailEnrollmentStepStatus.QUEUED
    assert requeued.rfc_message_id == "original-id@astronomic.com"  # exact preservation
    assert requeued.claimed_by is None


async def test_confirmed_not_sent_on_a_non_unknown_row_raises(svc, step_store, enrollment_store):
    step = make_unknown_step()
    queued_already = step.model_copy(update={"status": MailEnrollmentStepStatus.QUEUED})
    await seed(step_store, enrollment_store, queued_already)
    with pytest.raises(UnknownStepWrongStatusError):
        await svc.resolve_unknown_step_confirmed_not_sent("step-1", now=NOW)


async def test_confirmed_not_sent_on_a_missing_row_raises(svc):
    with pytest.raises(UnknownStepNotFoundError):
        await svc.resolve_unknown_step_confirmed_not_sent("nonexistent", now=NOW)


async def test_confirmed_not_sent_logs_a_structural_activity_event(svc, step_store, enrollment_store, activity_log):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    await svc.resolve_unknown_step_confirmed_not_sent("step-1", now=NOW)
    events = await activity_log.store.list()
    assert any(e.event_type == "mail_enrollment_step.manually_resolved_not_sent" for e in events)


async def test_no_automatic_inference_repeated_calls_still_require_explicit_assertion(svc, step_store, enrollment_store):
    """Resolving once to QUEUED, then trying to resolve AGAIN (still
    thinking it's UNKNOWN) must fail loudly -- there is no silent
    idempotent success for a row that's no longer UNKNOWN."""
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    await svc.resolve_unknown_step_confirmed_not_sent("step-1", now=NOW)
    with pytest.raises(UnknownStepWrongStatusError):
        await svc.resolve_unknown_step_confirmed_not_sent("step-1", now=NOW)


# --- API routes (session-gated, not public) --------------------------------------


@pytest.fixture
def api_client(svc, campaign_store):
    app = FastAPI()
    app.include_router(mail_router)
    campaign_service = MailCampaignService(
        campaign_store=campaign_store, step_store=MemoryMailSequenceStepStore(), enrollment_store=svc.enrollment_store,
        crm_service=CrmService(), activity_log=svc.activity_log, mailbox_store=svc.mailbox_store,
        channel_store=svc.channel_store, window_store=MemoryMailSendWindowStore(),
        enrollment_step_store=svc.step_store, sending_service=svc,
        batch_store=MemoryMailEnrollmentBatchStore(),
    )
    app.dependency_overrides[get_mail_sending_service] = lambda: svc
    app.dependency_overrides[get_mail_campaign_service] = lambda: campaign_service
    with TestClient(app) as c:
        yield c


async def test_api_resolve_sent_requires_provider_ids(api_client, step_store, enrollment_store, campaign_store):
    await campaign_store.create(MailCampaign(mail_campaign_id="c1", name="Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW))
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    resp = api_client.post("/mail/execution/step-1/resolve-sent", json={})
    assert resp.status_code == 422  # missing required fields


async def test_api_resolve_sent_success(api_client, step_store, enrollment_store, campaign_store):
    await campaign_store.create(MailCampaign(mail_campaign_id="c1", name="Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW))
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    resp = api_client.post(
        "/mail/execution/step-1/resolve-sent", json={"provider_message_id": "gmail-1", "provider_thread_id": "thr-1"}
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True


async def test_api_resolve_sent_on_missing_row_is_404(api_client):
    resp = api_client.post("/mail/execution/nonexistent/resolve-sent", json={"provider_message_id": "x", "provider_thread_id": "y"})
    assert resp.status_code == 404


async def test_api_resolve_not_sent_success(api_client, step_store, enrollment_store):
    step = make_unknown_step()
    await seed(step_store, enrollment_store, step)
    resp = api_client.post("/mail/execution/step-1/resolve-not-sent")
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    requeued = await step_store.get("step-1")
    assert requeued.status == MailEnrollmentStepStatus.QUEUED


async def test_api_resolve_not_sent_on_wrong_status_is_409(api_client, step_store, enrollment_store):
    step = make_unknown_step()
    already_sent = step.model_copy(update={"status": MailEnrollmentStepStatus.SENT})
    await seed(step_store, enrollment_store, already_sent)
    resp = api_client.post("/mail/execution/step-1/resolve-not-sent")
    assert resp.status_code == 409


async def test_execution_routes_require_a_session_in_the_real_app():
    """Structural proof these routes are NOT public -- unlike
    /mail/unsubscribe*, they carry no PUBLIC_PATHS exemption, so the
    real session-auth middleware rejects an unauthenticated request."""
    from app.session_auth_middleware import PUBLIC_PATHS

    assert "/mail/execution/step-1/resolve-sent" not in PUBLIC_PATHS
    assert not any(p.startswith("/mail/execution") for p in PUBLIC_PATHS)
