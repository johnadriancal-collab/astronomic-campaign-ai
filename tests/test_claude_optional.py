"""
Phase 0 (2026-08-12): ANTHROPIC_API_KEY is now optional at the Settings
level, so the app can boot and the CRM/ITF intake/everything-else can
operate without it. This file covers exactly that boundary:

  - ClaudeClient raises ClaudeNotConfiguredError immediately (no HTTP call,
    no retries) when no api_key is present -- app/claude/client.py.
  - /campaign/preview and /campaign/search map that error to a clear 503,
    distinguishable from a genuine Claude-side failure (502).
  - Settings() itself can be constructed with anthropic_api_key unset.
  - apollo_api_key is UNCHANGED -- still required -- confirming Apollo
    availability was never coupled to Claude's optionality (the user's
    explicit "don't disable Apollo just because Claude is unavailable").

Route-level tests build a fresh FastAPI app with just the campaign router,
same pattern as test_campaign_lifecycle_endpoints.py -- isolated from the
real SQLite file and from other tests' store state.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.claude.client as client_module
from app.agents.campaign_agent import CampaignAgent
from app.api.campaign import router as campaign_router
from app.claude.client import ClaudeClient, ClaudeNotConfiguredError
from app.config import Settings
from app.dependencies import get_campaign_service
from app.models.campaign import Campaign, CampaignPlan, CampaignStatus, Filters
from app.repositories.campaign_lead_store import MemoryCampaignLeadStore
from app.repositories.campaign_store import MemoryCampaignStore
from app.repositories.lead_store import MemoryLeadStore
from app.services.campaign_service import CampaignService
from app.services.lead_service import LeadService
from app.services.prospect_ranker import ProspectRanker


# --- ClaudeClient: fails fast, never attempts the HTTP call ---
#
# ClaudeClient.__init__ falls back to the real settings.anthropic_api_key
# whenever the constructor's own api_key arg is falsy (`api_key or
# settings.anthropic_api_key`) -- exactly the behavior that lets a real
# caller omit the arg and pick up the configured key. That means simply
# passing api_key=None here is NOT sufficient to simulate "unconfigured" in
# this dev environment, where a real ANTHROPIC_API_KEY is actually set in
# .env -- it would silently fall through to that real key and these tests
# would make real (or, worse, flakily-real) network calls. Monkeypatching
# settings.anthropic_api_key directly is what actually forces the
# "unconfigured" condition regardless of ambient environment.


@pytest.mark.asyncio
async def test_claude_client_with_no_api_key_raises_before_any_http_call(monkeypatch):
    monkeypatch.setattr(client_module.settings, "anthropic_api_key", None)
    client = ClaudeClient(model="claude-sonnet-4-5")
    assert client.api_key is None
    with pytest.raises(ClaudeNotConfiguredError, match="ANTHROPIC_API_KEY"):
        await client.generate_json("system prompt", "user prompt")


@pytest.mark.asyncio
async def test_claude_client_with_empty_string_api_key_also_raises(monkeypatch):
    """Falsy-but-not-None (e.g. an accidentally-blank env var) is treated the same way."""
    monkeypatch.setattr(client_module.settings, "anthropic_api_key", None)
    client = ClaudeClient(api_key="", model="claude-sonnet-4-5")
    with pytest.raises(ClaudeNotConfiguredError):
        await client.generate_json_with_usage("system prompt", "user prompt")


@pytest.mark.asyncio
async def test_generate_chat_reply_with_no_api_key_raises_before_any_http_call(monkeypatch):
    """Astro AI chat (generate_chat_reply) shares the exact same fail-fast
    guarantee as generate_json above -- one config, one boundary, for
    every Claude call site in this app."""
    monkeypatch.setattr(client_module.settings, "anthropic_api_key", None)
    client = ClaudeClient(model="claude-sonnet-5")
    with pytest.raises(ClaudeNotConfiguredError, match="ANTHROPIC_API_KEY"):
        await client.generate_chat_reply("system prompt", [{"role": "user", "content": "hi"}], max_tokens=100)


def test_claude_client_construction_never_raises_regardless_of_key():
    """Constructing a ClaudeClient (and therefore CampaignAgent()/ProspectRanker(), both
    constructed eagerly at app startup in main.py) must never fail just because no key is
    configured -- only an actual generate_json call should surface ClaudeNotConfiguredError."""
    ClaudeClient(api_key=None)
    CampaignAgent(claude_client=ClaudeClient(api_key=None))
    ProspectRanker(claude_client=ClaudeClient(api_key=None))


# --- Settings: anthropic_api_key optional, apollo_api_key still required ---


def test_settings_construct_fine_without_anthropic_api_key(monkeypatch):
    # Explicitly strip both real env vars first -- this dev environment's actual .env/
    # shell may well have both set, and pydantic-settings' env-var source would otherwise
    # win over an omitted field, making this assertion pass or fail based on ambient
    # state rather than the code under test.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    settings = Settings(_env_file=None, apollo_api_key="real-apollo-key")
    assert settings.anthropic_api_key is None


def test_settings_still_requires_apollo_api_key(monkeypatch):
    """Confirms Apollo's own requiredness is UNCHANGED by this fix -- Apollo must remain
    available independently, never coupled to Claude's optionality."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- Route level: /campaign/preview, /campaign/search -> 503 when Claude unconfigured ---


def make_campaign_service(claude_configured: bool) -> tuple[CampaignService, MemoryCampaignStore, AsyncMock]:
    agent_client = ClaudeClient(api_key="real-key" if claude_configured else "placeholder")
    ranker_client = ClaudeClient(api_key="real-key" if claude_configured else "placeholder")
    if not claude_configured:
        # Set directly rather than passing api_key=None to the constructor -- the
        # constructor falls back to the real settings.anthropic_api_key whenever its
        # own arg is falsy, which would silently pick up whatever real key this dev
        # environment actually has configured instead of simulating "unconfigured."
        agent_client.api_key = None
        ranker_client.api_key = None
    agent = CampaignAgent(claude_client=agent_client)
    ranker = ProspectRanker(claude_client=ranker_client)

    campaign_store = MemoryCampaignStore()
    lead_store = MemoryLeadStore()
    campaign_lead_store = MemoryCampaignLeadStore()
    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)

    fake_apollo = AsyncMock()
    fake_apollo.search_people.return_value = {"total_entries": 0, "people": []}

    service = CampaignService(
        agent=agent,
        apollo=fake_apollo,
        ranker=ranker,
        store=campaign_store,
        lead_service=lead_service,
        campaign_lead_store=campaign_lead_store,
    )
    return service, campaign_store, fake_apollo


@pytest.fixture
def unconfigured_client():
    service, campaign_store, fake_apollo = make_campaign_service(claude_configured=False)
    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: service
    with TestClient(app) as client:
        yield client, campaign_store


def test_preview_returns_503_when_claude_not_configured(unconfigured_client):
    client, _ = unconfigured_client
    resp = client.post("/campaign/preview", json={"prompt": "Find investors in Austin"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_search_returns_503_when_claude_not_configured(unconfigured_client):
    client, campaign_store = unconfigured_client
    # A DRAFT campaign already exists (as if preview() had succeeded earlier while a key
    # WAS configured) -- confirms search()'s own Claude call (ranking) is independently
    # guarded, not just preview()'s.
    campaign = Campaign(
        campaign_id="c1",
        original_prompt="Find investors in Austin",
        created_at=datetime.now(timezone.utc),
        status=CampaignStatus.DRAFT,
        plan=CampaignPlan(campaign_name="Austin Investors", filters=Filters(), sequence=[]),
    )
    await campaign_store.create(campaign)

    resp = client.post("/campaign/search", json={"campaign_id": "c1"})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_preview_succeeds_normally_when_claude_is_configured():
    """Confirms the 503 branch doesn't accidentally fire when a key IS present --
    exercises the ordinary success path with a stubbed CampaignAgent (no real
    Anthropic call), same isolation style as the other route tests in this file."""
    service, _, _ = make_campaign_service(claude_configured=True)
    service.agent.generate_campaign_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=CampaignPlan(campaign_name="Austin Investors", filters=Filters(), sequence=[])
    )

    app = FastAPI()
    app.include_router(campaign_router)
    app.dependency_overrides[get_campaign_service] = lambda: service
    with TestClient(app) as client:
        resp = client.post("/campaign/preview", json={"prompt": "Find investors in Austin"})

    assert resp.status_code == 200
    assert resp.json()["plan"]["campaign_name"] == "Austin Investors"
