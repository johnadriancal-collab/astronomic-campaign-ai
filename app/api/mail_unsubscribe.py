"""
Astronomic Mail Phase B3 -- the PUBLIC, unauthenticated unsubscribe
surface. Deliberately its own module/router, not folded into app/api/mail.py's
"Suppression" section: everything in mail.py is session-gated (see
app/session_auth_middleware.py); everything here is reachable by an
anonymous recipient with nothing but a token, which is a different enough
security posture to want its own file, matching this codebase's existing
precedent of splitting public/webhook surfaces into their own modules
(app/api/luma.py vs. the rest of app/api/).

NO SENDING CAPABILITY: this module never imports anything from
app/google/, never constructs a MailSenderPort, and is never touched by
app/services/mail_unsubscribe_composition.py (the reusable
footer/header-composition pieces) or vice versa -- this module only ever
CONSUMES a token that composition module's future caller would have
embedded in an outbound email; it never sends one. See
tests/test_mail_unsubscribe_safety.py.

Three routes, three different jobs:
  - GET /mail/unsubscribe: read-only confirmation page. Structurally
    incapable of mutating suppression state -- this handler has no
    MailSuppressionService dependency at all, so there is nothing here
    TO call even by mistake. A repeated GET (e.g. an email-security
    scanner prefetching the link) is exactly as harmless as the first.
  - POST /mail/unsubscribe: the human path, reached only by submitting
    the GET page's own form. Actually suppresses.
  - POST /mail/unsubscribe/one-click: the RFC 8058 target for mail
    clients' List-Unsubscribe-Post. Actually suppresses, immediately, no
    confirmation, no redirect (RFC 8058: "MUST NOT return an HTTPS
    redirect"), and reads nothing from the request except the token
    (RFC 8058: "MUST NOT include cookies, HTTP authorization, or any
    other context information" -- trivially true here since this
    handler never reads request.cookies/request.headers at all).

Both new paths added to app/session_auth_middleware.py's PUBLIC_PATHS as
exact strings (that middleware matches request.url.path only, never the
query string -- see this module's own tests for why the token lives in
the query string, never a path segment).

SECURITY: every failure mode -- missing token, malformed token, wrong
key, tampered ciphertext, right-shaped-but-wrong-purpose payload -- is
collapsed into ONE generic "this link is invalid or has expired" outcome
(see app/services/unsubscribe_token.py's UnsubscribeTokenInvalidError
docstring for why). This route family never accepts a raw email address
as input at all -- only an opaque, pre-issued token -- so there is no
way to use it to probe whether any particular address exists in the
suppression system; it cannot become an enumeration oracle by
construction, not merely by discipline. No token value, decrypted
payload, or constructed URL is ever logged.

PRIVACY (B3 final pass): neither HTML page displays the decrypted email
or any other token-payload field -- copy is deliberately generic
("Confirm unsubscribe" / "You will no longer receive Astronomic Mail
emails."). The token still identifies the recipient INTERNALLY (decoded
server-side on POST to know who to suppress) -- there is simply no
product reason for the browser to ever render it. The GET page's form
carries the token forward into the POST via the SAME query-string value
already present in the URL the recipient's email client gave them
(`action="/mail/unsubscribe?token=<the original opaque string>"`) --
never re-derived, never decrypted-then-re-rendered, never any other
token-payload field. The only token-shaped thing that ever reaches or
leaves the browser is that one already-opaque value.
"""

from html import escape as _escape

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.dependencies import get_mail_suppression_service
from app.models.mail import MailSuppressionReason
from app.services.mail_suppression_service import MailSuppressionService
from app.services.unsubscribe_token import (
    UnsubscribeTokenInvalidError,
    UnsubscribeTokenNotConfiguredError,
    decode_unsubscribe_token,
)

router = APIRouter(prefix="/mail", tags=["mail-unsubscribe"])

_NO_STORE_HEADERS = {"Cache-Control": "no-store"}

_ERROR_PAGE_HTML = (
    "<!doctype html><html><head><title>Unsubscribe</title></head><body>"
    "<p>This unsubscribe link is invalid or has expired.</p>"
    "</body></html>"
)


def _decode_token(token: str | None) -> str:
    """Shared decode step for all three routes. Raises
    UnsubscribeTokenNotConfiguredError (a real config problem -- callers
    should let this surface as a 503) or UnsubscribeTokenInvalidError
    (the generic public-facing outcome -- see that class's own
    docstring) for a missing/malformed/tampered/wrong-key token. Missing
    entirely is treated identically to malformed, never distinguished."""
    if not token:
        raise UnsubscribeTokenInvalidError("Missing token.")
    return decode_unsubscribe_token(token)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirm_page(token: str | None = Query(default=None)):
    """Read-only. Renders a confirmation page for a VALID token, or a
    generic error page for anything else -- never touches the
    suppression store, never accepts a MailSuppressionService dependency
    (see this module's docstring).

    Decodes the token only to VALIDATE it (so a bad/expired link shows
    the error page) -- the decoded email is deliberately discarded
    immediately after, never rendered. See this module's PRIVACY note:
    the browser doesn't need to see who the token is for."""
    try:
        _decode_token(token)  # validity check only -- see docstring above
    except UnsubscribeTokenNotConfiguredError:
        raise HTTPException(status_code=503, detail="Unsubscribe is not configured.")
    except UnsubscribeTokenInvalidError:
        return HTMLResponse(_ERROR_PAGE_HTML, status_code=400, headers=_NO_STORE_HEADERS)

    safe_token = _escape(token or "")
    return HTMLResponse(
        "<!doctype html><html><head><title>Confirm unsubscribe</title></head><body>"
        "<p>Confirm unsubscribe</p>"
        "<p>You will no longer receive Astronomic Mail emails.</p>"
        f'<form method="POST" action="/mail/unsubscribe?token={safe_token}">'
        '<button type="submit">Confirm</button>'
        "</form>"
        "</body></html>",
        headers=_NO_STORE_HEADERS,
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirm_submit(
    token: str | None = Query(default=None),
    service: MailSuppressionService = Depends(get_mail_suppression_service),
):
    """The human path -- reached only by submitting the GET page's own
    form. Idempotent (MailSuppressionService.suppress() already is --
    see that method's own docstring): a second submit of the same token,
    or a token for an already-unsubscribed address, succeeds identically."""
    try:
        email = _decode_token(token)
    except UnsubscribeTokenNotConfiguredError:
        raise HTTPException(status_code=503, detail="Unsubscribe is not configured.")
    except UnsubscribeTokenInvalidError:
        return HTMLResponse(_ERROR_PAGE_HTML, status_code=400, headers=_NO_STORE_HEADERS)

    await service.suppress(email, MailSuppressionReason.UNSUBSCRIBED)
    return HTMLResponse(
        "<!doctype html><html><head><title>Unsubscribed</title></head><body>"
        "<p>You've been unsubscribed.</p>"
        "<p>You will no longer receive Astronomic Mail emails.</p>"
        "</body></html>",
        headers=_NO_STORE_HEADERS,
    )


@router.post("/unsubscribe/one-click")
async def unsubscribe_one_click(
    token: str | None = Query(default=None),
    service: MailSuppressionService = Depends(get_mail_suppression_service),
):
    """RFC 8058 one-click target -- the ONLY thing List-Unsubscribe-Post
    points at. Immediate, no confirmation, NEVER a redirect (RFC 8058
    explicitly forbids one: "redirected POST actions have historically
    not worked reliably"), and this handler reads nothing from the
    request except `token` -- no cookies, no auth header -- per RFC
    8058's "MUST NOT include... any other context information."
    Idempotent, same as the human path. Response is deliberately NOT
    HTML -- this is consumed by mail-provider infrastructure, never
    rendered to a human."""
    try:
        email = _decode_token(token)
    except UnsubscribeTokenNotConfiguredError:
        raise HTTPException(status_code=503, detail="not_configured")
    except UnsubscribeTokenInvalidError:
        raise HTTPException(status_code=400, detail="invalid_token")

    await service.suppress(email, MailSuppressionReason.UNSUBSCRIBED)
    return PlainTextResponse("OK", headers=_NO_STORE_HEADERS)
