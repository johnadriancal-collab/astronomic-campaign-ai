"""
Astronomic Mail open tracking (2026-09-18) -- the PUBLIC, unauthenticated
pixel surface. Its own module, same reasoning as app/api/mail_unsubscribe.py's
own docstring: everything in app/api/mail.py is session-gated (see
app/session_auth_middleware.py); this route is reachable by anonymous mail
clients loading an `<img>` tag, a different enough security posture to
want its own file.

SECURITY: exactly one route, one response shape, ALWAYS -- a real, found
token and an unknown/malformed one get the byte-identical tiny GIF back
(never a 404, never a different status code, never a timing-detectable
branch beyond an in-process dict/index lookup) -- so this can never
become an oracle for which tokens are valid, matching the unsubscribe
routes' own "every failure mode collapses into one generic outcome"
posture. No token value or constructed URL is ever logged.

Added to app/session_auth_middleware.py's PUBLIC_PATHS as an exact
string (that middleware matches request.url.path only, never the query
string -- same reason the token lives in the query string here, never a
path segment).

Cache-Control: no-store -- a cached pixel response would silently
undercount real opens (a client serving its own cached copy never
re-requests this endpoint at all); this header at least ensures OUR
response never encourages that for a client that respects it. Real
mail-client image proxies that ignore this and cache/prefetch anyway are
a known, accepted source of approximation -- see MailOpenEvent's own
docstring and the Open rate UI's tooltip copy.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.dependencies import get_mail_open_tracking_service
from app.services.mail_open_tracking_service import MailOpenTrackingService

router = APIRouter(prefix="/mail", tags=["mail-open-tracking"])

# The smallest valid GIF: 1x1, transparent, no comment/extension blocks.
_TRANSPARENT_GIF_BYTES = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024c01003b"
)
_NO_STORE_HEADERS = {"Cache-Control": "no-store, max-age=0"}


@router.get("/track/open")
async def track_open(
    token: str | None = Query(default=None),
    service: MailOpenTrackingService = Depends(get_mail_open_tracking_service),
):
    """Read as: 'record an open if this token is real, then always
    return a 1x1 transparent GIF' -- see this module's own SECURITY note
    for why the response is identical whether or not `token` resolves to
    anything. A missing token is silently a no-op (not an error) for the
    same reason."""
    if token:
        await service.record_open(token, datetime.now(timezone.utc))
    return Response(content=_TRANSPARENT_GIF_BYTES, media_type="image/gif", headers=_NO_STORE_HEADERS)
