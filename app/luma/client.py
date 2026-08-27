"""
Thin HTTP client for the Luma (lu.ma) events API -- read-only surface, only
the two endpoints the backfill needs: listing events on our one configured
calendar, and listing guests for one event. Single-calendar-key scope only
(no organization-key/multi-calendar abstraction) -- see this module's
callers for why that's the deliberate, approved scope for Phase 1.

CONFIRMED against docs.luma.com (fetched live, not assumed from training
data): auth header is `x-luma-api-key`, base URL is
`https://public-api.luma.com`, list endpoints are cursor-paginated
(`pagination_cursor` in, `{entries, has_more, next_cursor}` out).

The API key is read from settings/constructor only -- never logged, never
returned by any method here (errors carry a status code and Luma's error
`type`, never headers or the request body that would contain the key).
"""

import httpx
from loguru import logger

from app.config import settings

LUMA_API_BASE_URL = "https://public-api.luma.com"


class LumaNotConfiguredError(RuntimeError):
    """Raised instead of attempting the HTTP call when no Luma API key is
    configured -- mirrors ClaudeNotConfiguredError's precedent (app/claude/client.py)
    so the backfill route can return a clear 503 rather than a confusing
    network-looking failure."""


class LumaAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code  # None for network-level failures


class LumaClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.luma_api_key
        self.base_url = base_url or LUMA_API_BASE_URL

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.api_key:
            raise LumaNotConfiguredError(
                "Luma is not configured (LUMA_API_KEY is unset) -- the Luma sync is unavailable "
                "until it's set. This does not affect the CRM, ITF intake, or any other part of "
                "the application."
            )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers={"x-luma-api-key": self.api_key},
                    params={k: v for k, v in (params or {}).items() if v is not None},
                )
        except httpx.HTTPError as e:
            raise LumaAPIError(f"Network error calling Luma: {type(e).__name__}") from e

        if resp.status_code == 429:
            raise LumaAPIError("Luma rate limit exceeded.", status_code=429)
        if resp.status_code >= 400:
            logger.error(f"Luma API error on GET {path}: {resp.status_code}")
            raise LumaAPIError(f"Luma returned status {resp.status_code}.", status_code=resp.status_code)
        return resp.json()

    async def list_calendar_events(
        self, cursor: str | None = None, limit: int = 50, status: str = "approved"
    ) -> dict:
        """One page of events on the configured calendar. Response shape:
        {"entries": [{"event": {...}, "tags": [...]}], "has_more": bool,
        "next_cursor": str | None}."""
        return await self._get(
            "/v1/calendars/events/list",
            params={"pagination_cursor": cursor, "pagination_limit": limit, "status": status},
        )

    async def list_event_guests(self, event_id: str, cursor: str | None = None, limit: int = 50) -> dict:
        """One page of guests for one event. Response shape:
        {"entries": [{"guest": {...}}], "has_more": bool, "next_cursor": str | None}."""
        return await self._get(
            "/v1/events/guests/list",
            params={"event_id": event_id, "pagination_cursor": cursor, "pagination_limit": limit},
        )
