"""
Regression coverage for the 2026-08-20 production incident: every real
Astro AI chat request was failing with a 502 because Anthropic rejected
`temperature` for claude-sonnet-5 (400 invalid_request_error --
"`temperature` is deprecated for this model"), confirmed directly against
the real API outside this app. generate_chat_reply() no longer sends
`temperature` at all. These tests intercept the actual HTTP call
ClaudeClient makes (via a monkeypatched httpx.AsyncClient.post) to assert
the literal JSON body sent, so a future change can't silently reintroduce
the parameter.

generate_json_with_usage (Campaign Builder, claude-sonnet-4-5) is a
separate code path with its own inline payload -- confirmed still sending
`temperature`, proving this fix didn't leak into the other Claude
workflow/model.
"""

import httpx
import pytest

import app.claude.client as client_module
from app.claude.client import ClaudeClient


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        # Valid JSON text so this same fake works for both
        # generate_chat_reply (plain text) and generate_json_with_usage
        # (which parses the text as JSON).
        return {
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


@pytest.fixture
def captured_request(monkeypatch):
    captured = {}

    async def fake_post(self, url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(client_module.settings, "anthropic_api_key", "test-key")
    return captured


@pytest.mark.asyncio
async def test_astro_chat_request_for_claude_sonnet_5_omits_temperature(captured_request):
    client = ClaudeClient(model="claude-sonnet-5")
    await client.generate_chat_reply(
        "system prompt", [{"role": "user", "content": "What is a family office investor?"}], max_tokens=1024
    )

    assert "temperature" not in captured_request["json"]
    assert captured_request["json"]["model"] == "claude-sonnet-5"
    assert captured_request["json"]["max_tokens"] == 1024
    assert captured_request["json"]["system"] == "system prompt"


@pytest.mark.asyncio
async def test_tools_omitted_from_request_when_not_passed(captured_request):
    client = ClaudeClient(model="claude-sonnet-5")
    await client.generate_chat_reply("system prompt", [{"role": "user", "content": "hi"}], max_tokens=100)

    assert "tools" not in captured_request["json"]


@pytest.mark.asyncio
async def test_tools_included_verbatim_when_passed(captured_request):
    from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS

    client = ClaudeClient(model="claude-sonnet-5")
    await client.generate_chat_reply(
        "system prompt", [{"role": "user", "content": "hi"}], max_tokens=100, tools=CRM_TOOL_DEFINITIONS
    )

    assert captured_request["json"]["tools"] == CRM_TOOL_DEFINITIONS


@pytest.mark.asyncio
async def test_generate_chat_reply_returns_stop_reason_and_content_blocks(captured_request):
    client = ClaudeClient(model="claude-sonnet-5")
    text, usage, stop_reason, content = await client.generate_chat_reply(
        "system prompt", [{"role": "user", "content": "hi"}], max_tokens=100
    )

    assert stop_reason == "end_turn"
    assert content == [{"type": "text", "text": '{"ok": true}'}]


@pytest.mark.asyncio
async def test_campaign_builder_request_still_sends_temperature(captured_request):
    """generate_json_with_usage is Campaign Builder's separate code path
    (claude-sonnet-4-5) -- must be completely unaffected by the Astro AI
    chat fix above."""
    client = ClaudeClient(model="claude-sonnet-4-5")
    await client.generate_json_with_usage("system prompt", "user prompt")

    assert captured_request["json"]["temperature"] == 0.4
    assert captured_request["json"]["model"] == "claude-sonnet-4-5"
