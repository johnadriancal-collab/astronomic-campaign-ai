"""
Astronomic Mail Phase 2 (Google Workspace mailbox CONNECTION) + Phase B1
(Gmail scope upgrade + token refresh foundation).

IMPORTANT, still load-bearing: there is no route here (and none anywhere
else in this app) that sends an email, queues one, or activates a
campaign. `MailboxService.begin_gmail_send_upgrade()` CAN request the
`gmail.send` scope (see app/google/oauth_client.py's GMAIL_SEND_SCOPE) for
an existing mailbox -- but no route in this file (or anywhere else) calls
it yet; it exists only as a directly-callable, directly-tested service
method (see tests/test_mailbox_service.py), with no frontend affordance
and no API entry point wired up in Phase B1. The ordinary connect flow
(`/google/start`, `begin_google_oauth()`) is completely unchanged: base
scopes only (`openid email profile`).

The callback route always ends in an HTTP redirect back to the frontend's
/manager/emails -- never a JSON error response a real browser navigation
could actually see, and never a redirect carrying token/code details in its
own query string (only a short, opaque error code, if any).
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.config import settings
from app.dependencies import get_mailbox_service
from app.google.oauth_client import GoogleOAuthNotConfiguredError, GoogleTokenExchangeError, GoogleUserinfoError
from app.models.mailbox import Mailbox
from app.services.mailbox_service import (
    MailboxNotFound,
    MailboxOAuthAccountMismatchError,
    MailboxOAuthDeniedError,
    MailboxOAuthMissingCodeError,
    MailboxOAuthScopeNotGrantedError,
    MailboxOAuthStateError,
    MailboxOAuthUpgradeMissingRefreshTokenError,
    MailboxService,
)
from app.services.token_encryption import TokenEncryptionNotConfiguredError

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


def _frontend_url(path: str) -> str:
    base = (settings.frontend_origin or "").rstrip("/")
    return f"{base}{path}"


@router.get("", response_model=list[Mailbox])
async def list_mailboxes(service: MailboxService = Depends(get_mailbox_service)):
    return await service.list_mailboxes()


@router.get("/google/start")
async def start_google_oauth(service: MailboxService = Depends(get_mailbox_service)):
    try:
        return {"authorize_url": service.begin_google_oauth()}
    except GoogleOAuthNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/google/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    service: MailboxService = Depends(get_mailbox_service),
):
    """
    Google redirects the user's browser here directly (never through the
    frontend's /backend/* rewrite proxy, since this is a top-level browser
    navigation Google itself issues) -- see app/config.py's
    google_oauth_redirect_uri/frontend_origin docstrings.
    """
    if not settings.frontend_origin:
        raise HTTPException(status_code=503, detail="FRONTEND_ORIGIN is not configured.")

    try:
        await service.handle_google_callback(code=code, state=state, error=error)
        return RedirectResponse(_frontend_url("/manager/emails?connected=1"))
    except MailboxOAuthStateError:
        return RedirectResponse(_frontend_url("/manager/emails?error=state_mismatch"))
    except MailboxOAuthDeniedError:
        return RedirectResponse(_frontend_url("/manager/emails?error=access_denied"))
    except MailboxOAuthMissingCodeError:
        return RedirectResponse(_frontend_url("/manager/emails?error=missing_code"))
    except (GoogleTokenExchangeError, GoogleUserinfoError):
        return RedirectResponse(_frontend_url("/manager/emails?error=token_exchange_failed"))
    except GoogleOAuthNotConfiguredError:
        return RedirectResponse(_frontend_url("/manager/emails?error=not_configured"))
    except TokenEncryptionNotConfiguredError:
        return RedirectResponse(_frontend_url("/manager/emails?error=not_configured"))
    except MailboxNotFound:
        # Only reachable from a GMAIL_SEND_UPGRADE flow whose target
        # mailbox vanished between begin_gmail_send_upgrade() and this
        # callback (e.g. disconnected+deleted -- disconnect never deletes
        # the row today, so this is defensive, not an expected case).
        return RedirectResponse(_frontend_url("/manager/emails?error=mailbox_not_found"))
    except MailboxOAuthAccountMismatchError:
        return RedirectResponse(_frontend_url("/manager/emails?error=account_mismatch"))
    except MailboxOAuthScopeNotGrantedError:
        return RedirectResponse(_frontend_url("/manager/emails?error=scope_not_granted"))
    except MailboxOAuthUpgradeMissingRefreshTokenError:
        return RedirectResponse(_frontend_url("/manager/emails?error=upgrade_needs_retry"))


@router.post("/{mailbox_id}/disconnect", response_model=Mailbox)
async def disconnect_mailbox(mailbox_id: str, service: MailboxService = Depends(get_mailbox_service)):
    try:
        return await service.disconnect_mailbox(mailbox_id)
    except MailboxNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
