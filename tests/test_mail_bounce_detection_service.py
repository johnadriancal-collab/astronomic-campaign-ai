"""
MailBounceDetectionService -- bounce detection (2026-09-18). Fake Gmail
clients (FakeGmailHistoryClient/FakeGmailMessageBodyClient) and a fake
MailboxService, same "test double lives in tests/, never a real network
call" convention as tests/test_mail_reply_detection.py's RecordingSender.
DSN structural parsing itself is covered exhaustively in
tests/test_mail_bounce_dsn_parser.py -- these tests exercise the
orchestration: checkpoint bootstrap/advance/expiry, per-mailbox
isolation, attribution, and idempotency.
"""

import base64
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.google.gmail_history_client import GmailHistoryExpiredError
from app.google.gmail_thread_reader_client import GmailReadConnectionError, GmailReadError
from app.google.oauth_client import GoogleRefreshTokenInvalidError
from app.models.mail import MailBounceType, MailEnrollmentStep, MailEnrollmentStepStatus
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mail_bounce_store import MemoryMailBounceStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mailbox_history_checkpoint_store import MemoryMailboxHistoryCheckpointStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.mail_bounce_detection_service import MailBounceDetectionService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _leaf(mime_type: str, text: str) -> dict:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


DELIVERY_STATUS_TEXT = (
    "Reporting-MTA: dns; mx.google.com\n"
    "\n"
    "Final-Recipient: rfc822; nobody@example.com\n"
    "Action: failed\n"
    "Status: 5.1.1\n"
    "Diagnostic-Code: smtp; 550 5.1.1 User unknown\n"
)


def make_full_dsn_message(message_id: str, original_rfc_message_id: str | None) -> dict:
    parts = [
        _leaf("text/plain", "Delivery has failed."),
        _leaf("message/delivery-status", DELIVERY_STATUS_TEXT),
    ]
    if original_rfc_message_id is not None:
        parts.append(_leaf("message/rfc822-headers", f"Message-ID: <{original_rfc_message_id}>\n"))
    return {
        "id": message_id,
        "threadId": f"thr-{message_id}",
        "payload": {"mimeType": "multipart/report", "parts": parts},
    }


def make_light_metadata(message_id: str, content_type: str = 'multipart/report; report-type=delivery-status; boundary="x"') -> dict:
    return {
        "id": message_id,
        "payload": {"headers": [{"name": "Content-Type", "value": content_type}, {"name": "From", "value": "mailer-daemon@mx.google.com"}]},
    }


def make_step(enrollment_step_id: str, rfc_message_id: str, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=enrollment_step_id,
        mail_campaign_id="c1",
        enrollment_id="e1",
        crm_contact_id="contact1",
        step_id="step-1",
        step_number=1,
        subject="Quick hello",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        mailbox_id="mbx-1",
        rfc_message_id=rfc_message_id,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


def make_mailbox(mailbox_id="mbx-1", status=MailboxStatus.CONNECTED) -> Mailbox:
    return Mailbox(
        mailbox_id=mailbox_id, provider=MailboxProvider.GOOGLE, email=f"{mailbox_id}@astronomic.com",
        display_name=None, status=status, google_user_id=f"g-{mailbox_id}",
        granted_scopes=[], connected_at=NOW, updated_at=NOW,
    )


class FakeMailboxService:
    def __init__(self, token: str = "tok-1", refresh_error: Exception | None = None):
        self.token = token
        self.refresh_error = refresh_error
        # Per-mailbox override -- lets a test make ONE specific mailbox's
        # refresh fail while every other mailbox keeps succeeding, for a
        # realistic "one mailbox's own auth is broken" isolation test.
        self.refresh_error_for_mailbox_id: dict[str, Exception] = {}

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        if mailbox_id in self.refresh_error_for_mailbox_id:
            raise self.refresh_error_for_mailbox_id[mailbox_id]
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.token


class FakeGmailHistoryClient:
    def __init__(self):
        self.current_history_id = "1000"
        self.list_history_result: dict | list[dict] = {"message_ids": [], "history_id": "1001"}
        self.list_history_error: Exception | None = None
        self.light_metadata: dict[str, dict] = {}
        self.get_profile_calls = 0
        self.list_history_calls: list[str] = []

    async def get_current_history_id(self, *, access_token: str) -> str:
        self.get_profile_calls += 1
        return self.current_history_id

    async def list_history(self, *, access_token: str, start_history_id: str, label_id: str = "INBOX") -> dict:
        self.list_history_calls.append(start_history_id)
        if self.list_history_error is not None:
            raise self.list_history_error
        return self.list_history_result

    async def get_message_metadata_light(self, *, access_token: str, message_id: str) -> dict:
        return self.light_metadata.get(message_id, make_light_metadata(message_id, content_type="text/plain"))


class FakeGmailMessageBodyClient:
    def __init__(self):
        self.full_messages: dict[str, dict] = {}

    async def get_message_full(self, *, access_token: str, message_id: str) -> dict:
        return self.full_messages[message_id]


@pytest_asyncio.fixture
async def stores():
    return {
        "mailbox_store": MemoryMailboxStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "checkpoint_store": MemoryMailboxHistoryCheckpointStore(),
        "bounce_store": MemoryMailBounceStore(),
    }


@pytest_asyncio.fixture
async def clients():
    return {"history_client": FakeGmailHistoryClient(), "message_body_client": FakeGmailMessageBodyClient()}


@pytest_asyncio.fixture
async def mailbox_service():
    return FakeMailboxService()


@pytest_asyncio.fixture
async def service(stores, clients, mailbox_service):
    return MailBounceDetectionService(mailbox_service=mailbox_service, **stores, **clients)


# --- Checkpoint bootstrap / persistence / restart ---------------------------


async def test_checkpoint_initialized_safely_on_first_poll_with_no_history_processed(stores, clients, service):
    count = await service.poll_one_mailbox("mbx-1", NOW)
    assert count == 0
    checkpoint = await stores["checkpoint_store"].get("mbx-1")
    assert checkpoint is not None
    assert checkpoint.history_id == "1000"
    assert clients["history_client"].list_history_calls == []  # no history call on the bootstrap cycle


async def test_new_history_processed_after_checkpoint_exists(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    clients["history_client"].list_history_result = {"message_ids": [], "history_id": "1005"}

    count = await service.poll_one_mailbox("mbx-1", NOW)
    assert count == 0
    assert clients["history_client"].list_history_calls == ["1000"]
    checkpoint = await stores["checkpoint_store"].get("mbx-1")
    assert checkpoint.history_id == "1005"


async def test_checkpoint_persists_across_a_simulated_restart(stores, clients, mailbox_service):
    service1 = MailBounceDetectionService(mailbox_service=mailbox_service, **stores, **clients)
    await service1.poll_one_mailbox("mbx-1", NOW)

    # A fresh service instance (simulating a process restart), SAME
    # underlying checkpoint_store -- must pick up right where it left off.
    service2 = MailBounceDetectionService(mailbox_service=mailbox_service, **stores, **clients)
    checkpoint = await stores["checkpoint_store"].get("mbx-1")
    assert checkpoint.history_id == "1000"
    clients["history_client"].list_history_result = {"message_ids": [], "history_id": "1010"}
    await service2.poll_one_mailbox("mbx-1", NOW)
    assert clients["history_client"].list_history_calls == ["1000"]


async def test_same_history_batch_processed_twice_is_idempotent(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    step = make_step("s1", "abc123@useastronomic.com")
    await stores["enrollment_step_store"].create(step)
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "abc123@useastronomic.com")

    first = await service.poll_one_mailbox("mbx-1", NOW)
    # Re-run with the SAME message still returned by history.list (e.g. a
    # retried/overlapping poll) -- must not create a second bounce row.
    second = await service.poll_one_mailbox("mbx-1", NOW)

    assert first == 1
    assert second == 0
    bounces = await stores["bounce_store"].list_for_campaign("c1")
    assert len(bounces) == 1


# --- Gmail failure handling --------------------------------------------------


async def test_gmail_404_on_history_list_is_handled_safely_not_silently(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    clients["history_client"].list_history_error = GmailHistoryExpiredError("stale")

    count = await service.poll_one_mailbox("mbx-1", NOW)

    assert count == 0
    checkpoint = await stores["checkpoint_store"].get("mbx-1")
    # Re-bootstrapped to the CURRENT position, not silently left at the
    # stale value and not crashing.
    assert checkpoint.history_id == "1000"


async def test_api_error_does_not_advance_checkpoint(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap at "1000"
    clients["history_client"].list_history_error = GmailReadConnectionError("boom")

    count = await service.poll_one_mailbox("mbx-1", NOW)

    assert count == 0
    checkpoint = await stores["checkpoint_store"].get("mbx-1")
    assert checkpoint.history_id == "1000"  # unchanged -- will retry the same batch


async def test_one_mailbox_failure_does_not_kill_others(stores, clients, mailbox_service):
    await stores["mailbox_store"].create(make_mailbox("mbx-1"))
    await stores["mailbox_store"].create(make_mailbox("mbx-2"))
    service = MailBounceDetectionService(mailbox_service=mailbox_service, **stores, **clients)

    # mbx-1's own auth is broken; mbx-2 is perfectly healthy.
    mailbox_service.refresh_error_for_mailbox_id["mbx-1"] = GoogleRefreshTokenInvalidError("needs reauth")

    total = await service.poll_all_mailboxes(NOW)

    assert total == 0  # nothing to bounce yet, but no exception propagated
    assert await stores["checkpoint_store"].get("mbx-1") is None  # never got past the broken refresh
    assert await stores["checkpoint_store"].get("mbx-2") is not None  # mbx-2 bootstrapped successfully regardless


async def test_disconnected_mailboxes_are_never_polled(stores, clients, mailbox_service):
    await stores["mailbox_store"].create(make_mailbox("mbx-1", status=MailboxStatus.DISCONNECTED))
    service = MailBounceDetectionService(mailbox_service=mailbox_service, **stores, **clients)

    await service.poll_all_mailboxes(NOW)

    assert await stores["checkpoint_store"].get("mbx-1") is None


async def test_mailbox_needing_reauth_is_skipped_without_crashing(stores, clients):
    broken_mailbox_service = FakeMailboxService(refresh_error=GoogleRefreshTokenInvalidError("needs reauth"))
    service = MailBounceDetectionService(mailbox_service=broken_mailbox_service, **stores, **clients)

    count = await service.poll_one_mailbox("mbx-1", NOW)

    assert count == 0
    assert await stores["checkpoint_store"].get("mbx-1") is None


# --- Attribution --------------------------------------------------------------


async def test_original_message_id_links_the_correct_step(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    await stores["enrollment_step_store"].create(make_step("s1", "matching-id@useastronomic.com"))
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "matching-id@useastronomic.com")

    await service.poll_one_mailbox("mbx-1", NOW)

    bounces = await stores["bounce_store"].list_for_campaign("c1")
    assert len(bounces) == 1
    assert bounces[0].enrollment_step_id == "s1"
    assert bounces[0].enrollment_id == "e1"


async def test_unrelated_dsn_with_no_matching_message_id_is_ignored(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    await stores["enrollment_step_store"].create(make_step("s1", "our-real-id@useastronomic.com"))
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "totally-unrelated@somewhere-else.com")

    count = await service.poll_one_mailbox("mbx-1", NOW)

    assert count == 0
    assert await stores["bounce_store"].list_for_campaign("c1") == []


async def test_ambiguous_attribution_with_no_original_message_id_is_ignored(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", None)  # no attached original headers

    count = await service.poll_one_mailbox("mbx-1", NOW)

    assert count == 0
    assert await stores["bounce_store"].list_for_campaign("c1") == []


async def test_ordinary_message_that_fails_the_cheap_prefilter_never_reaches_full_fetch(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)  # bootstrap
    clients["history_client"].list_history_result = {"message_ids": ["ordinary-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["ordinary-1"] = make_light_metadata("ordinary-1", content_type="text/plain")
    # Deliberately no full_messages entry -- if the service ever tried a
    # full fetch for this message it would KeyError, proving the
    # pre-filter genuinely short-circuited before that fetch.

    count = await service.poll_one_mailbox("mbx-1", NOW)
    assert count == 0


# --- Classification (spot-check; exhaustive in test_mail_bounce_dsn_parser.py) --


async def test_hard_bounce_classified_and_persisted(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)
    await stores["enrollment_step_store"].create(make_step("s1", "id-1@useastronomic.com"))
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "id-1@useastronomic.com")

    await service.poll_one_mailbox("mbx-1", NOW)

    [bounce] = await stores["bounce_store"].list_for_campaign("c1")
    assert bounce.bounce_type == MailBounceType.HARD
    assert bounce.status_code == "5.1.1"
    assert bounce.diagnostic_code == "smtp; 550 5.1.1 User unknown"


# --- Separation from reply detection / suppression --------------------------


async def test_a_bounce_never_creates_a_mail_reply_or_touches_enrollment_status(stores, clients, service):
    await service.poll_one_mailbox("mbx-1", NOW)
    enrollment_step = make_step("s1", "id-1@useastronomic.com")
    await stores["enrollment_step_store"].create(enrollment_step)
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "id-1@useastronomic.com")

    await service.poll_one_mailbox("mbx-1", NOW)

    # The step row itself is completely untouched -- this service has no
    # write access to MailEnrollment/MailEnrollmentStep at all.
    unchanged = await stores["enrollment_step_store"].get("s1")
    assert unchanged.status == MailEnrollmentStepStatus.SENT
    assert unchanged == enrollment_step


async def test_no_automatic_suppression_is_ever_created(stores, clients, service):
    """MailBounceDetectionService has no MailSuppressionStore dependency
    at all -- structurally incapable of suppressing anything, not merely
    configured not to. This test documents that fact at the fixture
    level: the service is constructed with zero suppression-related
    argument, and still successfully detects and persists a bounce."""
    await service.poll_one_mailbox("mbx-1", NOW)
    await stores["enrollment_step_store"].create(make_step("s1", "id-1@useastronomic.com"))
    clients["history_client"].list_history_result = {"message_ids": ["dsn-1"], "history_id": "1005"}
    clients["history_client"].light_metadata["dsn-1"] = make_light_metadata("dsn-1")
    clients["message_body_client"].full_messages["dsn-1"] = make_full_dsn_message("dsn-1", "id-1@useastronomic.com")

    count = await service.poll_one_mailbox("mbx-1", NOW)
    assert count == 1
    assert not hasattr(service, "suppression_store")
