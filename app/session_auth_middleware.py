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
rejected), identifying it as "service_read"/"service_operator" with
method/path/outcome -- never the token itself, never a request/response
body. See _log_service_read_decision/_log_service_operator_decision
below; tests/test_admin_service_auth.py structurally asserts no logger
call in this file can ever interpolate the token or the raw Authorization
header.

--- Admin/service OPERATOR token (Phase 2, 2026-09-03) -------------------

A SECOND service identity, alongside (never instead of) the read-only one
above -- same "Authorization header present -> commits to this auth mode,
never falls through to the cookie check" discipline, a SEPARATE secret
(ADMIN_SERVICE_OPERATOR_TOKEN, never compared against or derived from the
read token), and its OWN explicit method+path allowlist, narrower and
shaped differently from the read token's simple "/crm/* GET" prefix rule
since this identity can mutate.

Built for a trusted automation identity (not a browser, no Hub session
cookie, never the operator's own personal password, never a second human
Hub login) to prepare Astronomic Mail campaigns end-to-end without a human
manually running authenticated API calls for it -- see the approved scope
below. Deliberately does NOT attempt real multi-user auth/roles -- this
app has exactly one shared human Hub account (see AuthService's own
docstring for why that's a deliberate boundary, not a gap); this is a
second, narrowly-scoped, independently-revocable credential for automation
only, matching the read token's precedent rather than building on top of
the human login path.

Scope -- an explicit ALLOWLIST of (method, path) rules (see
_SERVICE_OPERATOR_RULES below), covering exactly:
  - Mail campaigns: create, read, edit (PATCH -- already restricted by
    MailCampaignService.update_campaign()'s own field allowlist, which
    silently drops `status` among other keys, so PATCH itself can never be
    used to activate/pause/resume/archive a campaign), sequence steps
    (create/edit/delete/reorder), schedule (read/replace), channel
    selection (read/replace -- selecting an ALREADY-connected mailbox by
    id only; never anything under /mailboxes/{id}/... OAuth), Mark Ready,
    Unlock (READY -> DRAFT), Activate (DRAFT/READY-lifecycle preparation
    only -- see the "Activate is a separate safety gate" note below),
    Pause (ACTIVE -> PAUSED -- the safer INVERSE of Activate, approved
    alongside it for the same reason: it can only ever stop new claims on
    an already-ACTIVE campaign, see the "Pause is Activate's safe inverse"
    note below), Add Prospects (POST .../prospects, Stage 3, 2026-09-03,
    CRM-List source only -- growing an already-persistent ACTIVE/PAUSED/
    legacy-COMPLETED campaign's audience; see
    MailCampaignService.add_prospects()'s own docstring for the full
    idempotent-freeze/reconciliation contract this route triggers, and
    _reconcile_batch()'s docstring for exactly when/how a legacy COMPLETED
    campaign may reopen to ACTIVE as a side effect -- never a bare
    reopen), and the review/enrollments/channels/schedule/steps/
    workload/batches reads needed to verify that state. CSV Upload as an
    Add Prospects source is Stage 4, not yet implemented -- this route
    only accepts source=crm_list for now (enforced by the request body's
    own Pydantic Literal type, see app/api/mail.py's
    MailAddProspectsRequest).
  - Mailboxes: the bare GET /mailboxes list ONLY (to pick a mailbox id for
    channel selection) -- never mailbox OAuth connect/disconnect.
  - CRM contact lists: create/edit a list and add/remove its membership --
    the audience mechanism a campaign's `source_list_id` points at (see
    MailCampaignService.mark_ready()) -- never whole-list deletion (not
    requested), never any CRM CONTACT record write (PATCH/DELETE
    /crm/contacts/*), never custom-field or Luma-mapping writes, never
    /crm/backup* or /crm/import* (those stay exclusive to a human session
    or the read-only token's own read-only exclusion list).
Explicitly, deliberately EXCLUDED, full stop, regardless of any future
addition to the allow-rules above without a fresh explicit review:
  - POST .../resume, /archive (every campaign lifecycle transition other
    than Ready/Unlock/Activate/Pause -- see the "not yet" note below)
  - everything under /mail/suppressions* and /mail/execution/*
  - everything under /mailboxes/* except the bare GET list
  - /crm/contacts/* writes, /crm/custom-fields*, /crm/backup*, /crm/import*
  - any auth/session/admin-configuration surface, and the
    MAIL_SENDING_ENGINE_ENABLED/allowlist Railway variables (no HTTP route
    controls these at all today, and none should be added to this
    identity's scope if one ever exists).

Activate is a SEPARATE safety gate from actual provider sending (Phase 2
addition, 2026-09-03, approved specifically for this reason): this route
can only ever flip a MailCampaign's `status` field from READY to ACTIVE
(re-running the exact same readiness validation mark_ready() used -- see
MailCampaignService.activate_campaign()'s own docstring) -- it has zero
ability to touch `mail_sending_engine_enabled`, the mailbox/recipient
send allowlists, or dispatch any real Gmail/SMTP call. Those remain
independent, Railway-environment-variable-only safety boundaries that
this token has no path to at all, with or without this grant.

Pause is Activate's safe inverse (Phase 2 addition, 2026-09-03, approved
alongside the discovery that ACTIVE/PAUSED campaigns cannot yet have
their schedule edited -- see MailCampaignService.pause_campaign()'s own
docstring): it stops new claims on an ACTIVE campaign without touching a
single MailEnrollment/MailEnrollmentStep row, and -- unlike Resume -- has
no way to make sending happen; if anything, granting Pause makes the
operator identity STRICTLY safer to hold, since it can now also stop an
activation it or a human made, without needing session access to do so.
Resume/archive remain excluded for now -- add them only after their own
explicit approval, the same way Activate and Pause each were.

Deliberately NOT over-engineered for Phase 2's upcoming persistent-
campaign/Batch 2+ work -- this allowlist covers exactly today's
one-source-list/Mark Ready model. When a dedicated Add Prospects/batch
endpoint exists, ITS route gets added here explicitly; this token grants
nothing preemptively.

Attribution: campaign/list-mutation activity-log events already emitted
by the relevant MailCampaignService/CrmService methods now accept an
`actor` argument (ActivityEvent.actor, previously always None -- see that
field's own docstring: "exists purely so a real identity can be attached
later"). The API routes reachable by this token pass
`actor="claude_operator"` when `request.state.identity == "service_operator"`,
and `actor=None` (byte-identical to before this feature) for every other
caller (an ordinary Hub session, or any route this token cannot reach in
the first place) -- see each route handler in app/api/mail.py/crm.py.
Nothing here rewrites or backfills any historical ActivityEvent row, and
no new read-only event type is introduced -- only the existing mutation
events gain an actor label.
"""

import hmac
import re

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


# One path segment -- deliberately permissive about the id's own shape
# (this app's real ids are UUIDs, but nothing here should depend on that
# happening to remain true), matching the read-scope check's own
# "structure, not id format" philosophy above. Never matches across a "/",
# so it can't accidentally swallow a deeper path segment.
_ID_SEGMENT = r"[^/]+"

# The full, explicit operator allowlist -- see the module docstring's
# "Scope" section for the reasoning behind every line and every
# deliberate omission. A (method, compiled fullmatch pattern) pair; a
# request is in scope only if BOTH match some entry here. This is a pure
# allowlist: anything not listed is denied by omission, with no separate
# exclusion list needed (unlike the read scope's backup/import carve-out,
# which exists only because that scope is otherwise a broad prefix).
_SERVICE_OPERATOR_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        # Mail campaigns -- create/read/edit, Mark Ready, Unlock, Activate,
        # Pause. NOT resume/archive (no rule below matches those paths at
        # all) -- see the module docstring's "Activate is a SEPARATE
        # safety gate" / "Pause is Activate's safe inverse" notes for why
        # exactly these two, and not resume/archive, were approved.
        ("GET", r"^/mail/campaigns$"),
        ("POST", r"^/mail/campaigns$"),
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}$"),
        ("PATCH", rf"^/mail/campaigns/{_ID_SEGMENT}$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/ready$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/unlock$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/activate$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/pause$"),
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/review$"),
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/enrollments$"),
        # Workload / prospect batches (Phase 2, 2026-09-03).
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/workload$"),
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/batches$"),
        # Add Prospects (Stage 3, 2026-09-03, CRM-List source only) -- see
        # the module docstring's own note on this being pre-approved,
        # narrow, operator-eligible-at-launch scope, same posture as
        # Activate/Pause/live-schedule/live-channel edits.
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/prospects$"),
        # Channels -- selecting an already-connected mailbox by id only.
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/channels$"),
        ("PUT", rf"^/mail/campaigns/{_ID_SEGMENT}/channels$"),
        # Schedule.
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/schedule$"),
        ("PUT", rf"^/mail/campaigns/{_ID_SEGMENT}/schedule$"),
        # Sequence steps.
        ("GET", rf"^/mail/campaigns/{_ID_SEGMENT}/steps$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/steps$"),
        ("PATCH", rf"^/mail/campaigns/{_ID_SEGMENT}/steps/{_ID_SEGMENT}$"),
        ("DELETE", rf"^/mail/campaigns/{_ID_SEGMENT}/steps/{_ID_SEGMENT}$"),
        ("POST", rf"^/mail/campaigns/{_ID_SEGMENT}/steps/reorder$"),
        # Mailboxes -- the bare list only, to pick an id for Channels.
        # Deliberately excludes every /mailboxes/{id}/... OAuth path (a
        # different, longer path shape this rule's exact "$" anchor
        # cannot match) and /mailboxes/google/* (also a different path).
        ("GET", r"^/mailboxes$"),
        # CRM contact lists -- audience/list membership management only.
        # No rule for GET (already reachable via the separate read-only
        # token's broader /crm/* scope) and no rule for DELETE
        # /crm/lists/{id} (whole-list deletion -- not requested).
        ("POST", r"^/crm/lists$"),
        ("PATCH", rf"^/crm/lists/{_ID_SEGMENT}$"),
        ("POST", rf"^/crm/lists/{_ID_SEGMENT}/contacts/bulk-add$"),
        ("POST", rf"^/crm/lists/{_ID_SEGMENT}/contacts/bulk-remove$"),
        ("DELETE", rf"^/crm/lists/{_ID_SEGMENT}/contacts/{_ID_SEGMENT}$"),
    )
)


def _log_service_operator_decision(request: Request, outcome: str) -> None:
    """Same non-leaking contract as _log_service_read_decision above."""
    logger.info(f"service_operator {outcome}: {request.method} {request.url.path}")


def _log_unrecognized_service_token(request: Request, outcome: str) -> None:
    """Used only when a presented token matches NEITHER configured
    identity -- deliberately not attributed to either "service_read" or
    "service_operator" since which one (if either) was actually intended
    is unknown. Same non-leaking contract as the two functions above."""
    logger.info(f"service_token {outcome}: {request.method} {request.url.path}")


def _is_in_service_operator_scope(request: Request) -> bool:
    path = request.url.path
    return any(request.method == method and pattern.fullmatch(path) for method, pattern in _SERVICE_OPERATOR_RULES)


async def _enforce_service_token(request: Request, authorization: str) -> JSONResponse | None:
    """Dispatches to whichever service identity's secret the presented
    token actually matches -- checked in a fixed order (read, then
    operator; irrelevant in practice since the two secrets are
    independently random and can never collide). Returns a JSONResponse
    to short-circuit the request, or None to allow it through -- the ONLY
    two outcomes; there is no path back to the cookie check from here
    (see module docstring). 503 only when NEITHER identity has a token
    configured at all (an operator/deployment gap); 401 for a malformed
    header or a token that matches no configured identity; 403 for a
    valid, recognized token used outside ITS OWN scope."""
    read_token = settings.admin_service_read_token
    operator_token = settings.admin_service_operator_token
    if not read_token and not operator_token:
        return JSONResponse(status_code=503, content={"detail": "Service authentication is not configured."})
    if not authorization.startswith("Bearer "):
        _log_unrecognized_service_token(request, "rejected (malformed header)")
        return JSONResponse(status_code=401, content={"detail": "Invalid Authorization header."})
    token = authorization.removeprefix("Bearer ").strip()

    if read_token and hmac.compare_digest(token, read_token):
        if not _is_in_service_read_scope(request):
            _log_service_read_decision(request, "rejected (out of scope)")
            return JSONResponse(status_code=403, content={"detail": "Service read token is not permitted for this request."})
        request.state.identity = "service_read"
        _log_service_read_decision(request, "allowed")
        return None

    if operator_token and hmac.compare_digest(token, operator_token):
        if not _is_in_service_operator_scope(request):
            _log_service_operator_decision(request, "rejected (out of scope)")
            return JSONResponse(
                status_code=403, content={"detail": "Service operator token is not permitted for this request."}
            )
        request.state.identity = "service_operator"
        _log_service_operator_decision(request, "allowed")
        return None

    _log_unrecognized_service_token(request, "rejected (invalid token)")
    return JSONResponse(status_code=401, content={"detail": "Invalid token."})


async def enforce_session_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    authorization = request.headers.get("authorization")
    if authorization is not None:
        rejection = await _enforce_service_token(request, authorization)
        if rejection is not None:
            return rejection
        return await call_next(request)

    auth_service = request.app.state.auth_service
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not await auth_service.validate_session(raw_token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated."})
    return await call_next(request)
