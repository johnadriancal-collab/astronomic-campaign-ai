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
  - /mail/unsubscribe, /mail/unsubscribe/one-click (Phase B3): reached by
    an anonymous recipient (a human clicking a link, or a mail provider's
    infrastructure POSTing List-Unsubscribe-Post) who by definition has
    no Hub session -- authenticated instead by a self-contained, opaque,
    Fernet-encrypted token in the QUERY STRING (see
    app/services/unsubscribe_token.py). This is exactly why the token
    lives in the query string and not a path segment: this middleware
    matches request.url.path by EXACT STRING ONLY (see below) -- it has
    no mechanism for a parameterized public path, so
    "/mail/unsubscribe/{token}" could never be allow-listed here, but
    "/mail/unsubscribe?token=..." matches the fixed path fine since the
    query string isn't part of `request.url.path` at all.

Everything else -- every CRM/campaign/lead/mailbox-data route, /docs,
/redoc, /openapi.json, and even this backend's own "/" status page --
requires a valid session.

--- Admin/service read-only token (Phase 1, 2026-09-03) -----------------

A SECOND, independent authentication mode, for a trusted development/
operations tool (not a browser -- no Hub session cookie involved or
wanted) to make read-only production CRM investigations directly,
without a human copying authenticated URLs out of their own browser
session. Deliberately its own code path, not a PUBLIC_PATHS entry and
not a weakening of the cookie check above -- that check is completely
untouched by this addition.

The mode switch is the PRESENCE of an `Authorization` header, checked
BEFORE the cookie logic runs at all:
  - No `Authorization` header at all -> falls through to the existing
    cookie/session flow exactly as before this feature existed. A normal
    browser request never sends this header for Hub traffic, so this is
    a no-op for every existing request shape.
  - `Authorization` header present -> the ENTIRE request is now decided
    by the service-token check below, one way or the other. It is NEVER
    allowed to fall through to the cookie check afterward, even on
    failure -- an invalid or out-of-scope service-token request must
    never silently re-attempt (and potentially succeed) as a cookie
    request. This is deliberately more rigid than a generic "try both"
    scheme: presenting this header at all commits you to this auth mode.

Scope, enforced together, both required:
  - method must be GET, HEAD, or OPTIONS -- no service-token request can
    ever mutate anything, full stop.
  - path must start with "/crm/" -- no service-token request can reach
    Mail sending/suppression, campaign activation, mailbox/OAuth, Luma
    webhook/config, or auth/admin config, regardless of method.
A request presenting a VALID token outside this scope gets an explicit
403, not a fallback to the cookie flow and not a generic 401 (401 is
reserved for the token itself being missing/malformed/wrong).

Phase 1 is READ ONLY: only ADMIN_SERVICE_READ_TOKEN exists. There is
deliberately no write token yet -- when one is added, it must default to
an explicit contact/list-only allowlist, NOT a blanket "/crm/* write"
grant, since /crm/luma-question-mappings and /crm/custom-fields also
live under that prefix and are their own, separately-approved concern
(see app/api/luma.py's mapping_router docstring).

EXCLUDED even though nominally read-only and under /crm/ -- both checked
BEFORE the general allow rule, both independent of method (never reachable
via GET/HEAD/OPTIONS either), both leaving normal browser/session access
completely unaffected since the exclusion only ever applies to the
service-read code path:
  - /crm/backup and everything under /crm/backup/ (currently just
    /crm/backup/export) -- a full JSON snapshot of EVERY contact plus
    every custom field definition in one call.
  - /crm/import and everything under /crm/import/ (currently
    /crm/import/{import_batch_id}) -- an import batch's `rows` field is
    "raw parsed CSV rows, in file order" (app/models/crm.py's
    CrmImportBatch), a complete raw dump of whatever was uploaded, of
    unbounded size depending on the batch.
Deliberately NOT excluded: GET /crm/contacts/{id}'s `source_snapshot`
field can also hold a full raw original form submission, but it's
bounded to ONE contact at a time and is exactly what per-contact
investigation (e.g. the duplicate-contact work this feature was built
for) needs -- excluding it would defeat the point of this token.

Logging: exactly one INFO line per service-token decision (allowed or
rejected), identifying it as "service_read" with method/path/outcome --
never the token itself, never a request/response body. See
_log_service_read_decision below; tests/test_admin_service_auth.py
structurally asserts no logger call in this file can ever interpolate
the token or the raw Authorization header.
"""

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import settings
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
        "/mail/unsubscribe",
        "/mail/unsubscribe/one-click",
    }
)

_SERVICE_READ_ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SERVICE_READ_ALLOWED_PATH_PREFIX = "/crm/"

# Excluded from service-read scope regardless of method -- see module
# docstring for why /crm/backup specifically. Exact-path-or-subpath match
# only ("/crm/backup" itself, or anything under "/crm/backup/"), never a
# bare prefix, so an unrelated path that merely starts with the same
# characters (e.g. a hypothetical "/crm/backupfoo") is never mistakenly
# excluded.
_SERVICE_READ_EXCLUDED_PATHS = ("/crm/backup", "/crm/import")


def _log_service_read_decision(request: Request, outcome: str) -> None:
    """`outcome` is always one of a short, fixed set of words below --
    never anything derived from the Authorization header or token value,
    so there is no way this call can ever leak the credential itself."""
    logger.info(f"service_read {outcome}: {request.method} {request.url.path}")


def _is_excluded_from_service_read(path: str) -> bool:
    return any(path == excluded or path.startswith(f"{excluded}/") for excluded in _SERVICE_READ_EXCLUDED_PATHS)


def _is_in_service_read_scope(request: Request) -> bool:
    path = request.url.path
    if _is_excluded_from_service_read(path):
        return False
    return request.method in _SERVICE_READ_ALLOWED_METHODS and path.startswith(_SERVICE_READ_ALLOWED_PATH_PREFIX)


async def _enforce_service_read_token(request: Request, authorization: str) -> JSONResponse | None:
    """Returns a JSONResponse to short-circuit the request, or None to
    allow it through -- the ONLY two outcomes; there is no path back to
    the cookie check from here (see module docstring)."""
    if not settings.admin_service_read_token:
        return JSONResponse(status_code=503, content={"detail": "Service authentication is not configured."})
    if not authorization.startswith("Bearer "):
        _log_service_read_decision(request, "rejected (malformed header)")
        return JSONResponse(status_code=401, content={"detail": "Invalid Authorization header."})
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, settings.admin_service_read_token):
        _log_service_read_decision(request, "rejected (invalid token)")
        return JSONResponse(status_code=401, content={"detail": "Invalid token."})
    if not _is_in_service_read_scope(request):
        _log_service_read_decision(request, "rejected (out of scope)")
        return JSONResponse(status_code=403, content={"detail": "Service read token is not permitted for this request."})
    request.state.identity = "service_read"
    _log_service_read_decision(request, "allowed")
    return None


async def enforce_session_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    authorization = request.headers.get("authorization")
    if authorization is not None:
        rejection = await _enforce_service_read_token(request, authorization)
        if rejection is not None:
            return rejection
        return await call_next(request)

    auth_service = request.app.state.auth_service
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not await auth_service.validate_session(raw_token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated."})
    return await call_next(request)
