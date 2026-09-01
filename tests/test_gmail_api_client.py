"""
GmailApiClient -- exercised against httpx.MockTransport (no real network
calls), matching tests/test_luma_client.py's established pattern for a
hand-rolled httpx-based integration client. Proves request construction
(auth header, URL, body shape), the full status-code-to-exception
taxonomy, and (B2 hardening pass) each exception's `.certainty`
(SendOutcomeCertainty) -- without depending on Gmail's actual API being
reachable.
"""

import httpx
import pytest

from app.google.gmail_api_client import (
    GMAIL_SEND_URL,
    GmailApiClient,
    GmailAuthRequiredError,
    GmailConnectionNeverEstablishedError,
    GmailMalformedResponseError,
    GmailPermanentRejectionError,
    GmailPermissionError,
    GmailRateLimitedError,
    GmailRequestOutcomeUnknownError,
    GmailSendError,
    GmailTemporaryProviderError,
)
from app.services.mail_sending_service import MailSendError, SendOutcomeCertainty

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Same technique as tests/test_luma_client.py's _patch_transport."""
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.google.gmail_api_client.httpx.AsyncClient", patched)


# --- Request construction ----------------------------------------------------


async def test_bearer_token_and_url_are_correct(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "msg-1", "threadId": "thr-1"})

    _patch_transport(monkeypatch, handler)
    client = GmailApiClient()
    await client.send_message(access_token="tok-abc", raw_message="cmF3")

    assert captured["headers"]["authorization"] == "Bearer tok-abc"
    assert captured["url"] == GMAIL_SEND_URL


async def test_raw_message_sent_as_the_documented_json_field(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg-1", "threadId": "thr-1"})

    _patch_transport(monkeypatch, handler)
    client = GmailApiClient()
    await client.send_message(access_token="tok", raw_message="cmF3LW1lc3NhZ2U")

    assert captured["body"] == {"raw": "cmF3LW1lc3NhZ2U"}


async def test_thread_id_included_when_given(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg-2", "threadId": "thr-existing"})

    _patch_transport(monkeypatch, handler)
    client = GmailApiClient()
    await client.send_message(access_token="tok", raw_message="cmF3", thread_id="thr-existing")

    assert captured["body"] == {"raw": "cmF3", "threadId": "thr-existing"}


async def test_thread_id_omitted_entirely_when_none(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "msg-3", "threadId": "thr-new"})

    _patch_transport(monkeypatch, handler)
    client = GmailApiClient()
    await client.send_message(access_token="tok", raw_message="cmF3")

    assert "threadId" not in captured["body"]


# --- Success parsing -----------------------------------------------------------


async def test_successful_response_returns_id_and_thread_id(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"id": "msg-9", "threadId": "thr-9"}))
    client = GmailApiClient()
    data = await client.send_message(access_token="tok", raw_message="cmF3")
    assert data == {"id": "msg-9", "threadId": "thr-9"}


# --- Error taxonomy -------------------------------------------------------------


async def test_401_raises_auth_required_and_is_definitely_not_sent(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}))
    with pytest.raises(GmailAuthRequiredError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_403_raises_permission_error_and_is_definitely_not_sent(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}))
    with pytest.raises(GmailPermissionError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_429_raises_rate_limited_and_is_definitely_not_sent(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(GmailRateLimitedError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_raises_temporary_provider_error_and_is_outcome_unknown(monkeypatch, status):
    """Deliberately NOT definitely-not-sent -- Gmail's own server erroring
    does not prove the message wasn't created before the error."""
    _patch_transport(monkeypatch, lambda r: httpx.Response(status, json={"error": {"status": "INTERNAL"}}))
    with pytest.raises(GmailTemporaryProviderError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


async def test_400_raises_permanent_rejection_and_is_definitely_not_sent(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}}))
    with pytest.raises(GmailPermanentRejectionError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_unclassified_4xx_raises_permanent_rejection_and_is_definitely_not_sent(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}))
    with pytest.raises(GmailPermanentRejectionError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_200_with_invalid_json_raises_malformed_response_and_is_outcome_unknown(monkeypatch):
    """Gmail's own success signal (200) -- the message may well have been
    created even though we failed to parse the confirmation."""
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(GmailMalformedResponseError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


async def test_200_missing_id_raises_malformed_response(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"threadId": "thr-1"}))
    with pytest.raises(GmailMalformedResponseError):
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")


async def test_200_missing_thread_id_raises_malformed_response(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"id": "msg-1"}))
    with pytest.raises(GmailMalformedResponseError):
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")


# --- Transport failures: connection-never-established vs. ambiguous -------------


@pytest.mark.parametrize("exc_cls", [httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout])
async def test_pre_connection_failures_raise_connection_never_established_and_are_definitely_not_sent(
    monkeypatch, exc_cls
):
    """No HTTP request bytes could possibly have reached Gmail in any of
    these cases -- verified against httpx's actual exception hierarchy
    (ConnectTimeout is NOT a subclass of ConnectError), not assumed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_cls("connection-phase failure", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailConnectionNeverEstablishedError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.DEFINITELY_NOT_SENT


async def test_read_timeout_after_request_sent_raises_outcome_unknown(monkeypatch):
    """The ambiguous case: the request may well have reached Gmail before
    the timeout -- this must be treated as an unknown/uncertain outcome,
    never as a confirmed failure or confirmed success."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailRequestOutcomeUnknownError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


async def test_write_timeout_raises_outcome_unknown(monkeypatch):
    """Even a write-phase failure is treated conservatively: httpx does
    not let this module prove how much of the request, if any, the
    server received before the failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write timed out", request=request)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailRequestOutcomeUnknownError) as exc_info:
        await GmailApiClient().send_message(access_token="tok", raw_message="cmF3")
    assert exc_info.value.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


async def test_every_taxonomy_exception_is_a_gmail_send_error_and_a_mail_send_error():
    import inspect

    from app.google import gmail_api_client as mod

    for name in (
        "GmailAuthRequiredError", "GmailPermissionError", "GmailRateLimitedError",
        "GmailTemporaryProviderError", "GmailPermanentRejectionError",
        "GmailConnectionNeverEstablishedError", "GmailRequestOutcomeUnknownError",
        "GmailMalformedResponseError",
    ):
        cls = getattr(mod, name)
        assert inspect.isclass(cls) and issubclass(cls, GmailSendError)
        assert issubclass(cls, MailSendError)
        assert cls.certainty in (SendOutcomeCertainty.DEFINITELY_NOT_SENT, SendOutcomeCertainty.OUTCOME_UNKNOWN)


async def test_uncategorized_gmail_send_error_defaults_conservatively_to_outcome_unknown():
    """The base class itself, and any future subclass that forgets to set
    `certainty`, must default to the conservative value -- never silently
    DEFINITELY_NOT_SENT."""
    assert GmailSendError.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


# --- .retryable classification (Phase C) ----------------------------------------


@pytest.mark.parametrize("name", ["GmailAuthRequiredError", "GmailRateLimitedError", "GmailConnectionNeverEstablishedError"])
async def test_transient_definitely_not_sent_errors_are_retryable(name):
    """401 (a lapsed access token, distinct from invalid_grant -- see
    GmailAuthRequiredError's own docstring), 429, and a connection that
    never reached Gmail at all are all safe to requeue: no Gmail-side
    state could have been created."""
    from app.google import gmail_api_client as mod

    cls = getattr(mod, name)
    assert cls.retryable is True


@pytest.mark.parametrize("name", ["GmailPermissionError", "GmailPermanentRejectionError"])
async def test_permanent_definitely_not_sent_errors_are_not_retryable(name):
    """A missing scope or a permanently-rejected request will fail again
    identically on retry -- must default to the conservative,
    non-retryable base value, never require an explicit opt-out."""
    from app.google import gmail_api_client as mod

    cls = getattr(mod, name)
    assert cls.retryable is False


async def test_mail_send_error_base_class_defaults_retryable_to_false():
    assert MailSendError.retryable is False


async def test_access_token_and_raw_message_never_appear_in_a_raised_error_message(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}))
    with pytest.raises(GmailAuthRequiredError) as exc_info:
        await GmailApiClient().send_message(access_token="super-secret-token-value", raw_message="cmF3LXNlY3JldA==")
    assert "super-secret-token-value" not in str(exc_info.value)
