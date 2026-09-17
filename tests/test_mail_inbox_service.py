"""
Inbox V1 (2026-09-17) -- MailInboxService.list_replies(), the single
read path behind GET /mail/inbox/replies. Uses plain in-memory stores
directly (no reply-detection worker machinery needed here -- that's
already covered by test_mail_reply_detection.py) since this service is
a pure join/read over rows the caller constructs directly.

Inbox V2 (2026-09-17) -- MailInboxService.get_reply_body(), exercised
against a FakeGmailMessageBodyClient test double (never real Gmail/HTTP
-- see tests/test_gmail_message_body_client.py for that client's own
HTTP-level coverage) and a real MailboxService wired with in-memory
stores + a fake oauth client (self-contained rather than importing
tests/test_mailbox_service.py's own fake, matching this codebase's
established per-test-file convention)."""

import base64
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.google.oauth_client import GoogleRefreshTokenInvalidError
from app.google.gmail_thread_reader_client import GmailReadNotFoundError, GmailReadProviderError
from app.services import token_encryption
from app.models.crm import CrmContact
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailInboxReplyView,
    MailReply,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_reply_store import MemoryMailReplyStore
from app.repositories.mailbox_credential_store import MemoryMailboxCredentialStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.mail_inbox_service import MailInboxService
from app.services.mailbox_service import MailboxService

pytestmark = pytest.mark.asyncio


class FakeGoogleOAuthClient:
    """Only what MailboxService.refresh_mailbox_access_token() actually
    calls -- refresh_access_token(). Mirrors
    tests/test_mailbox_service.py's own FakeGoogleOAuthClient shape,
    duplicated deliberately rather than imported (per-test-file
    convention)."""

    def __init__(self):
        self.refresh_outcome = "success"
        self.refresh_response = {"access_token": "fake-refreshed-access-token", "expires_in": 3600}

    async def refresh_access_token(self, refresh_token: str) -> dict:
        if self.refresh_outcome == "invalid_grant":
            raise GoogleRefreshTokenInvalidError("simulated invalid_grant")
        return self.refresh_response


class FakeGmailMessageBodyClient:
    """Records every (access_token, message_id) it was called with -- the
    core assertion for "unrelated Gmail messages cannot be fetched
    through Inbox route" is that this is ALWAYS the reply's own
    gmail_message_id, never anything else, since get_reply_body() takes
    no message_id from its own caller at all."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.response: dict | None = None
        self.raise_error: Exception | None = None

    async def get_message_full(self, *, access_token: str, message_id: str) -> dict:
        self.calls.append((access_token, message_id))
        if self.raise_error is not None:
            raise self.raise_error
        return self.response or {"id": message_id, "threadId": "thr-x", "payload": {}}

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CAMPAIGN_ID = "1fe98436-b4e5-4459-b415-20714726f469"
MAILBOX_ID = "fd090fb1-81e1-4575-94e4-5c7c83fc0137"


def make_campaign(**overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=CAMPAIGN_ID,
        name="Campaign Manager — 5-Person External Pilot",
        status=MailCampaignStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_contact(crm_contact_id: str, first_name: str, last_name: str, email: str, **overrides) -> CrmContact:
    fields = dict(
        crm_contact_id=crm_contact_id,
        created_at=NOW,
        updated_at=NOW,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )
    fields.update(overrides)
    return CrmContact(**fields)


def make_mailbox(**overrides) -> Mailbox:
    fields = dict(
        mailbox_id=MAILBOX_ID,
        provider=MailboxProvider.GOOGLE,
        email="victoria@useastronomic.com",
        display_name=None,
        status=MailboxStatus.CONNECTED,
        google_user_id="g-victoria",
        granted_scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.metadata",
        ],
        connected_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return Mailbox(**fields)


def make_enrollment(enrollment_id: str, crm_contact_id: str, email: str, **overrides) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=CAMPAIGN_ID,
        crm_contact_id=crm_contact_id,
        email_at_enrollment=email,
        status=MailEnrollmentStatus.REPLIED,
        enrolled_at=NOW,
        created_at=NOW,
        replied_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_step1(enrollment_id: str, crm_contact_id: str, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step1",
        mail_campaign_id=CAMPAIGN_ID,
        enrollment_id=enrollment_id,
        crm_contact_id=crm_contact_id,
        step_id="step-1",
        step_number=1,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        rendered_subject="Quick hello from Astronomic",
        mailbox_id=MAILBOX_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


def make_reply(enrollment_id: str, crm_contact_id: str, email: str, detected_at: datetime = NOW, **overrides) -> MailReply:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=CAMPAIGN_ID,
        crm_contact_id=crm_contact_id,
        mailbox_id=MAILBOX_ID,
        gmail_thread_id=f"thread-{enrollment_id}",
        gmail_message_id=f"reply-msg-{enrollment_id}",
        reply_email_normalized=email,
        detected_at=detected_at,
        created_at=detected_at,
    )
    fields.update(overrides)
    return MailReply(**fields)


@pytest.fixture(autouse=True)
def configured_encryption_key(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def stores():
    mailbox_store = MemoryMailboxStore()
    oauth_client = FakeGoogleOAuthClient()
    mailbox_service = MailboxService(
        mailbox_store=mailbox_store,
        credential_store=MemoryMailboxCredentialStore(),
        oauth_client=oauth_client,
    )
    return {
        "reply_store": MemoryMailReplyStore(),
        "campaign_store": MemoryMailCampaignStore(),
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "contact_store": MemoryCrmContactStore(),
        "mailbox_store": mailbox_store,
        "mailbox_service": mailbox_service,
        "oauth_client": oauth_client,  # test-only convenience, not a MailInboxService param
    }


@pytest_asyncio.fixture
async def service(stores):
    body_client = FakeGmailMessageBodyClient()
    inbox_service = MailInboxService(
        reply_store=stores["reply_store"],
        campaign_store=stores["campaign_store"],
        enrollment_store=stores["enrollment_store"],
        enrollment_step_store=stores["enrollment_step_store"],
        contact_store=stores["contact_store"],
        mailbox_store=stores["mailbox_store"],
        mailbox_service=stores["mailbox_service"],
        body_client=body_client,
    )
    inbox_service.body_client_test_handle = body_client  # convenience for assertions
    return inbox_service


async def _seed_credential(stores, mailbox_id: str) -> None:
    from datetime import datetime, timezone as tz

    from app.models.mailbox import MailboxCredential
    from app.services.token_encryption import encrypt_refresh_token

    await stores["mailbox_service"].credential_store.create(
        MailboxCredential(
            mailbox_id=mailbox_id,
            encrypted_refresh_token=encrypt_refresh_token("fake-refresh-token"),
            created_at=datetime.now(tz.utc),
            updated_at=datetime.now(tz.utc),
        )
    )


async def test_empty_inbox_returns_empty_list(service):
    assert await service.list_replies() == []


async def test_john_and_maximus_production_equivalent_replies_both_display(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())

    john = make_contact("9e147f6d-f58f-4171-9d03-1f39751f6291", "John Adrian", "Cal", "johnadriancal@astronomic.com")
    maximus = make_contact("70590a0d-8795-4a78-aa79-01ef9ae00968", "Maximus", "Romy", "maximusromy1165@gmail.com")
    await stores["contact_store"].create(john)
    await stores["contact_store"].create(maximus)

    john_enrollment = make_enrollment("ff5cbede-4785-4f5f-9f32-0d031ce1b7fd", john.crm_contact_id, john.email)
    maximus_enrollment = make_enrollment("47d5fdf0-4aaa-4184-ba6c-ae0aa6ff0dad", maximus.crm_contact_id, maximus.email)
    await stores["enrollment_store"].create(john_enrollment)
    await stores["enrollment_store"].create(maximus_enrollment)

    await stores["enrollment_step_store"].create(make_step1(john_enrollment.enrollment_id, john.crm_contact_id))
    await stores["enrollment_step_store"].create(make_step1(maximus_enrollment.enrollment_id, maximus.crm_contact_id))

    earlier = NOW
    later = datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc)
    await stores["reply_store"].create(make_reply(john_enrollment.enrollment_id, john.crm_contact_id, john.email, detected_at=earlier))
    await stores["reply_store"].create(make_reply(maximus_enrollment.enrollment_id, maximus.crm_contact_id, maximus.email, detected_at=later))

    views = await service.list_replies()
    assert len(views) == 2

    emails = {v.email for v in views}
    assert emails == {"johnadriancal@astronomic.com", "maximusromy1165@gmail.com"}
    names = {v.contact_name for v in views}
    assert names == {"John Adrian Cal", "Maximus Romy"}


async def test_newest_reply_sorts_first(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact_a = make_contact("contact-a", "Alice", "Older", "alice@example.com")
    contact_b = make_contact("contact-b", "Bob", "Newer", "bob@example.com")
    await stores["contact_store"].create(contact_a)
    await stores["contact_store"].create(contact_b)
    enrollment_a = make_enrollment("enroll-a", contact_a.crm_contact_id, contact_a.email)
    enrollment_b = make_enrollment("enroll-b", contact_b.crm_contact_id, contact_b.email)
    await stores["enrollment_store"].create(enrollment_a)
    await stores["enrollment_store"].create(enrollment_b)
    await stores["enrollment_step_store"].create(make_step1(enrollment_a.enrollment_id, contact_a.crm_contact_id))
    await stores["enrollment_step_store"].create(make_step1(enrollment_b.enrollment_id, contact_b.crm_contact_id))

    older = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    # Deliberately created out of chronological order to prove sorting isn't
    # accidentally just insertion order.
    await stores["reply_store"].create(make_reply(enrollment_a.enrollment_id, contact_a.crm_contact_id, contact_a.email, detected_at=older))
    await stores["reply_store"].create(make_reply(enrollment_b.enrollment_id, contact_b.crm_contact_id, contact_b.email, detected_at=newer))

    views = await service.list_replies()
    assert [v.contact_name for v in views] == ["Bob Newer", "Alice Older"]


async def test_correct_campaign_name_and_status_attached(stores, service):
    await stores["campaign_store"].create(make_campaign(name="Campaign Manager — 5-Person External Pilot", status=MailCampaignStatus.ACTIVE))
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.mail_campaign_id == CAMPAIGN_ID
    assert view.campaign_name == "Campaign Manager — 5-Person External Pilot"
    assert view.campaign_status == MailCampaignStatus.ACTIVE


async def test_replied_at_matches_detected_at(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    detected = datetime(2026, 9, 17, 4, 50, 0, tzinfo=timezone.utc)
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email, detected_at=detected))

    [view] = await service.list_replies()
    assert view.replied_at == detected


async def test_contact_name_and_email_displayed(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Brendan", "Hayes", "brendan@bizdevdinners.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.contact_name == "Brendan Hayes"
    assert view.email == "brendan@bizdevdinners.com"


async def test_completed_and_paused_campaign_replies_remain_visible(stores, service):
    await stores["campaign_store"].create(make_campaign(status=MailCampaignStatus.PAUSED))
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Victoria", "Bennett", "victoria@astronomicconnect.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    views = await service.list_replies()
    assert len(views) == 1
    assert views[0].campaign_status == MailCampaignStatus.PAUSED

    # A later archive/complete doesn't hide it either.
    campaign = await stores["campaign_store"].get(CAMPAIGN_ID)
    await stores["campaign_store"].save(campaign.model_copy(update={"status": MailCampaignStatus.ARCHIVED}))
    views_after = await service.list_replies()
    assert len(views_after) == 1
    assert views_after[0].campaign_status == MailCampaignStatus.ARCHIVED


async def test_repeated_reply_polling_never_produces_a_duplicate_row(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))

    reply = make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email)
    first = await stores["reply_store"].create(reply)
    second = await stores["reply_store"].create(reply)
    assert first is True
    assert second is False

    views = await service.list_replies()
    assert len(views) == 1


async def test_missing_contact_and_mailbox_degrade_gracefully_not_hidden(stores, service):
    await stores["campaign_store"].create(make_campaign())
    # Deliberately never create a contact or mailbox row.
    enrollment = make_enrollment("e1", "ghost-contact", "ghost@example.com")
    await stores["enrollment_store"].create(enrollment)
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, "ghost-contact", "ghost@example.com"))

    [view] = await service.list_replies()
    assert view.contact_name is None
    assert view.mailbox_email is None
    assert view.email == "ghost@example.com"


async def test_missing_enrollment_or_campaign_drops_the_row_rather_than_fabricating_one(stores, service):
    # No campaign, no enrollment ever created for this reply.
    await stores["reply_store"].create(make_reply("orphan-enrollment", "orphan-contact", "orphan@example.com"))
    assert await service.list_replies() == []


async def test_subject_falls_back_from_rendered_subject_to_frozen_subject(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    # A step predating the rendered_subject field (None) still falls back
    # to the frozen `subject` -- see MailEnrollmentStep's own docstring.
    await stores["enrollment_step_store"].create(
        make_step1(enrollment.enrollment_id, contact.crm_contact_id, rendered_subject=None, subject="Quick hello from Astronomic")
    )
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.subject == "Quick hello from Astronomic"


async def test_skipped_step_numbers_reflect_steps_the_reply_actually_skipped(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["enrollment_step_store"].create(
        make_step1(
            enrollment.enrollment_id, contact.crm_contact_id,
            enrollment_step_id="e1-step2", step_id="step-2", step_number=2,
            status=MailEnrollmentStepStatus.SKIPPED_REPLIED, sent_at=None, rendered_subject=None,
        )
    )
    await stores["enrollment_step_store"].create(
        make_step1(
            enrollment.enrollment_id, contact.crm_contact_id,
            enrollment_step_id="e1-step3", step_id="step-3", step_number=3,
            status=MailEnrollmentStepStatus.SKIPPED_REPLIED, sent_at=None, rendered_subject=None,
        )
    )
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.skipped_step_numbers == [2, 3]


async def test_inbox_reply_view_has_no_body_or_unread_state():
    """Structural guard: the Inbox LIST view must never grow a fake
    body/snippet or an invented unread/read field -- body content is
    only ever fetched on demand via get_reply_body(), a separate call
    the list itself never makes."""
    field_names = set(MailInboxReplyView.model_fields.keys())
    assert "body" not in field_names
    assert "snippet" not in field_names
    assert "unread" not in field_names
    assert "read" not in field_names


# --- Inbox V2: get_reply_body() ---------------------------------------------


async def _seed_reply_with_scope(stores, *, granted_scopes: list[str], reply_message_id: str = "reply-msg-1") -> str:
    """Returns the enrollment_id of a fully-wired, ready-to-fetch reply."""
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox(granted_scopes=granted_scopes))
    contact = make_contact("c1", "Maximus", "Romy", "maximusromy1165@gmail.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(
        make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email, gmail_message_id=reply_message_id)
    )
    return enrollment.enrollment_id


READONLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/gmail.readonly",
]


async def test_known_mail_reply_retrieves_body_by_its_own_reply_message_id(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES, reply_message_id="reply-msg-xyz")
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {
        "id": "reply-msg-xyz", "threadId": "thr-x",
        "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Got it, thanks!").decode().rstrip("=")}},
    }

    result = await service.get_reply_body(enrollment_id)

    assert result.status == "ok"
    assert result.body_text == "Got it, thanks!"
    assert result.body_source == "plain"
    # The client was called with EXACTLY this reply's own message id -- never anything else.
    assert service.body_client_test_handle.calls == [("fake-refreshed-access-token", "reply-msg-xyz")]


async def test_plain_text_reply_renders(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {
        "id": "reply-msg-1", "threadId": "thr-x",
        "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Sounds good.").decode().rstrip("=")}},
    }

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "ok"
    assert result.body_text == "Sounds good."
    assert result.body_source == "plain"


async def test_html_only_reply_renders_safely(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    html = "<p>Sounds <b>good</b>!</p>"
    service.body_client_test_handle.response = {
        "id": "reply-msg-1", "threadId": "thr-x",
        "payload": {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")}},
    }

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "ok"
    assert result.body_source == "html_converted"
    assert "<" not in result.body_text
    assert "Sounds good!" in result.body_text


async def test_multipart_reply_chooses_plain_text_over_html(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {
        "id": "reply-msg-1", "threadId": "thr-x",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Plain reply").decode().rstrip("=")}},
                {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>HTML reply</p>").decode().rstrip("=")}},
            ],
        },
    }

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "ok"
    assert result.body_source == "plain"
    assert result.body_text == "Plain reply"


async def test_unrelated_gmail_messages_cannot_be_fetched_through_inbox(stores, service):
    """The service takes ONLY enrollment_id -- there is no parameter
    anywhere that lets a caller name an arbitrary Gmail message_id, so
    the body_client is structurally guaranteed to only ever be asked
    for the reply's own, already-known gmail_message_id."""
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES, reply_message_id="the-only-allowed-id")
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {"id": "the-only-allowed-id", "threadId": "thr-x", "payload": {}}

    await service.get_reply_body(enrollment_id)

    assert all(message_id == "the-only-allowed-id" for _token, message_id in service.body_client_test_handle.calls)
    import inspect

    assert "message_id" not in inspect.signature(service.get_reply_body).parameters


async def test_missing_read_scope_returns_clear_scope_missing_status(stores, service):
    enrollment_id = await _seed_reply_with_scope(
        stores, granted_scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.metadata"]
    )

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "scope_missing"
    assert result.message
    assert service.body_client_test_handle.calls == []  # never even attempted the Gmail call


async def test_needs_reauth_when_google_reports_invalid_grant(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    stores["oauth_client"].refresh_outcome = "invalid_grant"

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "needs_reauth"
    assert result.message


async def test_gmail_api_failure_falls_back_gracefully_not_an_exception(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.raise_error = GmailReadProviderError("simulated 500")

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "provider_error"
    assert result.message


async def test_message_not_found_on_gmail_returns_not_found_status(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.raise_error = GmailReadNotFoundError("simulated 404")

    result = await service.get_reply_body(enrollment_id)
    assert result.status == "not_found"


async def test_john_production_equivalent_reply_body(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox(granted_scopes=READONLY_SCOPES))
    john = make_contact("9e147f6d-f58f-4171-9d03-1f39751f6291", "John Adrian", "Cal", "johnadriancal@astronomic.com")
    await stores["contact_store"].create(john)
    enrollment = make_enrollment("john-e1", john.crm_contact_id, john.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, john.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, john.crm_contact_id, john.email, gmail_message_id="john-reply-msg"))
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {
        "id": "john-reply-msg", "threadId": "thr-john",
        "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Got it -- thanks Victoria!").decode().rstrip("=")}},
    }

    result = await service.get_reply_body(enrollment.enrollment_id)
    assert result.status == "ok"
    assert result.body_text == "Got it -- thanks Victoria!"


async def test_maximus_production_equivalent_reply_body(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox(granted_scopes=READONLY_SCOPES))
    maximus = make_contact("70590a0d-8795-4a78-aa79-01ef9ae00968", "Maximus", "Romy", "maximusromy1165@gmail.com")
    await stores["contact_store"].create(maximus)
    enrollment = make_enrollment("max-e1", maximus.crm_contact_id, maximus.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, maximus.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, maximus.crm_contact_id, maximus.email, gmail_message_id="max-reply-msg"))
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.response = {
        "id": "max-reply-msg", "threadId": "thr-max",
        "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Got it, will do.").decode().rstrip("=")}},
    }

    result = await service.get_reply_body(enrollment.enrollment_id)
    assert result.status == "ok"
    assert result.body_text == "Got it, will do."


async def test_live_reply_stop_logic_untouched_by_get_reply_body(stores, service):
    """get_reply_body() must never write to enrollment/step stores -- it
    is a pure Gmail read plus a status classification, nothing else."""
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    before_enrollment = await stores["enrollment_store"].get(enrollment_id)
    before_steps = await stores["enrollment_step_store"].list_for_enrollment(enrollment_id)

    await service.get_reply_body(enrollment_id)

    after_enrollment = await stores["enrollment_store"].get(enrollment_id)
    after_steps = await stores["enrollment_step_store"].list_for_enrollment(enrollment_id)
    assert before_enrollment == after_enrollment
    assert before_steps == after_steps


async def test_existing_mail_reply_row_remains_unchanged_after_get_reply_body(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    before = await stores["reply_store"].get(enrollment_id)

    await service.get_reply_body(enrollment_id)

    after = await stores["reply_store"].get(enrollment_id)
    assert before == after


async def test_inbox_list_still_works_after_a_body_fetch_failure(stores, service):
    enrollment_id = await _seed_reply_with_scope(stores, granted_scopes=READONLY_SCOPES)
    await _seed_credential(stores, MAILBOX_ID)
    service.body_client_test_handle.raise_error = GmailReadProviderError("simulated 500")

    body_result = await service.get_reply_body(enrollment_id)
    assert body_result.status == "provider_error"

    # The list itself is completely independent -- unaffected by the failure above.
    views = await service.list_replies()
    assert len(views) == 1
    assert views[0].enrollment_id == enrollment_id


async def test_get_reply_body_returns_not_found_for_unknown_enrollment(stores, service):
    result = await service.get_reply_body("no-such-enrollment")
    assert result.status == "not_found"
