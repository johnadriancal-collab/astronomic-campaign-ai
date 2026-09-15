"""
Astronomic Mail Phase 2 (Google Workspace mailbox CONNECTION) + Phase B1
(Gmail scope upgrade + token refresh foundation).

IMPORTANT, still load-bearing: there is no route here (and none anywhere
else in this app) that sends an email, queues one, or activates a
campaign. `MailboxService.begin_gmail_send_upgrade()` requests the
`gmail.send` scope (see app/google/oauth_client.py's GMAIL_SEND_SCOPE) for
an existing, already-connected mailbox -- reachable via GET
/{mailbox_id}/google/gmail-send/start below, session-gated the same as
every other route in this router (no per-route auth dependency needed --
see app/session_auth_middleware.py, which protects everything not in
PUBLIC_PATHS). Starting this flow never mutates the mailbox itself -- it
only registers a pending OAuth state (see begin_gmail_send_upgrade()'s own
docstring); every actual scope/status change happens exclusively inside
handle_google_callback(), same as the ordinary connect flow. The ordinary
connect flow (`/google/start`, `begin_google_oauth()`) is completely
unchanged: base scopes only (`openid email profile`).

The callback route always ends in an HTTP redirect back to the frontend's
/manager/emails -- never a JSON error response a real browser navigation
could actually see, and never a redirect carrying token/code details in its
own query string (only a short, opaque error code, if any).
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from app.config import settings
from app.dependencies import get_mailbox_service
from app.google.oauth_client import (
    GoogleOAuthNotConfiguredError,
    GoogleRefreshTokenInvalidError,
    GoogleTokenExchangeError,
    GoogleTokenRefreshError,
    GoogleUserinfoError,
)
from app.google.gmail_thread_reader_client import (
    GmailReadError,
    GmailThreadReaderClient,
    extract_headers,
)
from app.models.mailbox import Mailbox
from app.services.mailbox_service import (
    MailboxCredentialMissingError,
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


@router.get("/{mailbox_id}/google/gmail-send/start")
async def start_gmail_send_upgrade(mailbox_id: str, service: MailboxService = Depends(get_mailbox_service)):
    """Begins the GMAIL_SEND_UPGRADE flow for an EXISTING mailbox -- see
    MailboxService.begin_gmail_send_upgrade()'s own docstring for the full
    contract (full desired scope set requested, pending state tagged with
    `expected_mailbox_id`, no mailbox mutation on initiation). Mirrors
    start_google_oauth() above exactly: returns the authorize URL as JSON
    for the frontend to do a full top-level navigation to, rather than
    redirecting itself (this IS an ordinary same-origin fetch from an
    already-loaded Hub page, unlike the callback route below, which Google
    itself navigates to directly)."""
    try:
        return {"authorize_url": await service.begin_gmail_send_upgrade(mailbox_id)}
    except MailboxNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
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


# --- Gmail read-only diagnostics (2026-09-15, temporary) ---------------------
#
# Added specifically to settle a real threading-verification contradiction
# (AstroHub's own outbound pipeline is provably correct end-to-end -- see
# tests/test_mail_threading_gmail_boundary.py -- yet Gmail's UI persists in
# showing two separate conversations for a controlled test). These two
# routes are the ONLY way to ask Gmail's server what it actually persisted,
# since no reply-detection capability exists yet to do this automatically.
# Requires the mailbox to have been reconnected with gmail.metadata (see
# begin_gmail_send_upgrade()) -- MailboxOAuthScopeNotGrantedError-shaped
# 403s from Gmail itself are the signal that hasn't happened yet. Session-
# gated like every other route in this router; deliberately NOT reachable
# via the admin/service operator token (see app/session_auth_middleware.py's
# own explicit exclusion list: "everything under /mailboxes/* except the
# bare GET list"). Returns headers only (From/To/Subject/Message-ID/
# In-Reply-To/References) -- gmail.metadata cannot return body/snippet
# content even if this code asked for it, and extract_headers() only ever
# pulls the fixed METADATA_HEADERS allowlist regardless.


async def _refresh_access_token_or_502(mailbox_id: str, service: MailboxService) -> str:
    try:
        return await service.refresh_mailbox_access_token(mailbox_id)
    except MailboxNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailboxCredentialMissingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except GoogleRefreshTokenInvalidError:
        raise HTTPException(status_code=409, detail="This mailbox needs to be reconnected (Google reports its grant is no longer valid).")
    except GoogleTokenRefreshError:
        raise HTTPException(status_code=502, detail="Could not refresh a Gmail access token for this mailbox.")


@router.get("/{mailbox_id}/gmail-diagnostic/threads/{thread_id}")
async def gmail_diagnostic_get_thread(mailbox_id: str, thread_id: str, service: MailboxService = Depends(get_mailbox_service)):
    access_token = await _refresh_access_token_or_502(mailbox_id, service)
    try:
        data = await GmailThreadReaderClient().get_thread(access_token=access_token, thread_id=thread_id)
    except GmailReadError as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")

    return {
        "thread_id": data["id"],
        "message_count": len(data.get("messages", [])),
        "messages": [
            {"id": m.get("id"), "threadId": m.get("threadId"), "headers": extract_headers(m)}
            for m in data.get("messages", [])
        ],
    }


@router.get("/{mailbox_id}/gmail-diagnostic/messages/{message_id}")
async def gmail_diagnostic_get_message(mailbox_id: str, message_id: str, service: MailboxService = Depends(get_mailbox_service)):
    access_token = await _refresh_access_token_or_502(mailbox_id, service)
    try:
        data = await GmailThreadReaderClient().get_message(access_token=access_token, message_id=message_id)
    except GmailReadError as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}")

    return {"id": data["id"], "threadId": data["threadId"], "headers": extract_headers(data)}
