"""
GmailThreadReaderClient -- exercised against httpx.MockTransport (no real
network calls), matching tests/test_gmail_api_client.py's established
pattern. Read-only diagnostic client (2026-09-15) -- see that module's
own docstring for why it exists.
"""

import httpx
import pytest

from app.google.gmail_thread_reader_client import (
    GMAIL_MESSAGES_GET_URL,
    GMAIL_THREADS_GET_URL,
    METADATA_HEADERS,
    GmailReadAuthError,
    GmailReadConnectionError,
    GmailReadMalformedResponseError,
    GmailReadNotFoundError,
    GmailReadProviderError,
    GmailReadRateLimitedError,
    GmailThreadReaderClient,
    extract_headers,
)

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.google.gmail_thread_reader_client.httpx.AsyncClient", patched)


def _message_shape(message_id: str, thread_id: str, headers: dict[str, str]) -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]},
    }


# --- Request construction ----------------------------------------------------


async def test_get_thread_bearer_token_url_and_metadata_params(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "thr-1", "messages": []})

    _patch_transport(monkeypatch, handler)
    await GmailThreadReaderClient().get_thread(access_token="tok-abc", thread_id="thr-1")

    assert captured["headers"]["authorization"] == "Bearer tok-abc"
    assert captured["url"].startswith(GMAIL_THREADS_GET_URL.format(thread_id="thr-1"))
    assert "format=metadata" in captured["url"]
    for h in METADATA_HEADERS:
        assert f"metadataHeaders={h}" in captured["url"]


async def test_get_message_bearer_token_and_url(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "msg-1", "threadId": "thr-1", "payload": {"headers": []}})

    _patch_transport(monkeypatch, handler)
    await GmailThreadReaderClient().get_message(access_token="tok-xyz", message_id="msg-1")

    assert captured["headers"]["authorization"] == "Bearer tok-xyz"
    assert captured["url"].startswith(GMAIL_MESSAGES_GET_URL.format(message_id="msg-1"))
    assert "format=metadata" in captured["url"]


# --- Success parsing -----------------------------------------------------------


async def test_get_thread_returns_full_parsed_response_with_messages(monkeypatch):
    thread_body = {
        "id": "thr-1",
        "historyId": "12345",
        "messages": [
            _message_shape("msg-1", "thr-1", {"Subject": "Hello", "Message-ID": "<a@x.com>"}),
            _message_shape("msg-2", "thr-1", {"Subject": "Hello", "Message-ID": "<b@x.com>", "In-Reply-To": "<a@x.com>"}),
        ],
    }
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=thread_body))

    data = await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")

    assert data["id"] == "thr-1"
    assert len(data["messages"]) == 2
    assert [m["id"] for m in data["messages"]] == ["msg-1", "msg-2"]


async def test_get_message_returns_full_parsed_response(monkeypatch):
    message_body = _message_shape("msg-2", "thr-1", {"Subject": "Hello", "In-Reply-To": "<a@x.com>"})
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=message_body))

    data = await GmailThreadReaderClient().get_message(access_token="tok", message_id="msg-2")

    assert data["id"] == "msg-2"
    assert data["threadId"] == "thr-1"


# --- extract_headers() ---------------------------------------------------------


def test_extract_headers_pulls_a_flat_dict():
    message = _message_shape("msg-1", "thr-1", {"Subject": "Hi", "From": "a@x.com", "Message-ID": "<a@x.com>"})
    assert extract_headers(message) == {"Subject": "Hi", "From": "a@x.com", "Message-ID": "<a@x.com>"}


def test_extract_headers_missing_header_is_simply_absent():
    message = _message_shape("msg-1", "thr-1", {"Subject": "Hi"})
    headers = extract_headers(message)
    assert "In-Reply-To" not in headers
    assert headers.get("In-Reply-To") is None


def test_extract_headers_ignores_anything_outside_the_metadata_allowlist():
    message = {
        "id": "msg-1", "threadId": "thr-1",
        "payload": {"headers": [{"name": "Subject", "value": "Hi"}, {"name": "X-Mailer", "value": "SomeClient"}]},
    }
    assert extract_headers(message) == {"Subject": "Hi"}


# --- Error taxonomy -------------------------------------------------------------


async def test_401_raises_auth_error(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}}))
    with pytest.raises(GmailReadAuthError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


async def test_403_raises_auth_error(monkeypatch):
    """403 on a read is folded into the SAME auth-error class as 401 --
    for THIS client's diagnostic purpose, both mean "the request wasn't
    honored," and distinguishing bad-token from missing-scope isn't
    needed here the way it is for the send path's mailbox-health logic."""
    _patch_transport(monkeypatch, lambda r: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}}))
    with pytest.raises(GmailReadAuthError):
        await GmailThreadReaderClient().get_message(access_token="tok", message_id="msg-1")


async def test_404_raises_not_found(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}))
    with pytest.raises(GmailReadNotFoundError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="does-not-exist")


async def test_429_raises_rate_limited(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(GmailReadRateLimitedError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_raises_provider_error(monkeypatch, status):
    _patch_transport(monkeypatch, lambda r: httpx.Response(status, json={"error": {"status": "INTERNAL"}}))
    with pytest.raises(GmailReadProviderError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


async def test_200_with_invalid_json_raises_malformed_response(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


async def test_200_missing_messages_raises_malformed_response(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"id": "thr-1"}))
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


async def test_200_missing_thread_id_on_message_raises_malformed_response(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json={"id": "msg-1"}))
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailThreadReaderClient().get_message(access_token="tok", message_id="msg-1")


async def test_connection_error_raises_connection_error(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("simulated")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailReadConnectionError):
        await GmailThreadReaderClient().get_thread(access_token="tok", thread_id="thr-1")


# --- Never logs sensitive values -------------------------------------------------


def test_module_source_never_logs_the_access_token():
    """Same discipline as test_gmail_api_client.py's own logging-safety
    checks -- greps this module's own source for a logger call that could
    interpolate the access token."""
    import inspect

    import app.google.gmail_thread_reader_client as module

    source = inspect.getsource(module)
    for line in source.splitlines():
        if "logger." in line:
            assert "access_token" not in line
