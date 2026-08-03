"""
Shared HTTP plumbing for all Apollo API calls.

CONFIRMED (via docs.apollo.io/docs/test-api-key):
  - Auth header is `x-api-key`, base URL is `https://api.apollo.io/api/v1`.

STILL VERIFY BEFORE PRODUCTION USE:
  - Key scoping (confirmed via docs.apollo.io/docs/create-api-key): Apollo
    keys are scoped by default — you pick which endpoints a key can call,
    and calling an unselected endpoint returns 403. A small number of
    endpoints (e.g. listing users) require a master key. When creating the
    key for this app, explicitly select the contacts/lists/sequences
    endpoints it needs (or use a master key if one of them turns out to
    require it).
  - Sequence endpoint naming — Apollo has referred to these as both
    "sequences" and "emailer_campaigns" in different API versions/docs;
    this code uses "emailer_campaigns", confirm against current docs.
"""

import httpx
from loguru import logger

from app.config import settings


class ApolloAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code  # None for network errors / retry-exhausted cases


class ApolloBaseClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.apollo_api_key
        self.base_url = base_url or settings.apollo_base_url

    async def request(self, method: str, path: str, max_retries: int = 3, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers.setdefault("x-api-key", self.api_key)
        headers.setdefault("Content-Type", "application/json")

        last_error: ApolloAPIError | None = None

        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, max_retries + 1):
                try:
                    resp = await client.request(method, url, headers=headers, **kwargs)

                    if resp.status_code == 429:
                        logger.warning(f"Apollo rate limited on {path}, attempt {attempt}")
                        last_error = ApolloAPIError(f"Rate limited: {resp.text}", status_code=429)
                        continue

                    resp.raise_for_status()
                    return resp.json() if resp.content else {}

                except httpx.HTTPStatusError as e:
                    logger.error(
                        f"Apollo API error on {method} {path}: "
                        f"{e.response.status_code} {e.response.text}"
                    )
                    last_error = ApolloAPIError(str(e), status_code=e.response.status_code)
                    # Client errors (bad request, auth, not found) won't
                    # resolve themselves on retry — fail fast.
                    if e.response.status_code < 500:
                        raise last_error

                except httpx.HTTPError as e:
                    logger.warning(f"Apollo network error on {path}, attempt {attempt}: {e}")
                    last_error = ApolloAPIError(str(e))

        raise last_error or ApolloAPIError(f"Request to {path} failed after {max_retries} attempts")
