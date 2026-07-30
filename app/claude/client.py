"""
Thin wrapper around the Anthropic Messages API for structured JSON generation.
"""

import json

import httpx
from loguru import logger

from app.config import settings

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


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
        """
        Calls Claude with a system + user prompt and parses the response as
        JSON. Retries on transient HTTP errors or malformed JSON.
        """
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
                    return self._parse_json(text)
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
