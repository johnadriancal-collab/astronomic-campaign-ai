"""
Thin wrapper around the Anthropic Messages API for structured JSON generation.
"""

import json

import httpx
from loguru import logger

from app.config import settings

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeNotConfiguredError(RuntimeError):
    """
    Raised instead of attempting the HTTP call when no Anthropic API key is
    configured (Phase 0, 2026-08-12: ANTHROPIC_API_KEY is now optional at the
    Settings level -- see app/config.py -- so the app can boot without it).
    Callers (CampaignAgent.generate_campaign_plan, ProspectRanker.rank, and
    therefore the /campaign/preview and /campaign/search routes) should let
    this propagate rather than swallow it -- app/api/campaign.py catches it
    specifically to return a clear 503 ("Claude unavailable/not configured")
    instead of the generic 502 used for an actual Anthropic-side failure.
    Deliberately raised BEFORE the retry loop below: retrying a call that is
    guaranteed to fail auth 3 times in a row wastes time and produces a
    confusing generic error instead of this precise one.
    """


class ClaudeClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.claude_model

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        max_tokens: int = 2000,
        temperature: float = 0.4,
    ) -> dict:
        """Calls Claude and returns just the parsed JSON (existing callers)."""
        parsed, _usage = await self.generate_json_with_usage(
            system_prompt, user_prompt, max_retries, max_tokens, temperature
        )
        return parsed

    async def generate_json_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        max_tokens: int = 2000,
        temperature: float = 0.4,
    ) -> tuple[dict, dict]:
        """
        Same as generate_json, but also returns Anthropic's `usage` object
        (input_tokens/output_tokens) -- needed for cost/latency reporting
        (see prospect_ranker.py). Retries on transient HTTP errors or
        malformed JSON.
        """
        if not self.api_key:
            raise ClaudeNotConfiguredError(
                "Claude is not configured (ANTHROPIC_API_KEY is unset) -- the Campaign "
                "Builder is unavailable until it's set. This does not affect the CRM, "
                "ITF intake, or any other part of the application."
            )

        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    resp = await client.post(
                        ANTHROPIC_MESSAGES_URL,
                        headers={
                            "x-api-key": self.api_key,
                            "anthropic-version": ANTHROPIC_VERSION,
                            "content-type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                            "system": system_prompt,
                            "messages": [{"role": "user", "content": user_prompt}],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    text = "".join(
                        block.get("text", "")
                        for block in data.get("content", [])
                        if block.get("type") == "text"
                    ).strip()
                    return self._parse_json(text), data.get("usage", {})
                except (httpx.HTTPError, ValueError) as e:
                    last_error = e
                    logger.warning(f"Claude generation attempt {attempt} failed: {e}")

        raise RuntimeError(
            f"Claude JSON generation failed after {max_retries} attempts: {last_error}"
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Malformed JSON from Claude (first 500 chars): {text[:500]}")
            raise ValueError(f"Malformed JSON from Claude: {e}") from e
