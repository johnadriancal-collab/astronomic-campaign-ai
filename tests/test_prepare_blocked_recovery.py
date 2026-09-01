"""
MailSendingService.resume_prepare_config_blocked_enrollments() and
resolve_prepare_blocked_step() -- the second Phase C hardening pass's fix
for the original PREPARE_BLOCKED infinite-retry-circuit bug (a single
pause reason meant EVERY periodic sweep blindly resumed every blocked
enrollment, regardless of whether the actual blocking condition had
cleared -- see MailEnrollmentPauseReason's own docstring for the
three-way split that replaces it).

PREPARE_CONFIG_BLOCKED: auto-resumed ONLY when the known configuration
prerequisite (_prepare_configuration_satisfied()) is actually present --
never blindly, never on a timer alone.

PREPARE_TRANSIENT_EXHAUSTED / PREPARE_UNCLASSIFIED_BLOCKED: NEVER
auto-resumed by any periodic sweep -- recoverable ONLY via the explicit,
authenticated resolve_prepare_blocked_step().
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail import router as mail_router
from app.dependencies import get_mail_sending_service
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentPauseReason,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_sending_service import (
    MailSendingService,
    PrepareBlockedWrongStateError,
    UnknownStepNotFoundError,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def campaign_store():
    return MemoryMailCampaignStore()


@pytest.fixture
def enrollment_store():
    return MemoryMailEnrollmentStore()


@pytest.fixture
def step_store():
    return MemoryMailEnrollmentStepStore()


@pytest.fixture
def mailbox_store():
    return MemoryMailboxStore()


@pytest.fixture
def channel_store():
    return MemoryMailCampaignMailboxStore()


@pytest.fixture
def activity_log():
    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def svc(campaign_store, enrollment_store, step_store, mailbox_store, channel_store, activity_log):
    return MailSendingService(
        campaign_store=campaign_store, enrollment_store=enrollment_store, step_store=step_store,
        mailbox_store=mailbox_store, channel_store=channel_store, policy_store=MemoryMailboxSendPolicyStore(),
        suppression_store=MemoryMailSuppressionStore(), activity_log=activity_log,
    )


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    """Happy-path default: both configuration prerequisites present.
    Tests that specifically need one/both missing override this
    themselves."""
    from cryptography.fernet import Fernet

    monkeypatch.setattr("app.services.mail_sending_service.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.mail_sending_service.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


def make_campaign(status=MailCampaignStatus.ACTIVE) -> MailCampaign:
    return MailCampaign(mail_campaign_id="c1", name="Test", status=status, created_at=NOW, updated_at=NOW)


def make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED) -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=status, google_user_id=f"g-{mailbox_id}", connected_at=NOW, updated_at=NOW,
    )


def make_paused_enrollment(reason: MailEnrollmentPauseReason, assigned_mailbox_id="mbx-1") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="lead@example.com",
        status=MailEnrollmentStatus.PAUSED, paused_reason=reason, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=assigned_mailbox_id,
    )


def make_queued_step(prepare_failure_count=0, rfc_message_id="preserved-id@astronomic.com") -> MailEnrollmentStep:
    return MailEnrollmentStep(
        enrollment_step_id="es1", mail_campaign_id="c1", enrollment_id="e1", crm_contact_id="contact-1",
        step_id="s1", step_number=1, subject="Subj", body="Body.", delay_days=0, reply_in_thread=False,
        status=MailEnrollmentStepStatus.QUEUED, next_send_at=NOW, mailbox_id="mbx-1",
        rfc_message_id=rfc_message_id, prepare_failure_count=prepare_failure_count,
        created_at=NOW, updated_at=NOW,
    )


async def _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, *, reason, prepare_failure_count=0):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    await enrollment_store.create(make_paused_enrollment(reason))
    await step_store.create(make_queued_step(prepare_failure_count=prepare_failure_count))


# =====================================================================
# resume_prepare_config_blocked_enrollments(): conditional, not blind
# =====================================================================


async def test_config_blocked_does_not_resume_while_public_origin_missing(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, monkeypatch
):
    monkeypatch.setattr("app.services.mail_sending_service.settings.public_backend_origin", None)
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED)

    # Simulate several periodic recovery cycles -- must remain a no-op every time.
    for _ in range(5):
        resumed = await svc.resume_prepare_config_blocked_enrollments(NOW)
        assert resumed == 0

    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED


async def test_config_blocked_does_not_resume_while_unsubscribe_key_missing(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, monkeypatch
):
    monkeypatch.setattr("app.services.mail_sending_service.settings.unsubscribe_token_encryption_keys", None)
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED)

    for _ in range(5):
        resumed = await svc.resume_prepare_config_blocked_enrollments(NOW)
        assert resumed == 0

    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED


async def test_config_blocked_sweep_is_a_complete_noop_when_prerequisite_missing_no_writes(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, monkeypatch
):
    """Not merely 'ends up still paused' -- genuinely untouched: no
    write happens at all when the prerequisite check fails, proven by
    the step's updated_at staying exactly what it was."""
    monkeypatch.setattr("app.services.mail_sending_service.settings.public_backend_origin", None)
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED, prepare_failure_count=2)

    await svc.resume_prepare_config_blocked_enrollments(NOW + timedelta(hours=3))

    step = await step_store.get("es1")
    assert step.updated_at == NOW
    assert step.prepare_failure_count == 2


async def test_config_blocked_resumes_once_both_prerequisites_satisfied(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, monkeypatch
):
    monkeypatch.setattr("app.services.mail_sending_service.settings.public_backend_origin", None)
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED)

    # Still missing -- stays paused.
    assert await svc.resume_prepare_config_blocked_enrollments(NOW) == 0

    # Now fixed.
    monkeypatch.setattr("app.services.mail_sending_service.settings.public_backend_origin", "https://fake.test")
    resumed = await svc.resume_prepare_config_blocked_enrollments(NOW + timedelta(minutes=5))

    assert resumed == 1
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.ACTIVE
    assert enrollment.paused_reason is None
    assert enrollment.assigned_mailbox_id == "mbx-1"  # never touched


async def test_config_blocked_resume_preserves_message_id_and_mailbox(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED)

    await svc.resume_prepare_config_blocked_enrollments(NOW)

    step = await step_store.get("es1")
    assert step.rfc_message_id == "preserved-id@astronomic.com"
    assert step.mailbox_id == "mbx-1"
    enrollment = await enrollment_store.get("e1")
    assert enrollment.assigned_mailbox_id == "mbx-1"


# =====================================================================
# transient-exhausted / unclassified: NEVER touched by the periodic sweep
# =====================================================================


@pytest.mark.parametrize(
    "reason", [MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, MailEnrollmentPauseReason.PREPARE_UNCLASSIFIED_BLOCKED]
)
async def test_transient_exhausted_and_unclassified_are_never_touched_by_the_periodic_sweep(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason
):
    """The core fix: these two reasons must NOT receive a fresh retry
    budget every recovery cycle, no matter how many cycles pass or
    whether configuration happens to be satisfied -- resume_prepare_
    config_blocked_enrollments() only ever looks at PREPARE_CONFIG_BLOCKED."""
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=reason, prepare_failure_count=3)

    for cycle in range(10):
        resumed = await svc.resume_prepare_config_blocked_enrollments(NOW + timedelta(minutes=5 * cycle))
        assert resumed == 0

    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.PAUSED
    assert enrollment.paused_reason == reason
    step = await step_store.get("es1")
    assert step.prepare_failure_count == 3  # never reset by the periodic sweep


# =====================================================================
# resolve_prepare_blocked_step(): explicit, authenticated recovery
# =====================================================================


async def test_explicit_recovery_resumes_transient_exhausted(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    applied = await svc.resolve_prepare_blocked_step("es1", now=NOW)

    assert applied is True
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.ACTIVE
    assert enrollment.paused_reason is None


async def test_explicit_recovery_resumes_unclassified_blocked(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_UNCLASSIFIED_BLOCKED, prepare_failure_count=1)

    applied = await svc.resolve_prepare_blocked_step("es1", now=NOW)

    assert applied is True
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.ACTIVE


async def test_explicit_recovery_resets_the_counter_only_as_its_own_consequence(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    step_before = await step_store.get("es1")
    assert step_before.prepare_failure_count == 3  # unchanged up to the moment of explicit recovery

    await svc.resolve_prepare_blocked_step("es1", now=NOW)

    step_after = await step_store.get("es1")
    assert step_after.prepare_failure_count == 0


async def test_explicit_recovery_preserves_message_id_and_assigned_mailbox(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    await svc.resolve_prepare_blocked_step("es1", now=NOW)

    step = await step_store.get("es1")
    assert step.rfc_message_id == "preserved-id@astronomic.com"
    enrollment = await enrollment_store.get("e1")
    assert enrollment.assigned_mailbox_id == "mbx-1"


async def test_explicit_recovery_logs_a_structural_activity_event(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store, activity_log
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    await svc.resolve_prepare_blocked_step("es1", now=NOW)

    events = await activity_log.store.list()
    assert any(e.event_type == "mail_enrollment.prepare_manually_resumed" for e in events)


async def test_explicit_recovery_raises_when_step_not_found(svc):
    with pytest.raises(UnknownStepNotFoundError):
        await svc.resolve_prepare_blocked_step("does-not-exist", now=NOW)


async def test_explicit_recovery_raises_when_enrollment_is_active(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    active_enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-1", email_at_enrollment="lead@example.com",
        status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW, assigned_mailbox_id="mbx-1",
    )
    await enrollment_store.create(active_enrollment)
    await step_store.create(make_queued_step())

    with pytest.raises(PrepareBlockedWrongStateError):
        await svc.resolve_prepare_blocked_step("es1", now=NOW)


async def test_explicit_recovery_raises_when_paused_for_mailbox_unavailable(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    """This manual recovery action must only ever apply to the two
    pause reasons it's documented for -- a MAILBOX_UNAVAILABLE pause has
    its OWN, different automatic recovery path and must not be
    short-circuited by this route."""
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE)

    with pytest.raises(PrepareBlockedWrongStateError):
        await svc.resolve_prepare_blocked_step("es1", now=NOW)

    enrollment = await enrollment_store.get("e1")
    assert enrollment.paused_reason == MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE  # untouched


async def test_explicit_recovery_raises_when_paused_for_config_blocked(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    """Config-blocked has its own conditional automatic recovery path
    (resume_prepare_config_blocked_enrollments()) -- this manual route
    is reserved for the two reasons with NO automatic path at all."""
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_CONFIG_BLOCKED)

    with pytest.raises(PrepareBlockedWrongStateError):
        await svc.resolve_prepare_blocked_step("es1", now=NOW)


async def test_explicit_recovery_is_idempotent_safe_on_repeated_wrong_state_calls(
    svc, campaign_store, mailbox_store, channel_store, enrollment_store, step_store
):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    await svc.resolve_prepare_blocked_step("es1", now=NOW)  # first call succeeds

    with pytest.raises(PrepareBlockedWrongStateError):
        await svc.resolve_prepare_blocked_step("es1", now=NOW)  # already ACTIVE -- second call rejected


# =====================================================================
# API route: POST /mail/execution/{enrollment_step_id}/resolve-prepare-blocked
# (session-gated, not public -- see app/api/mail.py's module docstring)
# =====================================================================


@pytest.fixture
def api_client(svc):
    app = FastAPI()
    app.include_router(mail_router)
    app.dependency_overrides[get_mail_sending_service] = lambda: svc
    with TestClient(app) as c:
        yield c


async def test_api_resolve_prepare_blocked_success(api_client, campaign_store, mailbox_store, channel_store, enrollment_store, step_store):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.PREPARE_TRANSIENT_EXHAUSTED, prepare_failure_count=3)

    resp = api_client.post("/mail/execution/es1/resolve-prepare-blocked")

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    enrollment = await enrollment_store.get("e1")
    assert enrollment.status == MailEnrollmentStatus.ACTIVE


async def test_api_resolve_prepare_blocked_on_missing_row_is_404(api_client):
    resp = api_client.post("/mail/execution/nonexistent/resolve-prepare-blocked")
    assert resp.status_code == 404


async def test_api_resolve_prepare_blocked_on_wrong_state_is_409(api_client, campaign_store, mailbox_store, channel_store, enrollment_store, step_store):
    await _standard_setup(campaign_store, mailbox_store, channel_store, enrollment_store, step_store, reason=MailEnrollmentPauseReason.MAILBOX_UNAVAILABLE)

    resp = api_client.post("/mail/execution/es1/resolve-prepare-blocked")

    assert resp.status_code == 409


async def test_resolve_prepare_blocked_route_is_not_public():
    """Structural proof, matching the existing resolve-sent/resolve-not-
    sent routes' own equivalent test -- see
    tests/test_unknown_reconciliation.py's
    test_execution_routes_require_a_session_in_the_real_app()."""
    from app.session_auth_middleware import PUBLIC_PATHS

    assert "/mail/execution/es1/resolve-prepare-blocked" not in PUBLIC_PATHS
    assert not any(p.startswith("/mail/execution") for p in PUBLIC_PATHS)
