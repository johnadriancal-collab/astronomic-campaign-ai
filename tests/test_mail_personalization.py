"""
P0-1 -- send-time personalization rendering (2026-09-15).

Two layers are covered:
  - Unit tests of app/services/mail_personalization.py's pure functions
    (render_mail_template / contact_personalization_variables) in
    isolation.
  - Integration tests through MailSendingService.prepare_and_send_step()
    -- the ONE canonical execution path (see that method's own docstring)
    -- proving the rendered subject/body is what actually reaches
    MailSenderPort.prepare(), that an unresolved allowed variable fails
    BEFORE any provider call rather than sending the literal token, and
    that the stored MailSequenceStep/MailEnrollmentStep content is never
    mutated by rendering.

Fixture conventions (mailbox/campaign/allowlist setup, FakePreparingSender)
mirror tests/test_prepare_and_send_step.py exactly, plus a CrmContact
fixture registered in a MemoryCrmContactStore passed explicitly to
MailSendingService.
"""

from cryptography.fernet import Fernet
from datetime import datetime, time, timezone

import pytest
import pytest_asyncio

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
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.mail_campaign_mailbox_store import MemoryMailCampaignMailboxStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.repositories.mailbox_send_policy_store import MemoryMailboxSendPolicyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_personalization import (
    MailPersonalizationError,
    contact_personalization_variables,
    render_mail_template,
)
from app.services.mail_sending_service import (
    MailSendError,
    MailSendingService,
    MailSenderPort,
    MailSendRequest,
    SendBlockReason,
    SendOutcomeCertainty,
    SendResult,
)

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


# =====================================================================
# Unit tests -- render_mail_template() / contact_personalization_variables()
# =====================================================================


def make_contact(**overrides) -> CrmContact:
    fields = dict(
        crm_contact_id="contact-1", created_at=NOW, updated_at=NOW,
        first_name="Ada", last_name="Lovelace", company="Astronomic",
    )
    fields.update(overrides)
    return CrmContact(**fields)


def test_unit_first_name_rendering():
    variables = contact_personalization_variables(make_contact())
    assert render_mail_template("Hi {{first_name}},", variables) == "Hi Ada,"


def test_unit_last_name_rendering():
    variables = contact_personalization_variables(make_contact())
    assert render_mail_template("Dear {{last_name}},", variables) == "Dear Lovelace,"


def test_unit_company_rendering():
    variables = contact_personalization_variables(make_contact())
    assert render_mail_template("Regarding {{company}}.", variables) == "Regarding Astronomic."


def test_unit_multiple_variables_in_one_template():
    variables = contact_personalization_variables(make_contact())
    rendered = render_mail_template("Hi {{first_name}} {{last_name}} from {{company}}.", variables)
    assert rendered == "Hi Ada Lovelace from Astronomic."


def test_unit_no_tokens_returns_template_unchanged():
    variables = contact_personalization_variables(None)
    assert render_mail_template("Hello, no tokens here.", variables) == "Hello, no tokens here."


def test_unit_missing_value_raises_personalization_error():
    variables = contact_personalization_variables(make_contact(company=None))
    with pytest.raises(MailPersonalizationError):
        render_mail_template("Regarding {{company}}.", variables)


def test_unit_blank_value_raises_personalization_error():
    variables = contact_personalization_variables(make_contact(first_name="   "))
    with pytest.raises(MailPersonalizationError):
        render_mail_template("Hi {{first_name}}.", variables)


def test_unit_unsupported_variable_raises_personalization_error():
    variables = contact_personalization_variables(make_contact())
    with pytest.raises(MailPersonalizationError):
        render_mail_template("Your deal size is {{deal_size}}.", variables)


def test_unit_is_a_value_error_subclass():
    assert issubclass(MailPersonalizationError, ValueError)


def test_unit_contact_personalization_variables_none_contact_returns_empty():
    assert contact_personalization_variables(None) == {}


# =====================================================================
# Integration tests -- through MailSendingService.prepare_and_send_step()
# =====================================================================


class FakeRetryableError(MailSendError):
    certainty = SendOutcomeCertainty.DEFINITELY_NOT_SENT
    retryable = True


class RecordingSender(MailSenderPort):
    def __init__(self):
        self.prepare_calls: list[MailSendRequest] = []
        self.send_prepared_calls: list[MailSendRequest] = []
        self.send_prepared_error: Exception | None = None

    async def prepare(self, request: MailSendRequest) -> MailSendRequest:
        self.prepare_calls.append(request)
        return request

    async def send_prepared(self, prepared: MailSendRequest) -> SendResult:
        self.send_prepared_calls.append(prepared)
        if self.send_prepared_error is not None:
            raise self.send_prepared_error
        return SendResult(
            provider_message_id=f"msg-{len(self.send_prepared_calls)}",
            provider_thread_id=f"thr-{len(self.send_prepared_calls)}",
            rfc_message_id=prepared.rfc_message_id,
        )


async def always_leader() -> bool:
    return True


def all_day_windows() -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{d}", mail_campaign_id="c1", day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


def make_mailbox(mailbox_id="mbx-1") -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=MailboxStatus.CONNECTED, google_user_id=f"g-{mailbox_id}",
        granted_scopes=["https://www.googleapis.com/auth/gmail.send"], connected_at=NOW, updated_at=NOW,
    )


def make_campaign() -> MailCampaign:
    return MailCampaign(
        mail_campaign_id="c1", name="Test Campaign", status=MailCampaignStatus.ACTIVE, timezone=TZ,
        created_at=NOW, updated_at=NOW,
    )


def make_step(subject="Subject", body="Body.") -> MailSequenceStep:
    return MailSequenceStep(
        step_id="s1", mail_campaign_id="c1", step_number=1, subject=subject, body=body,
        delay_days=0, reply_in_thread=False, created_at=NOW, updated_at=NOW,
    )


def make_enrollment(enrollment_id="e1", email="lead@example.com", crm_contact_id="contact-1") -> MailEnrollment:
    return MailEnrollment(
        enrollment_id=enrollment_id, mail_campaign_id="c1", crm_contact_id=crm_contact_id,
        email_at_enrollment=email, status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id="mbx-1",
    )


@pytest_asyncio.fixture
async def svc():
    campaign_store = MemoryMailCampaignStore()
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    crm_contact_store = MemoryCrmContactStore()
    service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=MemoryMailEnrollmentStore(),
        step_store=MemoryMailEnrollmentStepStore(), mailbox_store=mailbox_store, channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()), crm_contact_store=crm_contact_store,
    )
    await campaign_store.create(make_campaign())
    await mailbox_store.create(make_mailbox())
    await channel_store.replace_for_campaign("c1", ["mbx-1"])
    return service


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _controlled_test_gate_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", "mbx-1")
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")


async def _enroll_with_contact(svc, contact: CrmContact, subject="Subject", body="Body.") -> tuple:
    await svc.crm_contact_store.create(contact)
    enrollment = make_enrollment(crm_contact_id=contact.crm_contact_id)
    await svc.enrollment_store.create(enrollment)
    step1 = make_step(subject=subject, body=body)
    row = await svc.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    return row, enrollment, step1


async def _send(svc, row, sequence_step) -> tuple:
    sender = RecordingSender()
    outcome = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[sequence_step], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    return outcome, sender


# --- (1) first_name rendering -----------------------------------------------


@pytest.mark.asyncio
async def test_first_name_renders_in_sent_message(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Hi {{first_name}},")
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert "Hi Ada," in sender.prepare_calls[0].body
    assert "{{first_name}}" not in sender.prepare_calls[0].body


# --- (2) last_name rendering -------------------------------------------------


@pytest.mark.asyncio
async def test_last_name_renders_in_sent_message(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Dear {{last_name}},")
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert "Dear Lovelace," in sender.prepare_calls[0].body


# --- (3) company rendering ----------------------------------------------------


@pytest.mark.asyncio
async def test_company_renders_in_sent_message(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Regarding {{company}}.")
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert "Regarding Astronomic." in sender.prepare_calls[0].body


# --- (4) multiple variables in one template -----------------------------------


@pytest.mark.asyncio
async def test_multiple_variables_all_render_together(svc):
    row, _, step1 = await _enroll_with_contact(
        svc, make_contact(), body="Hi {{first_name}} {{last_name}} from {{company}}."
    )
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert sender.prepare_calls[0].body.startswith("Hi Ada Lovelace from Astronomic.")


# --- (5) subject rendering -----------------------------------------------------


@pytest.mark.asyncio
async def test_subject_renders(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), subject="A note for {{first_name}}")
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert sender.prepare_calls[0].subject == "A note for Ada"


# --- (6) body rendering ---------------------------------------------------------


@pytest.mark.asyncio
async def test_body_renders(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Hello {{first_name}}, welcome.")
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is True
    assert "Hello Ada, welcome." in sender.prepare_calls[0].body


# --- (7) missing value behavior -- fails safely, never sends the literal token --


@pytest.mark.asyncio
async def test_missing_value_fails_before_any_provider_call(svc, step_store=None):
    row, enrollment, step1 = await _enroll_with_contact(
        svc, make_contact(company=None), body="Regarding {{company}}."
    )
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is False
    assert len(sender.prepare_calls) == 0
    assert len(sender.send_prepared_calls) == 0

    sent_row = await svc.step_store.get(row.enrollment_step_id)
    assert sent_row.status == MailEnrollmentStepStatus.FAILED
    failed_enrollment = await svc.enrollment_store.get(enrollment.enrollment_id)
    assert failed_enrollment.status == MailEnrollmentStatus.FAILED


@pytest.mark.asyncio
async def test_missing_value_for_nonexistent_contact_fails_safely(svc):
    """crm_contact_id points at no registered contact at all (e.g. the
    Contact was deleted after enrollment) -- contact_personalization_
    variables(None) returns {}, so any token in the template is
    unresolved and must fail exactly the same way as a present-but-blank
    field, never send a literal token."""
    enrollment = make_enrollment(crm_contact_id="does-not-exist")
    await svc.enrollment_store.create(enrollment)
    step1 = make_step(body="Hi {{first_name}},")
    row = await svc.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is False
    assert len(sender.prepare_calls) == 0


# --- (8) unsupported variable rejection -----------------------------------------


@pytest.mark.asyncio
async def test_unsupported_variable_fails_before_any_provider_call(svc):
    row, _, step1 = await _enroll_with_contact(
        svc, make_contact(), body="Your deal size is {{deal_size}}."
    )
    outcome, sender = await _send(svc, row, step1)
    assert outcome.sent is False
    assert len(sender.prepare_calls) == 0
    sent_row = await svc.step_store.get(row.enrollment_step_id)
    assert sent_row.status == MailEnrollmentStepStatus.FAILED


# --- (9) no mutation of the stored template --------------------------------------


@pytest.mark.asyncio
async def test_rendering_never_mutates_stored_step_content(svc):
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Hi {{first_name}},")
    original_body = row.body
    await _send(svc, row, step1)

    sent_row = await svc.step_store.get(row.enrollment_step_id)
    assert sent_row.body == original_body
    assert sent_row.body == "Hi {{first_name}},"

    stored_sequence_step = step1
    assert stored_sequence_step.body == "Hi {{first_name}},"


# --- (10) retries render identically ----------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_transient_release_renders_identically(svc):
    """A step released back to QUEUED after a transient (retryable)
    provider failure and reprocessed on a second attempt must render the
    exact same subject/body both times -- rendering is a pure function of
    (stored template, current contact data), never cached, mutated, or
    re-derived differently between attempts on the SAME row."""
    row, _, step1 = await _enroll_with_contact(svc, make_contact(), body="Hi {{first_name}} from {{company}}.")

    sender = RecordingSender()
    sender.send_prepared_error = FakeRetryableError("rate limited")
    outcome1 = await svc.prepare_and_send_step(
        row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome1.sent is False
    assert outcome1.blocked_reason == SendBlockReason.DEFINITELY_NOT_SENT_RETRY
    first_rendered_body = sender.prepare_calls[0].body
    first_rendered_subject = sender.prepare_calls[0].subject

    released_row = await svc.step_store.get(row.enrollment_step_id)
    assert released_row.status == MailEnrollmentStepStatus.QUEUED

    sender.send_prepared_error = None
    outcome2 = await svc.prepare_and_send_step(
        released_row, sender=sender, claimed_by="w1", sequence_steps=[step1], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome2.sent is True
    second_rendered_body = sender.prepare_calls[1].body
    second_rendered_subject = sender.prepare_calls[1].subject

    # Compare only the personalization-owned prefix, not the full composed
    # body: compose_outbound_email() appends a per-attempt unsubscribe
    # link/token after the rendered text, which is EXPECTED to differ
    # attempt-to-attempt (a fresh token each time) -- that is
    # unsubscribe-token behavior, not personalization, and asserting full
    # body equality would conflate the two.
    assert first_rendered_body.startswith("Hi Ada from Astronomic.")
    assert second_rendered_body.startswith("Hi Ada from Astronomic.")
    assert first_rendered_subject == second_rendered_subject
