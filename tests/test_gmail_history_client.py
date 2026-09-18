"""
GmailHistoryClient -- exercised against httpx.MockTransport (no real
network calls), matching tests/test_gmail_thread_reader_client.py's
established pattern.
"""

import httpx
import pytest

from app.google.gmail_history_client import (
    GMAIL_HISTORY_LIST_URL,
    GMAIL_PROFILE_GET_URL,
    GmailHistoryClient,
    GmailHistoryExpiredError,
)
from app.google.gmail_thread_reader_client import (
    GmailReadAuthError,
    GmailReadConnectionError,
    GmailReadMalformedResponseError,
    GmailReadNotFoundError,
    GmailReadProviderError,
    GmailReadRateLimitedError,
)

pytestmark = pytest.mark.asyncio


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.google.gmail_history_client.httpx.AsyncClient", patched)


# --- get_current_history_id --------------------------------------------------


async def test_get_current_history_id_bearer_token_and_url(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"emailAddress": "a@b.com", "historyId": "12345"})

    _patch_transport(monkeypatch, handler)
    result = await GmailHistoryClient().get_current_history_id(access_token="tok-abc")

    assert captured["headers"]["authorization"] == "Bearer tok-abc"
    assert captured["url"] == GMAIL_PROFILE_GET_URL
    assert result == "12345"


async def test_get_current_history_id_malformed_response_missing_history_id(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"emailAddress": "a@b.com"}))
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailHistoryClient().get_current_history_id(access_token="tok-abc")


async def test_get_current_history_id_connection_error(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("boom")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailReadConnectionError):
        await GmailHistoryClient().get_current_history_id(access_token="tok-abc")


# --- list_history --------------------------------------------------------------


async def test_list_history_request_params(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"history": [], "historyId": "2000"})

    _patch_transport(monkeypatch, handler)
    await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")

    assert "startHistoryId=1000" in captured["url"]
    assert "historyTypes=messageAdded" in captured["url"]
    assert "labelId=INBOX" in captured["url"]
    assert captured["url"].startswith(GMAIL_HISTORY_LIST_URL)


async def test_list_history_extracts_added_message_ids(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "history": [
                    {"id": "h1", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                    {"id": "h2", "messagesAdded": [{"message": {"id": "m2", "threadId": "t2"}}]},
                ],
                "historyId": "2000",
            },
        )

    _patch_transport(monkeypatch, handler)
    result = await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")

    assert result == {"message_ids": ["m1", "m2"], "history_id": "2000"}


async def test_list_history_deduplicates_repeated_message_ids(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "history": [
                    {"id": "h1", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                    {"id": "h2", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                ],
                "historyId": "2000",
            },
        )

    _patch_transport(monkeypatch, handler)
    result = await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")
    assert result["message_ids"] == ["m1"]


async def test_list_history_walks_pagination(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page_token = httpx.QueryParams(request.url.query.decode()).get("pageToken")
        calls.append(page_token)
        if page_token is None:
            return httpx.Response(
                200,
                json={
                    "history": [{"id": "h1", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
                    "nextPageToken": "page2",
                },
            )
        return httpx.Response(
            200,
            json={
                "history": [{"id": "h2", "messagesAdded": [{"message": {"id": "m2", "threadId": "t2"}}]}],
                "historyId": "2000",
            },
        )

    _patch_transport(monkeypatch, handler)
    result = await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")

    assert calls == [None, "page2"]
    assert result == {"message_ids": ["m1", "m2"], "history_id": "2000"}


async def test_list_history_no_changes_keeps_start_history_id(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"history": []}))
    result = await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")
    assert result == {"message_ids": [], "history_id": "1000"}


async def test_list_history_404_raises_expired_not_generic_not_found(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={"error": {"message": "not found"}}))
    with pytest.raises(GmailHistoryExpiredError):
        await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="stale-id")


async def test_list_history_401_raises_auth_error(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(401, json={"error": {"message": "unauthorized"}}))
    with pytest.raises(GmailReadAuthError):
        await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")


async def test_list_history_429_raises_rate_limited(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(429, json={"error": {"message": "rate limited"}}))
    with pytest.raises(GmailReadRateLimitedError):
        await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")


async def test_list_history_500_raises_provider_error(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(500, json={"error": {"message": "server error"}}))
    with pytest.raises(GmailReadProviderError):
        await GmailHistoryClient().list_history(access_token="tok-abc", start_history_id="1000")


# --- get_message_metadata_light ----------------------------------------------


async def test_get_message_metadata_light_requests_content_type_header(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "m1", "payload": {"headers": []}})

    _patch_transport(monkeypatch, handler)
    await GmailHistoryClient().get_message_metadata_light(access_token="tok-abc", message_id="m1")

    assert "format=metadata" in captured["url"]
    assert "metadataHeaders=Content-Type" in captured["url"]
    assert "metadataHeaders=From" in captured["url"]
    assert "metadataHeaders=Subject" in captured["url"]


async def test_get_message_metadata_light_404_raises_ordinary_not_found_not_expired(monkeypatch):
    """A 404 on messages.get is a genuinely missing message -- distinct
    from history.list's own 404 meaning "stale cursor" -- must raise the
    ordinary GmailReadNotFoundError, never GmailHistoryExpiredError."""
    _patch_transport(monkeypatch, lambda req: httpx.Response(404, json={"error": {"message": "not found"}}))
    with pytest.raises(GmailReadNotFoundError):
        await GmailHistoryClient().get_message_metadata_light(access_token="tok-abc", message_id="m1")
