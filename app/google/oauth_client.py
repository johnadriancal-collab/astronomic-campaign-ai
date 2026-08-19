"""
Minimal, hand-rolled Google OAuth 2.0 Authorization Code client -- Astronomic
Mail Phase 2 (Google Workspace Mailbox Connection). Deliberately NOT using
google-auth/google-auth-oauthlib: this app's existing convention (see
app/apollo/client.py, app/claude/client.py) is a small httpx-based client
per integration rather than a heavy SDK, and Email Intake's Google Sheets
client was explicitly removed in favor of exactly this kind of minimal
surface when that feature pivoted to a webhook-token bridge instead -- this
follows the same philosophy.

CONNECTION ONLY. Scopes requested: `openid email profile` ONLY -- Google's
standard "Sign in with Google" scope set, classified as non-sensitive/non-
restricted. No `gmail.*` scope of any kind is requested, constructed, or
referenced anywhere in this file. A future sending phase would need
`https://www.googleapis.com/auth/gmail.send` (a Google "Sensitive" scope,
subject to Google's OAuth verification process) and would need every
already-connected mailbox to consent again; nothing here does that.

Nothing in this file logs a raw authorization code, access token, refresh
token, or Authorization header -- only HTTP status codes on failure.
"""

import secrets
from urllib.parse import urlencode

import httpx
from loguru import logger

from app.config import settings

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SCOPES = ("openid", "email", "profile")


class GoogleOAuthNotConfiguredError(Exception):
    """GOOGLE_OAUTH_CLIENT_ID/CLIENT_SECRET/REDIRECT_URI aren't all set --
    callers should surface this as a 503, never an unhandled 500."""


class GoogleTokenExchangeError(Exception):
    """The authorization code -> token exchange failed. Never carries the
    raw code, client secret, or response body in its message."""


class GoogleUserinfoError(Exception):
    """The userinfo fetch failed, or returned no usable email."""


def generate_state() -> str:
    """A fresh, unguessable, single-use CSRF token -- see
    MailboxService's in-memory pending-state store for how this is
    validated on callback."""
    return secrets.token_urlsafe(32)


def _require_configured() -> tuple[str, str, str]:
    client_id = settings.google_oauth_client_id
    client_secret = settings.google_oauth_client_secret
    redirect_uri = settings.google_oauth_redirect_uri
    if not client_id or not client_secret or not redirect_uri:
        raise GoogleOAuthNotConfiguredError(
            "Google OAuth is not configured (GOOGLE_OAUTH_CLIENT_ID/CLIENT_SECRET/REDIRECT_URI)."
        )
    return client_id, client_secret, redirect_uri


class GoogleOAuthClient:
    """Real network calls to Google. Tests must substitute a fake
    implementing this same interface (see tests/test_mailbox_service.py) --
    never exercise this against the real Google endpoints in a test."""

    def build_authorize_url(self, state: str) -> str:
        client_id, _client_secret, redirect_uri = _require_configured()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "access_type": "offline",  # required to receive a refresh token
            "prompt": "consent",  # required so a returning user is re-issued a refresh token too
            "include_granted_scopes": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        """Returns Google's raw token response dict (access_token,
        refresh_token, expires_in, id_token, ...). Never logs `code` or the
        response body -- only the outcome."""
        client_id, client_secret, redirect_uri = _require_configured()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if resp.status_code != 200:
            logger.warning(f"Google token exchange failed with status {resp.status_code}.")
            raise GoogleTokenExchangeError("Google token exchange failed.")
        return resp.json()

    async def fetch_userinfo(self, access_token: str) -> dict:
        """Returns {"sub", "email", "name", ...}. Never logs the access token."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code != 200:
            logger.warning(f"Google userinfo fetch failed with status {resp.status_code}.")
            raise GoogleUserinfoError("Google userinfo fetch failed.")
        return resp.json()

    async def revoke_token(self, token: str) -> bool:
        """Best-effort -- returns whether Google confirmed revocation.
        Never logs the token itself, only the outcome. Callers must proceed
        with deleting our own stored credential regardless of this result."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(REVOKE_URL, data={"token": token})
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"Google token revocation returned status {resp.status_code}.")
        return ok
