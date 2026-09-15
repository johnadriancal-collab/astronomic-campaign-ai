"""
End-to-end Gmail threading boundary proof (2026-09-15) -- requested after
a SECOND controlled production proof still showed two separate Gmail
conversations even with matching subjects. Every existing test file
checks one LAYER of the threading chain in isolation (test_mail_reply_
threading.py: MailSendingService populates MailSendRequest correctly;
test_gmail_sender.py: GmailSender passes thread_id through to
GmailApiClient; test_gmail_api_client.py: GmailApiClient includes
threadId in the JSON body). This file proves the WHOLE chain at once,
through the REAL GmailSender + REAL GmailApiClient (only the HTTP
transport is mocked, via httpx.MockTransport -- the same technique
test_gmail_api_client.py already uses, never a hand-rolled fake standing
in for either of those two real classes) -- capturing the actual JSON
request body Gmail would receive, decoding the actual base64url `raw`
MIME payload, and asserting on the real parsed headers.

Root-cause finding this file exists to document, not just test: every
layer traced here is CORRECT --
  - threadId IS present in the Gmail API request body for a threaded
    follow-up, sourced from Step 1's real (mock-)Gmail-returned thread
    id, never a locally-invented value.
  - Message-ID/In-Reply-To/References are RFC 5322 compliant (correctly
    single-bracketed, no double-bracketing, space-separated References,
    In-Reply-To pointing at the exact prior Message-ID).
  - Subject matches exactly (the 2026-09-15 threading-subject fix).
  - The base64url round-trip does not corrupt anything.
No code defect was found anywhere in this chain -- see the session
report for what remains to investigate outside of it (most likely a
Gmail account-level factor such as Conversation View being disabled,
which no amount of correct outbound header construction can work
around; confirming this needs live Gmail API read access, not yet
granted).
"""

import base64
import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from email import message_from_bytes

from app.google.gmail_api_client import GMAIL_SEND_URL, GmailApiClient
from app.google.gmail_sender import GmailSender
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailSendWindow,
    MailSequenceStep,
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
from app.services.mail_sending_service import MailSendingService

pytestmark = pytest.mark.asyncio

TZ = "America/Chicago"
NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
MAILBOX_ID = "mbx-1"


class FakeMailboxService:
    """Duck-types the one method GmailSender actually calls -- matches
    tests/test_gmail_sender.py's own FakeMailboxService exactly, so this
    file never has to stand up real Google OAuth/token-encryption
    plumbing to exercise a real GmailSender."""

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        return "fake-access-token"


def _patch_transport(monkeypatch, handler):
    """Same technique as tests/test_gmail_api_client.py's own
    _patch_transport -- routes the REAL GmailApiClient's httpx calls
    through a MockTransport, never a hand-rolled fake standing in for
    the client class itself."""
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.google.gmail_api_client.httpx.AsyncClient", patched)


def all_day_windows() -> list[MailSendWindow]:
    return [
        MailSendWindow(
            window_id=f"w-{d}", mail_campaign_id="c1", day_of_week=d,
            start_time=time(0, 0), end_time=time(23, 59), created_at=NOW, updated_at=NOW,
        )
        for d in range(7)
    ]


def make_sequence_step(step_id: str, step_number: int, reply_in_thread: bool, subject: str) -> MailSequenceStep:
    return MailSequenceStep(
        step_id=step_id, mail_campaign_id="c1", step_number=step_number, subject=subject,
        body=f"Body {step_number}.", delay_days=0, reply_in_thread=reply_in_thread, created_at=NOW, updated_at=NOW,
    )


@pytest_asyncio.fixture
async def svc():
    campaign_store = MemoryMailCampaignStore()
    mailbox_store = MemoryMailboxStore()
    channel_store = MemoryMailCampaignMailboxStore()
    service = MailSendingService(
        campaign_store=campaign_store, enrollment_store=MemoryMailEnrollmentStore(),
        step_store=MemoryMailEnrollmentStepStore(), mailbox_store=mailbox_store, channel_store=channel_store,
        policy_store=MemoryMailboxSendPolicyStore(), suppression_store=MemoryMailSuppressionStore(),
        activity_log=ActivityLogService(MemoryActivityEventStore()),
    )
    await campaign_store.create(
        MailCampaign(mail_campaign_id="c1", name="Threading Boundary Test", status=MailCampaignStatus.ACTIVE, timezone=TZ, created_at=NOW, updated_at=NOW)
    )
    await mailbox_store.create(
        Mailbox(mailbox_id=MAILBOX_ID, provider=MailboxProvider.GOOGLE, email="victoria@useastronomic.com",
                display_name=None, status=MailboxStatus.CONNECTED, google_user_id="g-1",
                granted_scopes=["https://www.googleapis.com/auth/gmail.send"], connected_at=NOW, updated_at=NOW)
    )
    await channel_store.replace_for_campaign("c1", [MAILBOX_ID])
    return service


@pytest.fixture(autouse=True)
def _unsubscribe_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://fake.test")
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _controlled_test_gate_configured(monkeypatch):
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_mailbox_allowlist", MAILBOX_ID)
    monkeypatch.setattr("app.services.mail_sending_service.settings.mail_sending_recipient_allowlist", "lead@example.com")


async def always_leader() -> bool:
    return True


async def test_full_chain_step2_threadId_and_mime_headers_are_all_correct(svc, monkeypatch):
    """The exact test requested: captures the REAL Gmail API request body
    for both Step 1 and Step 2, through the REAL GmailSender + REAL
    GmailApiClient (only httpx is mocked), and asserts on all of:
      - Step 2's JSON request body contains threadId == Step 1's
        (mock-)Gmail-returned gmail_thread_id.
      - MIME Subject is identical between Step 1 and Step 2.
      - MIME In-Reply-To == Step 1's exact bracketed Message-ID.
      - MIME References contains Step 1's exact bracketed Message-ID.
      - Every Message-ID-family header is well-formed (single angle
        brackets, no double-bracketing, no injected whitespace)."""
    captured_requests: list[dict] = []
    gmail_message_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        gmail_message_counter["n"] += 1
        n = gmail_message_counter["n"]
        payload = json.loads(request.content)
        captured_requests.append(payload)
        # Simulate Gmail: a message sent WITH a threadId joins that
        # thread (echoes it back); one sent with none starts a new
        # thread whose id equals its own new message id -- exactly
        # Gmail's own documented behavior for a fresh conversation.
        thread_id = payload.get("threadId") or f"gmail-thread-{n}"
        return httpx.Response(200, json={"id": f"gmail-msg-{n}", "threadId": thread_id})

    _patch_transport(monkeypatch, handler)
    gmail_api_client = GmailApiClient()
    sender = GmailSender(mailbox_service=FakeMailboxService(), gmail_api_client=gmail_api_client)

    enrollment = MailEnrollment(
        enrollment_id="e1", mail_campaign_id="c1", crm_contact_id="contact-e1",
        email_at_enrollment="lead@example.com", status=MailEnrollmentStatus.ACTIVE, enrolled_at=NOW, created_at=NOW,
        assigned_mailbox_id=MAILBOX_ID,
    )
    await svc.enrollment_store.create(enrollment)

    subject = "ZZTEST Threading Production Proof - John Adrian"
    step1 = make_sequence_step("s1", 1, reply_in_thread=False, subject=subject)
    step2 = make_sequence_step("s2", 2, reply_in_thread=True, subject="irrelevant, overridden by the threading fix")

    row1 = await svc.create_step1_execution(
        enrollment=enrollment, step1=step1, windows=all_day_windows(), timezone_name=TZ, now=NOW
    )
    outcome1 = await svc.prepare_and_send_step(
        row1, sender=sender, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW, confirm_leadership=always_leader,
    )
    assert outcome1.sent is True

    row2 = await svc.step_store.get_by_enrollment_and_step("e1", "s2")
    assert row2 is not None
    outcome2 = await svc.prepare_and_send_step(
        row2, sender=sender, claimed_by="w1", sequence_steps=[step1, step2], windows=all_day_windows(),
        timezone_name=TZ, now=NOW + timedelta(seconds=60), confirm_leadership=always_leader,
    )
    assert outcome2.sent is True

    assert len(captured_requests) == 2
    step1_request, step2_request = captured_requests

    # --- Step 1: no threading context at all ---
    assert "threadId" not in step1_request
    step1_mime = message_from_bytes(base64.urlsafe_b64decode(step1_request["raw"]))
    assert step1_mime["Subject"] == subject
    step1_message_id = step1_mime["Message-ID"]
    assert step1_message_id.startswith("<") and step1_message_id.endswith(">")
    assert step1_message_id.count("<") == 1 and step1_message_id.count(">") == 1  # no double-bracketing
    assert step1_mime["In-Reply-To"] is None
    assert step1_mime["References"] is None

    # --- Step 2: real Gmail API request body, from the REAL client ---
    step1_sent_row = await svc.step_store.get(row1.enrollment_step_id)
    assert "threadId" in step2_request
    assert step2_request["threadId"] == step1_sent_row.gmail_thread_id  # Gmail's OWN returned thread id, echoed back

    step2_mime = message_from_bytes(base64.urlsafe_b64decode(step2_request["raw"]))
    assert step2_mime["Subject"] == subject == step1_mime["Subject"]

    step2_in_reply_to = step2_mime["In-Reply-To"]
    assert step2_in_reply_to == step1_message_id
    assert step2_in_reply_to.count("<") == 1 and step2_in_reply_to.count(">") == 1

    step2_references = step2_mime["References"]
    assert step2_references == step1_message_id  # exactly one prior step, so References == that one Message-ID
    assert step2_references.count("<") == 1 and step2_references.count(">") == 1

    # Message-IDs are distinct between the two messages, and neither
    # accidentally equals its own In-Reply-To/References value.
    assert step2_mime["Message-ID"] != step1_message_id
    assert step2_mime["Message-ID"] != step2_in_reply_to
