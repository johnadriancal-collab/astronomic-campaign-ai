"""
MailboxService -- Astronomic Mail Phase 2 (Google Workspace Mailbox
Connection) + Phase B1 (Gmail scope upgrade + token refresh foundation).
Orchestrates the OAuth authorization-code flow, mailbox dedup/upsert,
disconnect, the Gmail-send scope upgrade for an EXISTING mailbox, and
access-token refresh. STILL connection/authorization only -- nothing here
ever sends an email, queues one, or activates a campaign; see this
module's own tests for explicit "never calls gmail.send/messages.send"
assertions.

CSRF `state` is held IN-MEMORY ONLY, not persisted -- this is a deliberate
exception to this app's usual "everything persists" convention (see e.g.
MailCampaignService). An in-flight OAuth attempt lost to a backend restart
is harmless (the user just retries "Connect Email"/"Enable Sending"); unlike
a Mailbox or its credential, there is no data-loss cost to keeping this
ephemeral, and it avoids adding a table for something inherently short-lived
and single-use. See the Phase B1 report for the explicit evaluation of
whether this remains acceptable now that a scope-upgrade flow depends on it
too -- accepted as-is, not redesigned, given Railway deploys are infrequent,
human-triggered events and the OAuth round trip itself normally completes in
well under a minute; the failure mode if a redeploy DOES land mid-flow is a
clean, retryable `MailboxOAuthStateError` (`?error=state_mismatch`), never
data loss or a corrupted mailbox.

CREDENTIAL-REPLACEMENT WRITE ORDER (Phase B1 correction): `Mailbox` and
`MailboxCredential` are separate stores/connections -- there is NO
cross-store transaction here, and this module never claims one (see
sqlite_txn.py's docstring for why that's true everywhere in this codebase).
handle_google_callback() writes the CREDENTIAL first, the public Mailbox
row SECOND -- the reverse of a naive "mailbox looks more important" order,
and deliberately so: if the process crashes/raises between the two writes,
the public Mailbox row is left showing whatever it already reflected
BEFORE this call (old `granted_scopes`, old `status`) while the credential
underneath it is already the NEW one. That failure direction is safe --
the public row never OVER-claims a capability (e.g. gmail.send) the stored
credential doesn't actually back; at worst it briefly UNDER-reports one,
which every scope-gated caller correctly treats as "not yet upgraded."
The opposite order (mailbox first) would let a crash leave the public row
claiming gmail.send while the credential underneath still only supports
the old scope set -- exactly the invariant a production audit flagged as
unacceptable. This is the same "prepare the risky write, commit the
authoritative/visible state last" shape as MailCampaignService.
activate_campaign()'s PREPARE/COMMIT contract -- a deliberately adopted,
idempotent-and-resumable pattern, not literal cross-store atomicity, which
this codebase's independent SQLite connections cannot provide.

SECOND correction, same audit, opposite direction: writing the credential
FIRST is safe for the "Mailbox must never over-claim" invariant, but a
naive overwrite there creates a DIFFERENT gap -- a crash AFTER the
credential write but BEFORE the Mailbox COMMIT would otherwise leave the
PREVIOUSLY-WORKING credential permanently destroyed, with no way back,
even though the upgrade never actually completed. The credential write
therefore preserves the value it's about to replace into
`MailboxCredential.previous_encrypted_refresh_token` (see that field's own
docstring) rather than simply discarding it. This is intentionally a
small, single-field addition -- no separate staging table, no credential
versioning system, no cross-store transaction -- consistent with "don't
introduce a large framework to satisfy one invariant." Nothing in Phase B1
reads this field automatically (no auto-rollback exists); it is a durable,
decryptable recovery trace for a human/future-repair-path to use, not an
active fallback mechanism.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger

from app.google.oauth_client import (
    GMAIL_SEND_SCOPE,
    SCOPES,
    GoogleOAuthClient,
    GoogleRefreshTokenInvalidError,
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


class OAuthFlowType(str, Enum):
    """What a pending `state` value was issued for -- determines how
    handle_google_callback() interprets and validates the result. Never
    serialized/persisted (see this module's own docstring on why pending
    state is in-memory only)."""

    CONNECT = "connect"
    GMAIL_SEND_UPGRADE = "gmail_send_upgrade"


@dataclass
class _PendingState:
    created_at: datetime
    flow_type: OAuthFlowType = OAuthFlowType.CONNECT
    # Set only for GMAIL_SEND_UPGRADE -- the mailbox this upgrade must
    # resolve back to. handle_google_callback() verifies the Google
    # account that actually completes the flow matches THIS mailbox's
    # google_user_id before writing anything -- see
    # MailboxOAuthAccountMismatchError.
    expected_mailbox_id: str | None = None


class MailboxNotFound(Exception):
    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox not found: {mailbox_id}")


class MailboxCredentialMissingError(Exception):
    """A mailbox row exists but has no stored credential at all -- an
    anomalous state (see this module's docstring on why the credential-
    first write order exists specifically to prevent it). Raised by
    refresh_mailbox_access_token() rather than proceeding with nothing to
    refresh."""

    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox {mailbox_id} has no stored credential.")


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


class MailboxOAuthAccountMismatchError(Exception):
    """A GMAIL_SEND_UPGRADE flow completed, but the Google account that
    actually authorized is NOT the account already connected as the
    target mailbox. Zero mutation: no store write happens before this
    check, so the target mailbox and its existing credential are
    completely untouched, and no new mailbox is created for the
    unexpected account either -- an upgrade attempt is never allowed to
    silently become a fresh connection."""

    def __init__(self, mailbox_id: str, expected_google_user_id: str | None, actual_google_user_id: str | None):
        self.mailbox_id = mailbox_id
        self.expected_google_user_id = expected_google_user_id
        self.actual_google_user_id = actual_google_user_id
        super().__init__(f"Mailbox {mailbox_id}: OAuth completed with a different Google account than expected.")


class MailboxOAuthScopeNotGrantedError(Exception):
    """A GMAIL_SEND_UPGRADE flow completed for the correct account, but
    Google's token response did not actually include `gmail.send` in
    `scope`. Zero mutation -- the prior mailbox/credential state is
    preserved exactly as it was; `granted_scopes` must only ever reflect
    what Google actually granted, never what was requested (see
    Mailbox.granted_scopes' own docstring)."""

    def __init__(self, mailbox_id: str, required_scope: str, granted_scopes: list[str]):
        self.mailbox_id = mailbox_id
        self.required_scope = required_scope
        self.granted_scopes = granted_scopes
        super().__init__(f"Mailbox {mailbox_id}: upgrade did not include the required scope {required_scope!r}.")


class MailboxOAuthUpgradeMissingRefreshTokenError(Exception):
    """A GMAIL_SEND_UPGRADE flow completed for the correct account with
    the required scope reported in THIS exchange's `scope` field, but
    Google's token response included no `refresh_token`. Treated as a
    failed upgrade (not merely "keep the old credential," as the ordinary
    reconnect flow does).

    Researched, not assumed: Google's documentation confirms that when a
    fresh refresh token IS issued for a combined/incremental grant, using
    it covers every scope in that combined authorization -- but it does
    NOT document the reverse guarantee we'd need to safely relax this
    check: that an EXISTING refresh token, issued before this upgrade and
    never itself reissued, automatically starts covering a newly-granted
    scope purely because the account-level grant to this client_id was
    expanded. No authoritative source confirms that. Given that gap, and
    given this app already unconditionally sends `prompt=consent` (which
    Google's own docs describe as reliably re-prompting and reissuing a
    refresh token even for a returning user, unlike the undecorated
    default flow, where a refresh token is normally issued only on first
    consent) -- an upgrade genuinely omitting a fresh refresh_token would
    be an ANOMALY under this app's specific configuration, not the
    expected case. The conservative choice given that ambiguity: require
    the fresh token. Zero mutation on this path; the caller should simply
    retry."""

    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox {mailbox_id}: upgrade succeeded but Google issued no refresh token.")


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
        """Returns the URL the frontend should navigate the browser to, for
        an ORDINARY connect/reconnect -- base scopes only (`openid email
        profile`), unchanged from before Phase B1. Raises
        GoogleOAuthNotConfiguredError (via the oauth_client) if Google
        credentials aren't set -- the API layer maps that to a 503."""
        self._prune_expired_states()
        state = generate_state()
        authorize_url = self.oauth_client.build_authorize_url(state, scopes=SCOPES)
        self._pending_states[state] = _PendingState(created_at=datetime.now(timezone.utc), flow_type=OAuthFlowType.CONNECT)
        return authorize_url

    async def begin_gmail_send_upgrade(self, mailbox_id: str) -> str:
        """Returns the URL to send the browser to for upgrading an
        EXISTING, already-connected mailbox to also grant `gmail.send`.
        Requests the FULL desired scope set (`openid email profile
        gmail.send`), not just the delta -- `include_granted_scopes=true`
        (set unconditionally in build_authorize_url()) is Google's own
        documented incremental-authorization mechanism and remains the
        safety net, but requesting the complete set explicitly is the
        primary, unambiguous path (see GoogleOAuthClient.
        build_authorize_url()'s docstring).

        Raises MailboxNotFound if `mailbox_id` doesn't exist. Does NOT
        require the mailbox to currently be CONNECTED -- an upgrade
        attempt against a DISCONNECTED mailbox is unusual but harmless;
        it would simply reconnect-and-upgrade in one step on success, and
        every failure path (wrong account, scope not granted, no refresh
        token) still leaves it exactly as it was, same as any other
        rejected upgrade."""
        mailbox = await self.mailbox_store.get(mailbox_id)
        if mailbox is None:
            raise MailboxNotFound(mailbox_id)

        self._prune_expired_states()
        state = generate_state()
        authorize_url = self.oauth_client.build_authorize_url(state, scopes=(*SCOPES, GMAIL_SEND_SCOPE))
        self._pending_states[state] = _PendingState(
            created_at=datetime.now(timezone.utc),
            flow_type=OAuthFlowType.GMAIL_SEND_UPGRADE,
            expected_mailbox_id=mailbox_id,
        )
        return authorize_url

    def _prune_expired_states(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [s for s, p in self._pending_states.items() if now - p.created_at > _STATE_TTL]
        for s in expired:
            del self._pending_states[s]

    def _consume_state(self, state: str | None) -> _PendingState:
        """Single-use: removes the state whether or not it was found, so a
        replay of the same callback URL always fails on the second
        attempt. Returns the consumed _PendingState so the caller knows
        which flow this callback belongs to."""
        self._prune_expired_states()
        if not state:
            raise MailboxOAuthStateError("Missing, expired, or already-used OAuth state.")
        pending = self._pending_states.pop(state, None)
        if pending is None:
            raise MailboxOAuthStateError("Missing, expired, or already-used OAuth state.")
        return pending

    async def handle_google_callback(self, code: str | None, state: str | None, error: str | None) -> Mailbox:
        """
        The one place a Google authorization is turned into a stored,
        connected Mailbox -- for BOTH the ordinary connect/reconnect flow
        and the GMAIL_SEND_UPGRADE flow (see OAuthFlowType). Validates
        state FIRST (before even looking at `code`/`error`) so a forged or
        replayed callback is rejected identically regardless of what else
        is in the query string.

        Dedup (CONNECT flow only): looks up an existing Mailbox by
        `google_user_id` (the OIDC `sub` claim -- the one Google-
        guaranteed-stable identifier) first, then by normalized email as a
        defensive fallback. If found, that row (and its credential) is
        UPDATED in place rather than a second row being created -- see
        tests/test_mailbox_service.py's explicit "reconnecting the same
        account never duplicates" case.

        GMAIL_SEND_UPGRADE flow: resolves directly to the `expected_
        mailbox_id` the upgrade was started for (no generic dedup search
        at all -- we already know exactly which mailbox this is). Before
        ANY store write, verifies (in order): the returned Google account
        matches that mailbox's `google_user_id` (else
        MailboxOAuthAccountMismatchError), the required GMAIL_SEND_SCOPE
        is actually present in `granted_scopes` (else
        MailboxOAuthScopeNotGrantedError), and a refresh_token was
        actually issued in this exchange (else
        MailboxOAuthUpgradeMissingRefreshTokenError). Every one of these
        three checks fails with ZERO mutation -- see this module's
        docstring for the credential-first write order that protects
        every write that DOES happen after these checks pass.
        """
        pending = self._consume_state(state)

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

        if pending.flow_type == OAuthFlowType.GMAIL_SEND_UPGRADE:
            target = await self.mailbox_store.get(pending.expected_mailbox_id)
            if target is None:
                raise MailboxNotFound(pending.expected_mailbox_id)
            if google_user_id != target.google_user_id:
                raise MailboxOAuthAccountMismatchError(pending.expected_mailbox_id, target.google_user_id, google_user_id)
            if GMAIL_SEND_SCOPE not in granted_scopes:
                raise MailboxOAuthScopeNotGrantedError(pending.expected_mailbox_id, GMAIL_SEND_SCOPE, granted_scopes)
            if not refresh_token:
                raise MailboxOAuthUpgradeMissingRefreshTokenError(pending.expected_mailbox_id)
            existing = target
        else:
            existing = None
            if google_user_id:
                existing = await self.mailbox_store.get_by_google_user_id(google_user_id)
            if existing is None:
                existing = await self.mailbox_store.get_by_email(email)

        # Encrypt BEFORE writing anything to either store. If
        # MAILBOX_TOKEN_ENCRYPTION_KEY is missing/invalid,
        # encrypt_refresh_token() raises TokenEncryptionNotConfiguredError
        # right here -- before any Mailbox row is created or marked
        # connected, and before the credential write below. (An earlier
        # version of this method persisted the Mailbox first and encrypted
        # after -- a misconfigured key then left a "Connected"-looking
        # mailbox with no stored credential at all. See tests/
        # test_mailbox_service.py's test_encryption_failure_* regression
        # coverage.)
        #
        # refresh_token is only ever present on the FIRST consent for a
        # given grant (or whenever Google re-issues one, which
        # build_authorize_url()'s prompt=consent always requests) -- for
        # the ORDINARY connect/reconnect flow, if Google ever omits it,
        # keep whatever credential we already have rather than wiping a
        # working one, and skip encryption entirely (nothing new to
        # encrypt). The GMAIL_SEND_UPGRADE flow already required a
        # refresh_token to be present above, so this branch is only ever
        # "no new token" for CONNECT.
        encrypted_refresh_token: str | None = None
        if refresh_token:
            encrypted_refresh_token = encrypt_refresh_token(refresh_token)

        # --- PREPARE: the credential is written FIRST -- see this
        # module's own docstring for exactly why this order (not "mailbox
        # first") is what makes a crash between the two writes safe for
        # the "Mailbox must never over-claim" invariant.
        #
        # THIS write also preserves whatever `encrypted_refresh_token` was
        # immediately before it, into `previous_encrypted_refresh_token`,
        # rather than simply overwriting it -- see MailboxCredential's own
        # docstring. This closes a DIFFERENT, opposite-direction gap a
        # production audit identified: without this, a crash AFTER this
        # write but BEFORE the Mailbox COMMIT below would leave the
        # PREVIOUSLY-WORKING credential permanently destroyed (overwritten
        # with a not-yet-publicly-reflected replacement) with no way to
        # recover it. Preserving it here means that specific failure
        # window is now fully recoverable -- the old, still-valid
        # ciphertext sits right there, decryptable, never lost.
        if encrypted_refresh_token is not None and existing is not None:
            existing_credential = await self.credential_store.get(existing.mailbox_id)
            if existing_credential is not None:
                await self.credential_store.save(
                    existing_credential.model_copy(
                        update={
                            "previous_encrypted_refresh_token": existing_credential.encrypted_refresh_token,
                            "encrypted_refresh_token": encrypted_refresh_token,
                            "updated_at": now,
                        }
                    )
                )
            else:
                await self.credential_store.create(
                    MailboxCredential(
                        mailbox_id=existing.mailbox_id,
                        encrypted_refresh_token=encrypted_refresh_token,
                        created_at=now,
                        updated_at=now,
                    )
                )

        # --- COMMIT: the public Mailbox row is written LAST.
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
            # Brand-new mailbox (CONNECT flow only -- GMAIL_SEND_UPGRADE
            # always resolves `existing` to the known target above, so
            # this branch never runs for an upgrade). mailbox_id is minted
            # here, in memory, BEFORE either write, so the credential
            # (if any) can still be written first, keyed by this same id.
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
            if encrypted_refresh_token is not None:
                await self.credential_store.create(
                    MailboxCredential(
                        mailbox_id=mailbox.mailbox_id,
                        encrypted_refresh_token=encrypted_refresh_token,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await self.mailbox_store.create(mailbox)

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

    async def refresh_mailbox_access_token(self, mailbox_id: str) -> str:
        """Decrypts this mailbox's stored refresh token and exchanges it
        for a fresh access token via GoogleOAuthClient.refresh_access_
        token(). The access token is returned to the caller and is NEVER
        stored anywhere by this method (or anywhere else in this
        codebase) -- it exists only transiently, for the immediate
        caller's own use.

        This is the ONLY place in this codebase that calls
        refresh_access_token(), and therefore the ONE place CONNECTED can
        ever become NEEDS_REAUTH: exclusively on
        GoogleRefreshTokenInvalidError (Google's confirmed `invalid_grant`
        -- the grant itself is gone, not a transient hiccup). Every OTHER
        failure -- GoogleTokenRefreshError (network/5xx/other provider
        error), GoogleTokenRefreshMalformedResponseError, a decryption
        failure, a missing credential -- propagates to the caller WITHOUT
        touching mailbox status at all. There is no Gmail sender anywhere
        in this codebase yet (Phase B1 scope), so nothing calls this
        method automatically today -- it exists as a complete, correct,
        independently-tested foundation for when one does."""
        mailbox = await self.mailbox_store.get(mailbox_id)
        if mailbox is None:
            raise MailboxNotFound(mailbox_id)

        credential = await self.credential_store.get(mailbox_id)
        if credential is None:
            raise MailboxCredentialMissingError(mailbox_id)

        refresh_token = decrypt_refresh_token(credential.encrypted_refresh_token)

        try:
            tokens = await self.oauth_client.refresh_access_token(refresh_token)
        except GoogleRefreshTokenInvalidError:
            now = datetime.now(timezone.utc)
            updated = mailbox.model_copy(update={"status": MailboxStatus.NEEDS_REAUTH, "updated_at": now})
            await self.mailbox_store.save(updated)
            logger.warning(f"Mailbox {mailbox_id} moved CONNECTED -> NEEDS_REAUTH (invalid_grant on token refresh).")
            raise

        return tokens["access_token"]
