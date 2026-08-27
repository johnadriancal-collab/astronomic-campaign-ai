"""
Centralized session-authentication gate for the ENTIRE application --
deny by default. This is the REAL security boundary (frontend route
protection, see frontend/middleware.ts, is a UX convenience only -- it
cannot stop a direct API request, which is exactly what this guards).

Only the exact paths in PUBLIC_PATHS are reachable without a valid Hub
session cookie:
  - /health: required public by Railway's own health checks.
  - /auth/login, /auth/logout, /auth/session: the login flow itself
    obviously can't require you to already be logged in.
  - /mailboxes/google/callback: Google's OAuth redirect lands here
    directly (never through the frontend's /backend/* rewrite proxy),
    on a different origin than wherever the Hub session cookie is
    scoped to -- the browser physically cannot attach that cookie to
    this request regardless of what this middleware does. This route's
    real guard is its own single-use CSRF `state` check (see
    MailboxService._consume_state()).
  - /sync/itf-contact, /sync/email-intake: existing webhook endpoints
    called by a Google Apps Script (no browser, no session cookie
    possible) -- each already has its OWN shared-secret bearer-token
    check (verify_itf_webhook_token / verify_email_intake_webhook_token
    in app/dependencies.py). This is a second, independent auth
    mechanism for server-to-server calls, not a bypass of this one.
  - /sync/luma-event: Luma's own webhook delivery (no browser, no session
    cookie possible) -- authenticated by Luma's own signed-webhook
    mechanism (verify_luma_webhook_request in app/dependencies.py), not a
    token we issue. /sync/luma-backfill is deliberately NOT here -- it's
    an internal admin action and stays behind the normal session gate.

Everything else -- every CRM/campaign/lead/mailbox-data route, /docs,
/redoc, /openapi.json, and even this backend's own "/" status page --
requires a valid session.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.services.auth_service import SESSION_COOKIE_NAME

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/auth/login",
        "/auth/logout",
        "/auth/session",
        "/mailboxes/google/callback",
        "/sync/itf-contact",
        "/sync/email-intake",
        "/sync/luma-event",
    }
)


async def enforce_session_auth(request: Request, call_next):
    if request.url.path not in PUBLIC_PATHS:
        auth_service = request.app.state.auth_service
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if not await auth_service.validate_session(raw_token):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated."})
    return await call_next(request)
