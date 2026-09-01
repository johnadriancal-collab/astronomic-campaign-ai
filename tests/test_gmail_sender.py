"""
GmailSender -- exercises the composition of MailboxService.
refresh_mailbox_access_token() + gmail_mime + GmailApiClient, using fakes
for both collaborators (matching this codebase's established convention
of faking the outer boundary client -- see tests/test_mailbox_service.py's
FakeGoogleOAuthClient -- rather than mocking httpx internals here; httpx
itself is already covered directly by tests/test_gmail_api_client.py).

B2 HARDENING PASS: GmailSender.send() now takes a single MailSendRequest
(app/services/mail_sending_service.py) rather than keyword arguments --
these tests construct that request explicitly and assert GmailSender
never regenerates/replaces its rfc_message_id or threading fields.
"""

from datetime import datetime, timezone

import pytest

from app.google.gmail_api_client import GmailPermissionError, GmailRateLimitedError
from app.google.gmail_sender import GmailSender
from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.services.mail_sending_service import MailSendRequest, SendResult

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_mailbox(email: str = "victoria@astronomic.com") -> Mailbox:
    return Mailbox(
        mailbox_id="mb-1",
        provider=MailboxProvider.GOOGLE,
        email=email,
        display_name="Victoria",
        status=MailboxStatus.CONNECTED,
        google_user_id="sub-1",
        granted_scopes=["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.send"],
        connected_at=NOW,
        updated_at=NOW,
    )


def make_request(**overrides) -> MailSendRequest:
    defaults = dict(
        mailbox=make_mailbox(),
        to_email="lead@example.com",
        subject="Hi",
        body="Body",
        rfc_message_id="fixed-id-123@astronomic.com",
        reply_in_thread=False,
    )
    defaults.update(overrides)
    return MailSendRequest(**defaults)


class FakeMailboxService:
    """Duck-types the one method GmailSender actually calls --
    refresh_mailbox_access_token() -- without depending on real
    MailboxService construction (real DB stores, Fernet key, etc.)."""

    def __init__(self, access_token: str = "fresh-access-token", raise_error: Exception | None = None):
        self.access_token = access_token
        self.raise_error = raise_error
        self.calls: list[str] = []

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        self.calls.append(mailbox_id)
        if self.raise_error is not None:
            raise self.raise_error
        return self.access_token


class FakeGmailApiClient:
    def __init__(self, result: dict | None = None, raise_error: Exception | None = None):
        self.result = result or {"id": "gmail-msg-1", "threadId": "gmail-thr-1"}
        self.raise_error = raise_error
        self.calls: list[dict] = []

    async def send_message(self, *, access_token: str, raw_message: str, thread_id: str | None = None) -> dict:
        self.calls.append({"access_token": access_token, "raw_message": raw_message, "thread_id": thread_id})
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


# --- Success path --------------------------------------------------------------


async def test_send_returns_a_send_result_built_from_the_gmail_response():
    sender = GmailSender(FakeMailboxService(), FakeGmailApiClient())

    result = await sender.send(make_request())

    assert isinstance(result, SendResult)
    assert result.provider_message_id == "gmail-msg-1"
    assert result.provider_thread_id == "gmail-thr-1"


async def test_refreshes_the_access_token_for_the_correct_mailbox_before_sending():
    mailbox_service = FakeMailboxService()
    sender = GmailSender(mailbox_service, FakeGmailApiClient())

    await sender.send(make_request())

    assert mailbox_service.calls == ["mb-1"]


async def test_the_fresh_access_token_is_passed_to_the_gmail_api_client():
    mailbox_service = FakeMailboxService(access_token="tok-xyz")
    api_client = FakeGmailApiClient()
    sender = GmailSender(mailbox_service, api_client)

    await sender.send(make_request())

    assert api_client.calls[0]["access_token"] == "tok-xyz"


async def test_no_access_token_is_retained_on_the_sender_after_send():
    """Never persisted anywhere -- not even transiently on `self` after
    the call returns."""
    mailbox_service = FakeMailboxService(access_token="tok-should-not-linger")
    sender = GmailSender(mailbox_service, FakeGmailApiClient())

    await sender.send(make_request())

    for attr_name in dir(sender):
        if attr_name.startswith("_"):
            continue
        value = getattr(sender, attr_name)
        if isinstance(value, str):
            assert "tok-should-not-linger" not in value


async def test_the_raw_mime_sent_is_base64url_and_contains_the_recipient_and_subject():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    await sender.send(make_request(to_email="lead@example.com", subject="A specific subject", body="A specific body."))

    raw = api_client.calls[0]["raw_message"]
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    assert "To: lead@example.com" in decoded
    assert "A specific subject" in decoded
    assert "A specific body." in decoded


# --- Message-ID: execution-owned, consumed verbatim (B2 hardening pass) --------


async def test_send_uses_the_caller_supplied_message_id_exactly():
    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    result = await sender.send(make_request(rfc_message_id="caller-chosen-id-999@astronomic.com"))

    assert result.rfc_message_id == "caller-chosen-id-999@astronomic.com"


async def test_the_supplied_message_id_appears_verbatim_in_the_mime_sent_to_gmail():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    await sender.send(make_request(rfc_message_id="verbatim-check-id@astronomic.com"))

    raw = api_client.calls[0]["raw_message"]
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    assert "Message-ID: <verbatim-check-id@astronomic.com>" in decoded


async def test_two_calls_with_the_same_persisted_request_use_the_same_message_id():
    """Simulates a retry/reconciliation pass reusing a persisted request
    (same rfc_message_id) rather than generating a fresh one -- this is
    exactly the determinism-per-attempt property the B2 hardening pass
    moved from 'generate deterministically' to 'persist and reuse'."""
    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)
    request = make_request(rfc_message_id="same-id-across-retries@astronomic.com")

    result1 = await sender.send(request)
    result2 = await sender.send(request)

    assert result1.rfc_message_id == result2.rfc_message_id == "same-id-across-retries@astronomic.com"


async def test_gmail_sender_never_calls_the_message_id_generator():
    """A structural guarantee, not just a behavioral one: GmailSender's
    own module must not IMPORT (and therefore cannot call)
    generate_rfc_message_id -- Message-ID generation now belongs
    exclusively to the execution layer (app/services/rfc_message_id.py).
    The module docstring is allowed to mention the function name in
    prose (explaining where it moved to); an actual import is not."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/google/gmail_sender.py").read_text())
    imported_names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    imported_modules = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert "generate_rfc_message_id" not in imported_names
    assert "uuid" not in imported_modules


async def test_malformed_message_id_is_rejected_before_any_gmail_api_client_call():
    """MailSendRequest itself is the gate -- constructing one with a bad
    ID raises before GmailSender (or GmailApiClient) is ever involved."""
    from app.services.mail_sending_service import MailSendRequestValidationError

    api_client = FakeGmailApiClient()
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="not-a-valid-message-id")  # no '@'

    assert api_client.calls == []


# --- Token refresh integration (B1 boundary) ------------------------------------


async def test_invalid_grant_from_refresh_propagates_unwrapped():
    """GmailSender must not catch/reinterpret GoogleRefreshTokenInvalidError
    -- the NEEDS_REAUTH transition already happened inside
    MailboxService.refresh_mailbox_access_token() itself (B1, unchanged);
    this class must not duplicate or second-guess that decision."""
    mailbox_service = FakeMailboxService(raise_error=GoogleRefreshTokenInvalidError("invalid"))
    sender = GmailSender(mailbox_service, FakeGmailApiClient())

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await sender.send(make_request())


async def test_an_ordinary_refresh_failure_also_propagates_and_is_not_swallowed():
    mailbox_service = FakeMailboxService(raise_error=GoogleTokenRefreshError("network blip"))
    sender = GmailSender(mailbox_service, FakeGmailApiClient())

    with pytest.raises(GoogleTokenRefreshError):
        await sender.send(make_request())


async def test_gmail_api_client_is_never_called_when_token_refresh_fails():
    api_client = FakeGmailApiClient()
    mailbox_service = FakeMailboxService(raise_error=GoogleRefreshTokenInvalidError("invalid"))
    sender = GmailSender(mailbox_service, api_client)

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await sender.send(make_request())

    assert api_client.calls == []


# --- Gmail API error propagation -------------------------------------------------


async def test_gmail_permission_error_propagates_unwrapped():
    api_client = FakeGmailApiClient(raise_error=GmailPermissionError("no scope"))
    sender = GmailSender(FakeMailboxService(), api_client)

    with pytest.raises(GmailPermissionError):
        await sender.send(make_request())


async def test_gmail_rate_limited_error_propagates_unwrapped():
    api_client = FakeGmailApiClient(raise_error=GmailRateLimitedError("slow down"))
    sender = GmailSender(FakeMailboxService(), api_client)

    with pytest.raises(GmailRateLimitedError):
        await sender.send(make_request())


# --- Threading (request-driven, B2 hardening pass) --------------------------------


async def test_threading_fields_survive_request_to_mime_and_api_construction_exactly():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    await sender.send(
        make_request(
            subject="Re: s",
            reply_in_thread=True,
            in_reply_to_message_id="prior123@astronomic.com",
            references=("prior123@astronomic.com",),
            thread_id="gmail-thread-existing",
        )
    )

    assert api_client.calls[0]["thread_id"] == "gmail-thread-existing"
    decoded = base64.urlsafe_b64decode(api_client.calls[0]["raw_message"].encode("ascii")).decode("utf-8")
    assert "In-Reply-To: <prior123@astronomic.com>" in decoded
    assert "References: <prior123@astronomic.com>" in decoded


async def test_new_thread_send_has_no_threading_headers_or_thread_id():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    await sender.send(make_request(reply_in_thread=False))

    assert api_client.calls[0]["thread_id"] is None
    decoded = base64.urlsafe_b64decode(api_client.calls[0]["raw_message"].encode("ascii")).decode("utf-8")
    assert "In-Reply-To:" not in decoded
    assert "References:" not in decoded
