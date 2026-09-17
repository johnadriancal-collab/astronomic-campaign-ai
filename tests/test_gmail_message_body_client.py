"""
GmailMessageBodyClient + extract_best_body -- exercised against
httpx.MockTransport (no real network calls), matching
tests/test_gmail_thread_reader_client.py's established pattern.
Inbox V2 (2026-09-17) reply-body reading.
"""

import base64

import httpx
import pytest

from app.google.gmail_message_body_client import (
    GMAIL_MESSAGES_GET_FULL_URL,
    GmailMessageBodyClient,
    extract_best_body,
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

    monkeypatch.setattr("app.google.gmail_message_body_client.httpx.AsyncClient", patched)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


# --- Request construction ----------------------------------------------------


async def test_get_message_full_bearer_token_url_and_format_param(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "msg-1", "threadId": "thr-1", "payload": {}})

    _patch_transport(monkeypatch, handler)
    await GmailMessageBodyClient().get_message_full(access_token="tok-abc", message_id="msg-1")

    assert captured["headers"]["authorization"] == "Bearer tok-abc"
    assert captured["url"].startswith(GMAIL_MESSAGES_GET_FULL_URL.format(message_id="msg-1"))
    assert "format=full" in captured["url"]


# --- Error classification -----------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [(401, GmailReadAuthError), (403, GmailReadAuthError), (404, GmailReadNotFoundError), (429, GmailReadRateLimitedError), (500, GmailReadProviderError)],
)
async def test_error_status_codes_classified(monkeypatch, status, expected):
    _patch_transport(monkeypatch, lambda request: httpx.Response(status, json={"error": "simulated"}))
    with pytest.raises(expected):
        await GmailMessageBodyClient().get_message_full(access_token="tok", message_id="msg-1")


async def test_malformed_json_raises(monkeypatch):
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailMessageBodyClient().get_message_full(access_token="tok", message_id="msg-1")


async def test_missing_id_or_thread_id_raises_malformed(monkeypatch):
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={"id": "msg-1"}))  # no threadId
    with pytest.raises(GmailReadMalformedResponseError):
        await GmailMessageBodyClient().get_message_full(access_token="tok", message_id="msg-1")


async def test_connection_failure_raises(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("simulated")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(GmailReadConnectionError):
        await GmailMessageBodyClient().get_message_full(access_token="tok", message_id="msg-1")


# --- extract_best_body ---------------------------------------------------------


def test_plain_text_leaf_extracted_directly():
    message = {"payload": {"mimeType": "text/plain", "body": {"data": _b64("Got it, thanks!")}}}
    result = extract_best_body(message)
    assert result == ("Got it, thanks!", "plain")


def test_html_only_reply_renders_safely_as_converted_text():
    html = "<div>Hi <b>there</b>,<br>Got it &amp; thanks!<br></div>"
    message = {"payload": {"mimeType": "text/html", "body": {"data": _b64(html)}}}
    text, source = extract_best_body(message)
    assert source == "html_converted"
    assert "<" not in text and ">" not in text  # never raw markup
    assert "Hi there" in text
    assert "Got it & thanks!" in text


def test_multipart_alternative_prefers_plain_text_over_html():
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Plain version")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>HTML version</p>")}},
            ],
        }
    }
    text, source = extract_best_body(message)
    assert source == "plain"
    assert text == "Plain version"


def test_multipart_mixed_with_nested_alternative_and_attachment():
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("Reply body text")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<p>Reply body text</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "att-1", "size": 12345},
                },
            ],
        }
    }
    text, source = extract_best_body(message)
    assert source == "plain"
    assert text == "Reply body text"
    # The attachment's own bytes are never touched/decoded -- no "data" key on it.


def test_empty_or_attachment_only_message_returns_none():
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [{"mimeType": "application/pdf", "filename": "invoice.pdf", "body": {"attachmentId": "att-1"}}],
        }
    }
    assert extract_best_body(message) is None


def test_never_exposes_raw_base64_or_mime_structure_as_text():
    raw_b64 = _b64("Reply body text")
    message = {"payload": {"mimeType": "text/plain", "body": {"data": raw_b64}}}
    text, _source = extract_best_body(message)
    assert text == "Reply body text"
    assert raw_b64 not in text
