"""
Thin wrapper around the Anthropic Messages API. Originally built for
structured JSON generation (Campaign Builder's plan generation, prospect
ranking); Astro AI chat (see app/services/astro_ai_service.py) extends this
SAME client with a plain-text, multi-turn chat method rather than
duplicating a second Anthropic HTTP client -- one place owns the API key,
the base URL, and the wire format.
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

    Also raised by generate_chat_reply() (Astro AI) for the identical reason
    -- one config, one error type, for every Claude call site in this app.
    """


class ClaudeAuthenticationError(RuntimeError):
    """Anthropic rejected the configured API key (HTTP 401). Distinct from
    ClaudeNotConfiguredError: a key IS set, but Anthropic says it's invalid
    -- an operator/deployment problem, not a transient outage."""


class ClaudeRateLimitError(RuntimeError):
    """Anthropic returned HTTP 429. Never retried automatically here --
    retrying a rate limit immediately just makes it worse; the caller
    should surface this to the user rather than hide it behind a retry."""


class ClaudeTimeoutError(RuntimeError):
    """The request exceeded its timeout before Anthropic responded."""


class ClaudeProviderError(RuntimeError):
    """Any other non-2xx response or network-level failure from Anthropic
    (5xx outage, malformed response, etc.) -- never carries the raw
    response body in its message, so a caller can safely log/surface this
    without risking a secret or provider-internal detail leaking."""


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

    async def generate_chat_reply(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        timeout: float = 30.0,
    ) -> tuple[str, dict]:
        """
        Astro AI chat (see app/services/astro_ai_service.py) -- plain
        conversational text, not JSON, and a `messages` list (multi-turn)
        rather than a single user_prompt. No automatic retry (unlike
        generate_json_with_usage): a failed chat turn should surface
        cleanly to the user immediately rather than silently spend up to
        3x the tokens/latency retrying, and retrying a 429 immediately
        would only make it worse.

        Deliberately never sends `temperature`: confirmed directly against
        Anthropic (2026-08-20 production investigation) that claude-sonnet-5
        rejects it with a 400 invalid_request_error ("`temperature` is
        deprecated for this model"), which is exactly what was causing
        every real Astro AI chat request to fail. generate_json_with_usage
        below is a separate code path (Campaign Builder, claude-sonnet-4-5)
        with its own `temperature` -- unaffected by this.

        Raises one of ClaudeNotConfiguredError / ClaudeAuthenticationError /
        ClaudeRateLimitError / ClaudeTimeoutError / ClaudeProviderError --
        never a bare httpx exception -- so every caller has a small, closed
        set of cases to handle and none of them carry Anthropic's raw
        response body (which could contain the prompt or other detail
        callers shouldn't surface verbatim).
        """
        if not self.api_key:
            raise ClaudeNotConfiguredError(
                "Claude is not configured (ANTHROPIC_API_KEY is unset) -- Astro AI is unavailable "
                "until it's set. This does not affect the CRM, ITF intake, or any other part of "
                "the application."
            )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
                        "system": system_prompt,
                        "messages": messages,
                    },
                )
        except httpx.TimeoutException as e:
            raise ClaudeTimeoutError("Claude did not respond in time.") from e
        except httpx.HTTPError as e:
            raise ClaudeProviderError(f"Network error calling Claude: {type(e).__name__}") from e

        if resp.status_code == 401:
            logger.error("Claude authentication failed for Astro AI chat -- check ANTHROPIC_API_KEY.")
            raise ClaudeAuthenticationError("Claude rejected the configured API key.")
        if resp.status_code == 429:
            raise ClaudeRateLimitError("Claude rate limit exceeded.")
        if resp.status_code >= 400:
            logger.error(f"Claude chat request failed with status {resp.status_code}.")
            raise ClaudeProviderError(f"Claude returned status {resp.status_code}.")

        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        return text, data.get("usage", {})

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
