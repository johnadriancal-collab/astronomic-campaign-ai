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
from app.google.gmail_sender import GmailScopeMissingError, GmailSender, PreparedGmailSend
from app.google.oauth_client import GoogleRefreshTokenInvalidError, GoogleTokenRefreshError
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.services.mail_sending_service import MailSendRequest, SendOutcomeCertainty, SendResult

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


# --- prepare() / send_prepared() split (Phase C) --------------------------------


async def test_prepare_returns_a_prepared_gmail_send_without_calling_the_api_client():
    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    prepared = await sender.prepare(make_request())

    assert isinstance(prepared, PreparedGmailSend)
    assert api_client.calls == []


async def test_prepare_carries_the_access_token_raw_message_thread_id_and_message_id():
    mailbox_service = FakeMailboxService(access_token="tok-abc")
    sender = GmailSender(mailbox_service, FakeGmailApiClient())

    prepared = await sender.prepare(
        make_request(
            rfc_message_id="prep-id-1@astronomic.com",
            reply_in_thread=True,
            thread_id="thr-1",
            in_reply_to_message_id="prior@astronomic.com",
            references=("prior@astronomic.com",),
        )
    )

    assert prepared.access_token == "tok-abc"
    assert prepared.thread_id == "thr-1"
    assert prepared.rfc_message_id == "prep-id-1@astronomic.com"


async def test_send_prepared_makes_exactly_one_api_call_and_no_token_refresh():
    mailbox_service = FakeMailboxService()
    api_client = FakeGmailApiClient()
    sender = GmailSender(mailbox_service, api_client)
    prepared = await sender.prepare(make_request())
    mailbox_service.calls.clear()

    result = await sender.send_prepared(prepared)

    assert isinstance(result, SendResult)
    assert len(api_client.calls) == 1
    assert mailbox_service.calls == []  # send_prepared() never touches the mailbox/OAuth boundary


async def test_send_prepared_passes_the_prepared_access_token_raw_message_and_thread_id_verbatim():
    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)
    prepared = PreparedGmailSend(
        access_token="verbatim-token", raw_message="verbatim-raw", thread_id="verbatim-thread", rfc_message_id="verbatim-id@astronomic.com"
    )

    await sender.send_prepared(prepared)

    assert api_client.calls[0] == {"access_token": "verbatim-token", "raw_message": "verbatim-raw", "thread_id": "verbatim-thread"}


async def test_send_combines_prepare_and_send_prepared_with_identical_result_to_calling_them_separately():
    api_client = FakeGmailApiClient()
    sender_combined = GmailSender(FakeMailboxService(), api_client)
    sender_split = GmailSender(FakeMailboxService(), FakeGmailApiClient(result=api_client.result))

    combined_result = await sender_combined.send(make_request(rfc_message_id="combined@astronomic.com"))
    split_result = await sender_split.send_prepared(await sender_split.prepare(make_request(rfc_message_id="combined@astronomic.com")))

    assert combined_result == split_result


# --- GmailScopeMissingError: missing gmail.send scope (Phase C) -----------------


def make_mailbox_missing_send_scope() -> Mailbox:
    return Mailbox(
        mailbox_id="mb-1",
        provider=MailboxProvider.GOOGLE,
        email="victoria@astronomic.com",
        display_name="Victoria",
        status=MailboxStatus.CONNECTED,
        google_user_id="sub-1",
        granted_scopes=["openid", "email", "profile"],  # no gmail.send
        connected_at=NOW,
        updated_at=NOW,
    )


async def test_prepare_raises_scope_missing_error_when_mailbox_lacks_gmail_send_scope():
    sender = GmailSender(FakeMailboxService(), FakeGmailApiClient())

    with pytest.raises(GmailScopeMissingError):
        await sender.prepare(make_request(mailbox=make_mailbox_missing_send_scope()))


async def test_scope_missing_error_is_definitely_not_sent():
    assert GmailScopeMissingError.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_scope_check_happens_before_any_token_refresh_or_api_call():
    """A purely local check -- must fail before spending a network round
    trip on either the OAuth refresh or (obviously) the Gmail send call."""
    mailbox_service = FakeMailboxService()
    api_client = FakeGmailApiClient()
    sender = GmailSender(mailbox_service, api_client)

    with pytest.raises(GmailScopeMissingError):
        await sender.prepare(make_request(mailbox=make_mailbox_missing_send_scope()))

    assert mailbox_service.calls == []
    assert api_client.calls == []


async def test_scope_missing_error_also_blocks_the_combined_send_method():
    sender = GmailSender(FakeMailboxService(), FakeGmailApiClient())

    with pytest.raises(GmailScopeMissingError):
        await sender.send(make_request(mailbox=make_mailbox_missing_send_scope()))


# --- List-Unsubscribe headers threaded through prepare() (Phase C) --------------


async def test_list_unsubscribe_headers_are_threaded_from_request_into_the_prepared_mime():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)
    request = make_request(
        list_unsubscribe_header="<https://astronomic.example/u/tok-1>",
        list_unsubscribe_post_header="List-Unsubscribe=One-Click",
    )

    prepared = await sender.prepare(request)
    await sender.send_prepared(prepared)

    decoded = base64.urlsafe_b64decode(api_client.calls[0]["raw_message"].encode("ascii")).decode("utf-8")
    assert "List-Unsubscribe: <https://astronomic.example/u/tok-1>" in decoded
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in decoded


async def test_no_list_unsubscribe_headers_when_request_omits_them():
    import base64

    api_client = FakeGmailApiClient()
    sender = GmailSender(FakeMailboxService(), api_client)

    await sender.send(make_request())

    decoded = base64.urlsafe_b64decode(api_client.calls[0]["raw_message"].encode("ascii")).decode("utf-8")
    assert "List-Unsubscribe" not in decoded
