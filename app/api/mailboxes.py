"""
Astronomic Mail Phase 2 -- Google Workspace mailbox CONNECTION only.

IMPORTANT, load-bearing for this phase's safety guarantee: there is no
route here (and none anywhere else in this app) that sends an email, queues
one, or activates a campaign. The OAuth scopes requested (see
app/google/oauth_client.py) never include any `gmail.*` scope -- connecting
a mailbox here grants this backend NOTHING beyond confirming which Google
account it is.

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
    MailboxOAuthDeniedError,
    MailboxOAuthMissingCodeError,
    MailboxOAuthStateError,
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


@router.post("/{mailbox_id}/disconnect", response_model=Mailbox)
async def disconnect_mailbox(mailbox_id: str, service: MailboxService = Depends(get_mailbox_service)):
    try:
        return await service.disconnect_mailbox(mailbox_id)
    except MailboxNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
