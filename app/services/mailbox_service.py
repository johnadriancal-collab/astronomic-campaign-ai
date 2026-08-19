"""
MailboxService -- Astronomic Mail Phase 2 (Google Workspace Mailbox
Connection). Orchestrates the OAuth authorization-code flow, mailbox
dedup/upsert, and disconnect. CONNECTION ONLY -- nothing here ever sends an
email, queues one, or activates a campaign; see this module's own tests for
explicit "never calls gmail.send/messages.send" assertions.

CSRF `state` is held IN-MEMORY ONLY, not persisted -- this is a deliberate
exception to this app's usual "everything persists" convention (see e.g.
MailCampaignService). An in-flight OAuth attempt lost to a backend restart
is harmless (the user just retries "Connect Email"); unlike a Mailbox or its
credential, there is no data-loss cost to keeping this ephemeral, and it
avoids adding a table for something inherently short-lived and single-use.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.google.oauth_client import (
    GoogleOAuthClient,
    GoogleTokenExchangeError,
    GoogleUserinfoError,
    generate_state,
)
from app.models.crm import normalize_email
from app.models.mailbox import Mailbox, MailboxCredential, MailboxProvider, MailboxStatus
from app.repositories.mailbox_credential_store import MailboxCredentialStore
from app.repositories.mailbox_store import MailboxStore
from app.services.token_encryption import TokenEncryptionNotConfiguredError, decrypt_refresh_token, encrypt_refresh_token

_STATE_TTL = timedelta(minutes=10)


@dataclass
class _PendingState:
    created_at: datetime


class MailboxNotFound(Exception):
    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox not found: {mailbox_id}")


class MailboxOAuthStateError(Exception):
    """State missing, expired, or already used -- deliberately a single
    error for all three cases, so a forged/replayed/stale callback is
    rejected identically from the outside."""


class MailboxOAuthDeniedError(Exception):
    """Google returned an `error` param (e.g. the user clicked Cancel)."""

    def __init__(self, error: str):
        self.error = error
        super().__init__(f"Google denied authorization: {error}")


class MailboxOAuthMissingCodeError(Exception):
    """Google's callback had no `error` but also no `code` -- malformed."""


class MailboxService:
    def __init__(
        self,
        mailbox_store: MailboxStore,
        credential_store: MailboxCredentialStore,
        oauth_client: GoogleOAuthClient,
    ):
        self.mailbox_store = mailbox_store
        self.credential_store = credential_store
        self.oauth_client = oauth_client
        self._pending_states: dict[str, _PendingState] = {}

    async def list_mailboxes(self) -> list[Mailbox]:
        return await self.mailbox_store.list()

    def begin_google_oauth(self) -> str:
        """Returns the URL the frontend should navigate the browser to.
        Raises GoogleOAuthNotConfiguredError (via the oauth_client) if
        Google credentials aren't set -- the API layer maps that to a 503."""
        self._prune_expired_states()
        state = generate_state()
        authorize_url = self.oauth_client.build_authorize_url(state)
        self._pending_states[state] = _PendingState(created_at=datetime.now(timezone.utc))
        return authorize_url

    def _prune_expired_states(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [s for s, p in self._pending_states.items() if now - p.created_at > _STATE_TTL]
        for s in expired:
            del self._pending_states[s]

    def _consume_state(self, state: str | None) -> None:
        """Single-use: removes the state whether or not it was found, so a
        replay of the same callback URL always fails on the second attempt."""
        self._prune_expired_states()
        if not state or self._pending_states.pop(state, None) is None:
            raise MailboxOAuthStateError("Missing, expired, or already-used OAuth state.")

    async def handle_google_callback(self, code: str | None, state: str | None, error: str | None) -> Mailbox:
        """
        The one place a Google authorization is turned into a stored,
        connected Mailbox. Validates state FIRST (before even looking at
        `code`/`error`) so a forged or replayed callback is rejected
        identically regardless of what else is in the query string.

        Dedup: looks up an existing Mailbox by `google_user_id` (the OIDC
        `sub` claim -- the one Google-guaranteed-stable identifier) first,
        then by normalized email as a defensive fallback. If found, that
        row (and its credential) is UPDATED in place rather than a second
        row being created -- see tests/test_mailbox_service.py's explicit
        "reconnecting the same account never duplicates" case.
        """
        self._consume_state(state)

        if error:
            raise MailboxOAuthDeniedError(error)
        if not code:
            raise MailboxOAuthMissingCodeError("Google's callback had no authorization code.")

        tokens = await self.oauth_client.exchange_code(code)
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not access_token:
            raise GoogleTokenExchangeError("Google's token response had no access_token.")

        userinfo = await self.oauth_client.fetch_userinfo(access_token)
        google_user_id = userinfo.get("sub")
        email = normalize_email(userinfo.get("email") or "")
        display_name = userinfo.get("name")
        granted_scopes = sorted((tokens.get("scope") or "").split())

        if not email:
            raise GoogleUserinfoError("Google's userinfo response had no usable email address.")

        now = datetime.now(timezone.utc)
        existing = None
        if google_user_id:
            existing = await self.mailbox_store.get_by_google_user_id(google_user_id)
        if existing is None:
            existing = await self.mailbox_store.get_by_email(email)

        # Encrypt BEFORE writing anything to either store. If
        # MAILBOX_TOKEN_ENCRYPTION_KEY is missing/invalid,
        # encrypt_refresh_token() raises TokenEncryptionNotConfiguredError
        # right here -- before any Mailbox row is created or marked
        # connected. This ordering is deliberate and load-bearing: a
        # mailbox must never be left showing status=CONNECTED while no
        # usable credential is actually stored for it. (An earlier version
        # of this method persisted the Mailbox first and encrypted after --
        # a misconfigured key then left a "Connected"-looking mailbox with
        # no stored credential at all, while the user was shown an error.
        # See tests/test_mailbox_service.py's
        # test_encryption_failure_leaves_no_partially_connected_mailbox*
        # for the regression coverage.)
        #
        # refresh_token is only ever present on the FIRST consent for a
        # given grant (or whenever Google re-issues one, which
        # build_authorize_url()'s prompt=consent always requests) -- if
        # Google ever omits it, keep whatever credential we already have
        # rather than wiping a working one, and skip encryption entirely
        # (nothing new to encrypt).
        encrypted_refresh_token: str | None = None
        if refresh_token:
            encrypted_refresh_token = encrypt_refresh_token(refresh_token)

        if existing is not None:
            mailbox = existing.model_copy(
                update={
                    "email": email,
                    "display_name": display_name or existing.display_name,
                    "status": MailboxStatus.CONNECTED,
                    "google_user_id": google_user_id or existing.google_user_id,
                    "granted_scopes": granted_scopes or existing.granted_scopes,
                    "updated_at": now,
                    "disconnected_at": None,
                }
            )
            await self.mailbox_store.save(mailbox)
        else:
            mailbox = Mailbox(
                mailbox_id=str(uuid.uuid4()),
                provider=MailboxProvider.GOOGLE,
                email=email,
                display_name=display_name,
                status=MailboxStatus.CONNECTED,
                google_user_id=google_user_id,
                granted_scopes=granted_scopes,
                connected_at=now,
                updated_at=now,
            )
            await self.mailbox_store.create(mailbox)

        if encrypted_refresh_token is not None:
            existing_credential = await self.credential_store.get(mailbox.mailbox_id)
            if existing_credential is not None:
                await self.credential_store.save(
                    existing_credential.model_copy(
                        update={"encrypted_refresh_token": encrypted_refresh_token, "updated_at": now}
                    )
                )
            else:
                await self.credential_store.create(
                    MailboxCredential(
                        mailbox_id=mailbox.mailbox_id,
                        encrypted_refresh_token=encrypted_refresh_token,
                        created_at=now,
                        updated_at=now,
                    )
                )

        logger.info(f"Mailbox connected: {mailbox.mailbox_id} ({mailbox.provider.value}).")
        return mailbox

    async def disconnect_mailbox(self, mailbox_id: str) -> Mailbox:
        """Best-effort revoke against Google, then the encrypted credential
        is deleted outright regardless of whether Google's revoke call
        succeeded -- what matters is that WE no longer retain usable
        credentials. The Mailbox row itself is kept with status=disconnected
        (never deleted), matching MailCampaignStatus.ARCHIVED's "keep the
        row, use a terminal status" precedent, for audit history."""
        mailbox = await self.mailbox_store.get(mailbox_id)
        if mailbox is None:
            raise MailboxNotFound(mailbox_id)

        credential = await self.credential_store.get(mailbox_id)
        if credential is not None:
            try:
                refresh_token = decrypt_refresh_token(credential.encrypted_refresh_token)
                await self.oauth_client.revoke_token(refresh_token)
            except TokenEncryptionNotConfiguredError:
                logger.warning(f"Could not decrypt credential for {mailbox_id} to revoke it -- deleting stored credential anyway.")
            except Exception as e:  # noqa: BLE001 -- best-effort revoke must never block disconnect
                logger.warning(f"Google token revocation failed for {mailbox_id}: {type(e).__name__} -- deleting stored credential anyway.")
            await self.credential_store.delete(mailbox_id)

        now = datetime.now(timezone.utc)
        updated = mailbox.model_copy(
            update={"status": MailboxStatus.DISCONNECTED, "disconnected_at": now, "updated_at": now}
        )
        await self.mailbox_store.save(updated)
        logger.info(f"Mailbox disconnected: {mailbox_id}.")
        return updated
