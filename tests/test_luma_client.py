"""
LumaClient -- exercised against httpx.MockTransport (no real network
calls), so these prove request construction (auth header, URL, query
params) and error mapping without depending on Luma's actual API being
reachable.
"""

import httpx
import pytest

from app.luma.client import LumaAPIError, LumaClient, LumaNotConfiguredError

pytestmark = pytest.mark.asyncio


async def test_not_configured_raises_before_any_request():
    client = LumaClient(api_key=None)
    with pytest.raises(LumaNotConfiguredError):
        await client.list_calendar_events()


async def test_api_key_sent_as_the_documented_header(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"entries": [], "has_more": False, "next_cursor": None})

    client = LumaClient(api_key="test-key-123")
    _patch_transport(monkeypatch, handler)

    await client.list_calendar_events()

    assert captured["headers"]["x-luma-api-key"] == "test-key-123"
    assert captured["url"].startswith("https://public-api.luma.com/v1/calendars/events/list")


async def test_list_calendar_events_pagination_params(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"entries": [], "has_more": False, "next_cursor": None})

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    await client.list_calendar_events(cursor="cursor-abc", limit=25)

    assert captured["params"]["pagination_cursor"] == "cursor-abc"
    assert captured["params"]["pagination_limit"] == "25"


async def test_list_calendar_events_omits_cursor_when_none(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"entries": [], "has_more": False, "next_cursor": None})

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    await client.list_calendar_events(cursor=None)

    assert "pagination_cursor" not in captured["params"]


async def test_list_event_guests_includes_event_id(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"entries": [], "has_more": False, "next_cursor": None})

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    await client.list_event_guests("evt-123", cursor="c1", limit=10)

    assert captured["params"]["event_id"] == "evt-123"
    assert captured["params"]["pagination_cursor"] == "c1"
    assert captured["path"] == "/v1/events/guests/list"


async def test_response_data_is_returned_as_is(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entries": [{"guest": {"id": "gst-1"}}], "has_more": True, "next_cursor": "next-1"})

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    page = await client.list_event_guests("evt-1")
    assert page["entries"][0]["guest"]["id"] == "gst-1"
    assert page["has_more"] is True
    assert page["next_cursor"] == "next-1"


async def test_rate_limit_response_raises_with_status_code(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    with pytest.raises(LumaAPIError) as exc_info:
        await client.list_calendar_events()
    assert exc_info.value.status_code == 429


async def test_server_error_response_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = LumaClient(api_key="k")
    _patch_transport(monkeypatch, handler)

    with pytest.raises(LumaAPIError) as exc_info:
        await client.list_calendar_events()
    assert exc_info.value.status_code == 500


async def test_api_key_never_appears_in_a_raised_error_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = LumaClient(api_key="super-secret-key-value")
    _patch_transport(monkeypatch, handler)

    with pytest.raises(LumaAPIError) as exc_info:
        await client.list_calendar_events()
    assert "super-secret-key-value" not in str(exc_info.value)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Redirects LumaClient's internal httpx.AsyncClient construction
    through a MockTransport -- avoids any real network call while still
    exercising the actual request-building code. Uses monkeypatch (not a
    manual reassignment) so it's automatically undone after each test,
    never leaking into other test files."""
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.luma.client.httpx.AsyncClient", patched)
