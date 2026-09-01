"""
Minimal, hand-rolled Google OAuth 2.0 Authorization Code client -- Astronomic
Mail Phase 2 (Google Workspace Mailbox Connection) + Phase B1 (Gmail scope
upgrade + token refresh foundation). Deliberately NOT using google-auth/
google-auth-oauthlib: this app's existing convention (see app/apollo/
client.py, app/claude/client.py) is a small httpx-based client per
integration rather than a heavy SDK, and Email Intake's Google Sheets
client was explicitly removed in favor of exactly this kind of minimal
surface when that feature pivoted to a webhook-token bridge instead -- this
follows the same philosophy.

CONNECTION ONLY, STILL -- this file gains the ABILITY to request
`gmail.send` and to refresh an access token, but nothing here (or anywhere
else in app/) ever calls Gmail's messages.send or constructs a MIME
message. `SCOPES` (the base "sign in" set) is unchanged; `GMAIL_SEND_SCOPE`
is a separate constant a caller must explicitly opt into by passing it to
build_authorize_url()'s new `scopes` parameter -- the base connect flow
(MailboxService.begin_google_oauth()) still passes only `SCOPES`.

Nothing in this file logs a raw authorization code, access token, refresh
token, client secret, or Authorization header -- only HTTP status codes and
Google's own (non-secret) `error`/`error_description` codes on failure.
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

# Phase B1: the ONE Gmail scope this codebase is authorized to ever request
# (see the approved Phase B architecture report -- gmail.send is a Google
# "Sensitive" scope, the narrowest of the four that support
# users.messages.send, deliberately NOT gmail.compose/gmail.modify/
# mail.google.com). Requesting it is still entirely MailboxService's
# decision (via begin_gmail_send_upgrade()) -- this constant existing here
# grants nothing by itself.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GoogleOAuthNotConfiguredError(Exception):
    """GOOGLE_OAUTH_CLIENT_ID/CLIENT_SECRET/REDIRECT_URI aren't all set --
    callers should surface this as a 503, never an unhandled 500."""


class GoogleTokenExchangeError(Exception):
    """The authorization code -> token exchange failed. Never carries the
    raw code, client secret, or response body in its message."""


class GoogleUserinfoError(Exception):
    """The userinfo fetch failed, or returned no usable email."""


class GoogleTokenRefreshError(Exception):
    """Base class for refresh_access_token() failures -- an ORDINARY
    provider/network failure (non-200 response that isn't a confirmed
    invalid_grant, a connection error, a timeout). Never carries the raw
    refresh token, access token, or response body. Deliberately NOT proof
    the stored refresh token itself is bad -- callers (see
    MailboxService.refresh_mailbox_access_token()) must never treat this
    as a reason to move a mailbox to NEEDS_REAUTH."""


class GoogleRefreshTokenInvalidError(GoogleTokenRefreshError):
    """Google's token endpoint returned `error=invalid_grant` for a
    grant_type=refresh_token request -- Google's own canonical signal that
    the stored refresh token is no longer usable (revoked by the user via
    myaccount.google.com, expired from long inactivity, or the app's OAuth
    consent was pulled/the app was unpublished). This is the ONE condition
    anywhere in this codebase that should ever drive a mailbox from
    CONNECTED to NEEDS_REAUTH -- see MailboxService.
    refresh_mailbox_access_token()."""


class GoogleTokenRefreshMalformedResponseError(GoogleTokenRefreshError):
    """Google responded HTTP 200 but the body wasn't valid JSON, or was
    missing `access_token` -- a protocol-level problem, distinct from both
    an ordinary provider failure and a confirmed invalid_grant. Never
    treated as proof the refresh token is bad."""


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

    def build_authorize_url(self, state: str, scopes: tuple[str, ...] = SCOPES) -> str:
        """`scopes` defaults to the base `SCOPES` (unchanged behavior for
        the ordinary connect flow) -- a caller requesting an upgrade (e.g.
        MailboxService.begin_gmail_send_upgrade()) passes the full desired
        set explicitly (base scopes + GMAIL_SEND_SCOPE), rather than
        relying solely on `include_granted_scopes=true` to merge a
        smaller incremental request -- that flag stays set as a safety
        net (Google's own documented incremental-authorization mechanism:
        it re-presents already-granted scopes so the resulting token's
        `scope` field reflects the full, current grant either way), but
        the explicit-full-set request is the primary, unambiguous path."""
        client_id, _client_secret, redirect_uri = _require_configured()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
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

    async def refresh_access_token(self, refresh_token: str) -> dict:
        """Exchanges a stored refresh token for a fresh access token via
        `grant_type=refresh_token`. Returns Google's raw response dict
        (`access_token`, `expires_in`, `scope`, `token_type` -- Google does
        NOT normally issue a new refresh_token from this grant type, so
        the stored one is never replaced by this call). Never logs
        `refresh_token`, the returned `access_token`, or the raw response
        body -- only HTTP status codes and Google's own `error` code
        string (never secret) on failure.

        Raises GoogleRefreshTokenInvalidError specifically and only for
        `error=invalid_grant` -- the one signal callers may treat as "this
        mailbox genuinely needs reauthorization." Raises the base
        GoogleTokenRefreshError for any other non-200 response (network
        failure, 5xx, a DIFFERENT error code) -- these must never be
        treated as proof the grant itself is broken. Raises
        GoogleTokenRefreshMalformedResponseError for a 200 response that
        isn't valid JSON or lacks `access_token`."""
        client_id, client_secret, _redirect_uri = _require_configured()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            )
        if resp.status_code != 200:
            try:
                error_code = resp.json().get("error")
            except ValueError:
                error_code = None
            if error_code == "invalid_grant":
                logger.warning("Google refresh token grant is invalid (invalid_grant) -- reauthorization required.")
                raise GoogleRefreshTokenInvalidError("Google reports the stored refresh token is no longer valid.")
            logger.warning(f"Google token refresh failed with status {resp.status_code} (error={error_code!r}).")
            raise GoogleTokenRefreshError("Google token refresh failed.")
        try:
            data = resp.json()
        except ValueError as e:
            raise GoogleTokenRefreshMalformedResponseError("Google's refresh response was not valid JSON.") from e
        if not data.get("access_token"):
            raise GoogleTokenRefreshMalformedResponseError("Google's refresh response had no access_token.")
        return data

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
