"""
MailboxService tests -- Astronomic Mail Phase 2 (Google Workspace Mailbox
Connection). FakeGoogleOAuthClient below NEVER makes a real network call to
Google; every test exercises MailboxService's own logic (state validation,
dedup/upsert, encryption, disconnect/revoke) against canned responses.

Also asserts, explicitly, that nothing here is capable of sending mail:
FakeGoogleOAuthClient exposes no send-capable method at all, and this file
greps its own module plus mailbox_service.py for gmail.send/messages.send
to catch even an accidental future addition.
"""

import pathlib

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.google.oauth_client import (
    GMAIL_SEND_SCOPE,
    SCOPES,
    GoogleOAuthNotConfiguredError,
    GoogleRefreshTokenInvalidError,
    GoogleTokenExchangeError,
    GoogleTokenRefreshError,
    GoogleTokenRefreshMalformedResponseError,
    GoogleUserinfoError,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mailbox_credential_store import MemoryMailboxCredentialStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services import token_encryption
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

pytestmark = pytest.mark.asyncio


class FakeGoogleOAuthClient:
    """Deliberately has NO method resembling send/messages.send -- only
    what MailboxService actually calls: build_authorize_url, exchange_code,
    fetch_userinfo, refresh_access_token, revoke_token."""

    def __init__(self):
        self.token_response: dict = {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "scope": "openid email profile",
        }
        self.userinfo_response: dict = {"sub": "google-sub-1", "email": "chris@astronomic.io", "name": "Chris Beaman"}
        self.exchange_should_fail = False
        self.userinfo_should_fail = False
        self.revoked_tokens: list[str] = []
        self.exchanged_codes: list[str] = []
        self.requested_scopes: list[tuple[str, ...]] = []

        # refresh_access_token() controls -- see the four refresh-outcome
        # tests below. "success" (default), "invalid_grant", "provider_error",
        # or "malformed".
        self.refresh_outcome = "success"
        self.refresh_response: dict = {"access_token": "fake-refreshed-access-token", "expires_in": 3600}
        self.refreshed_tokens: list[str] = []

    def build_authorize_url(self, state: str, scopes: tuple[str, ...] = ()) -> str:
        # Deliberately keeps "state=" as the LAST query param, with nothing
        # after it -- every test extracts the raw state via
        # `url.split("state=")[1]`; scope tracking lives in
        # `requested_scopes` instead of the URL itself, so it can never
        # interfere with that extraction.
        self.requested_scopes.append(scopes)
        return f"https://accounts.google.com/o/oauth2/v2/auth?scope={'+'.join(scopes)}&state={state}"

    async def exchange_code(self, code: str) -> dict:
        self.exchanged_codes.append(code)
        if self.exchange_should_fail:
            raise GoogleTokenExchangeError("simulated failure")
        return self.token_response

    async def fetch_userinfo(self, access_token: str) -> dict:
        if self.userinfo_should_fail:
            raise GoogleUserinfoError("simulated failure")
        return self.userinfo_response

    async def refresh_access_token(self, refresh_token: str) -> dict:
        self.refreshed_tokens.append(refresh_token)
        if self.refresh_outcome == "invalid_grant":
            raise GoogleRefreshTokenInvalidError("simulated invalid_grant")
        if self.refresh_outcome == "provider_error":
            raise GoogleTokenRefreshError("simulated provider failure")
        if self.refresh_outcome == "malformed":
            raise GoogleTokenRefreshMalformedResponseError("simulated malformed response")
        return self.refresh_response

    async def revoke_token(self, token: str) -> bool:
        self.revoked_tokens.append(token)
        return True


@pytest.fixture(autouse=True)
def configured_encryption_key(monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())


@pytest.fixture
def oauth_client():
    return FakeGoogleOAuthClient()


@pytest.fixture
def service(oauth_client):
    return MailboxService(
        mailbox_store=MemoryMailboxStore(),
        credential_store=MemoryMailboxCredentialStore(),
        oauth_client=oauth_client,
    )


# --- begin_google_oauth / state ---------------------------------------------


async def test_begin_oauth_returns_a_url_containing_a_state(service):
    url = service.begin_google_oauth()

    assert "state=" in url


async def test_begin_oauth_generates_a_different_state_each_call(service):
    url1 = service.begin_google_oauth()
    url2 = service.begin_google_oauth()

    assert url1 != url2


async def test_callback_with_unknown_state_is_rejected(service):
    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state="never-issued", error=None)


async def test_callback_with_no_state_is_rejected(service):
    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state=None, error=None)


async def test_state_is_single_use(service):
    url = service.begin_google_oauth()
    state = url.split("state=")[1]

    await service.handle_google_callback(code="abc", state=state, error=None)

    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state=state, error=None)


async def test_expired_state_is_rejected(service, monkeypatch):
    from datetime import datetime, timedelta, timezone

    url = service.begin_google_oauth()
    state = url.split("state=")[1]

    # Simulate 11 minutes having passed (TTL is 10 minutes).
    service._pending_states[state].created_at = datetime.now(timezone.utc) - timedelta(minutes=11)

    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state=state, error=None)


# --- callback: denial / missing code / exchange & userinfo failure ---------


async def test_callback_with_google_error_param_raises_denied(service):
    state = service.begin_google_oauth().split("state=")[1]

    with pytest.raises(MailboxOAuthDeniedError):
        await service.handle_google_callback(code=None, state=state, error="access_denied")


async def test_callback_denial_error_is_checked_before_missing_code(service):
    """A denial should surface as "denied", not "missing code" -- state is
    consumed and error is checked first."""
    state = service.begin_google_oauth().split("state=")[1]

    try:
        await service.handle_google_callback(code=None, state=state, error="access_denied")
        pytest.fail("expected MailboxOAuthDeniedError")
    except MailboxOAuthDeniedError as e:
        assert e.error == "access_denied"


async def test_callback_with_no_code_and_no_error_raises_missing_code(service):
    state = service.begin_google_oauth().split("state=")[1]

    with pytest.raises(MailboxOAuthMissingCodeError):
        await service.handle_google_callback(code=None, state=state, error=None)


async def test_callback_token_exchange_failure_propagates(service, oauth_client):
    oauth_client.exchange_should_fail = True
    state = service.begin_google_oauth().split("state=")[1]

    with pytest.raises(GoogleTokenExchangeError):
        await service.handle_google_callback(code="abc", state=state, error=None)


async def test_callback_userinfo_failure_propagates(service, oauth_client):
    oauth_client.userinfo_should_fail = True
    state = service.begin_google_oauth().split("state=")[1]

    with pytest.raises(GoogleUserinfoError):
        await service.handle_google_callback(code="abc", state=state, error=None)


async def test_callback_with_no_usable_email_raises_userinfo_error(service, oauth_client):
    oauth_client.userinfo_response = {"sub": "sub-1"}  # no email at all
    state = service.begin_google_oauth().split("state=")[1]

    with pytest.raises(GoogleUserinfoError):
        await service.handle_google_callback(code="abc", state=state, error=None)


# --- successful connection --------------------------------------------------


async def test_successful_callback_creates_a_connected_mailbox(service):
    state = service.begin_google_oauth().split("state=")[1]

    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    assert mailbox.status == MailboxStatus.CONNECTED
    assert mailbox.email == "chris@astronomic.io"
    assert mailbox.display_name == "Chris Beaman"
    assert mailbox.google_user_id == "google-sub-1"
    assert mailbox.granted_scopes == ["email", "openid", "profile"]


async def test_successful_callback_stores_an_encrypted_credential(service):
    state = service.begin_google_oauth().split("state=")[1]

    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    credential = await service.credential_store.get(mailbox.mailbox_id)
    assert credential is not None
    assert credential.encrypted_refresh_token != "fake-refresh-token"
    assert "fake-refresh-token" not in credential.encrypted_refresh_token


async def test_mailbox_appears_in_list_after_connecting(service):
    state = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="abc", state=state, error=None)

    mailboxes = await service.list_mailboxes()

    assert len(mailboxes) == 1


# --- duplicate connection / dedup -------------------------------------------


async def test_reconnecting_the_same_google_account_does_not_duplicate(service):
    state1 = service.begin_google_oauth().split("state=")[1]
    first = await service.handle_google_callback(code="abc", state=state1, error=None)

    state2 = service.begin_google_oauth().split("state=")[1]
    second = await service.handle_google_callback(code="def", state=state2, error=None)

    assert first.mailbox_id == second.mailbox_id
    assert len(await service.list_mailboxes()) == 1


async def test_reconnecting_updates_display_name_and_status(service, oauth_client):
    state1 = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="abc", state=state1, error=None)

    oauth_client.userinfo_response = {"sub": "google-sub-1", "email": "chris@astronomic.io", "name": "Chris B."}
    state2 = service.begin_google_oauth().split("state=")[1]
    updated = await service.handle_google_callback(code="def", state=state2, error=None)

    assert updated.display_name == "Chris B."
    assert updated.status == MailboxStatus.CONNECTED


async def test_reconnecting_a_disconnected_mailbox_marks_it_connected_again(service):
    state1 = service.begin_google_oauth().split("state=")[1]
    first = await service.handle_google_callback(code="abc", state=state1, error=None)
    await service.disconnect_mailbox(first.mailbox_id)

    state2 = service.begin_google_oauth().split("state=")[1]
    reconnected = await service.handle_google_callback(code="def", state=state2, error=None)

    assert reconnected.mailbox_id == first.mailbox_id
    assert reconnected.status == MailboxStatus.CONNECTED
    assert reconnected.disconnected_at is None
    assert len(await service.list_mailboxes()) == 1


async def test_dedup_falls_back_to_email_when_google_user_id_missing(service, oauth_client):
    """Defensive fallback -- google_user_id absent from userinfo shouldn't
    create a second row for the same email."""
    state1 = service.begin_google_oauth().split("state=")[1]
    first = await service.handle_google_callback(code="abc", state=state1, error=None)

    oauth_client.userinfo_response = {"email": "chris@astronomic.io", "name": "Chris Beaman"}  # no "sub"
    state2 = service.begin_google_oauth().split("state=")[1]
    second = await service.handle_google_callback(code="def", state=state2, error=None)

    assert first.mailbox_id == second.mailbox_id
    assert len(await service.list_mailboxes()) == 1


async def test_two_different_google_accounts_produce_two_mailboxes(service, oauth_client):
    state1 = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="abc", state=state1, error=None)

    oauth_client.userinfo_response = {"sub": "google-sub-2", "email": "karla@astronomic.io", "name": "Karla Alvarez"}
    state2 = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="def", state=state2, error=None)

    assert len(await service.list_mailboxes()) == 2


async def test_mailboxes_from_different_workspace_domains_both_connect_independently(service, oauth_client):
    """Nothing in the OAuth client or service restricts a hosted domain
    (no `hd` parameter, no domain allowlist) -- two Google accounts on two
    entirely different Workspace organizations must both connect as
    separate, independent mailboxes with no cross-domain interference."""
    oauth_client.userinfo_response = {
        "sub": "google-sub-victoria",
        "email": "victoria@astronomicconnect.com",
        "name": "Victoria",
    }
    state1 = service.begin_google_oauth().split("state=")[1]
    victoria = await service.handle_google_callback(code="abc", state=state1, error=None)

    oauth_client.userinfo_response = {
        "sub": "google-sub-other-org",
        "email": "someone@a-completely-different-workspace.com",
        "name": "Someone Else",
    }
    state2 = service.begin_google_oauth().split("state=")[1]
    other_org = await service.handle_google_callback(code="def", state=state2, error=None)

    mailboxes = await service.list_mailboxes()
    assert len(mailboxes) == 2
    assert victoria.mailbox_id != other_org.mailbox_id
    assert {m.email for m in mailboxes} == {
        "victoria@astronomicconnect.com",
        "someone@a-completely-different-workspace.com",
    }


# --- disconnect --------------------------------------------------------------


async def test_disconnect_marks_status_disconnected(service):
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    disconnected = await service.disconnect_mailbox(mailbox.mailbox_id)

    assert disconnected.status == MailboxStatus.DISCONNECTED
    assert disconnected.disconnected_at is not None


async def test_disconnect_deletes_the_stored_credential(service):
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    await service.disconnect_mailbox(mailbox.mailbox_id)

    assert await service.credential_store.get(mailbox.mailbox_id) is None


async def test_disconnect_calls_revoke_with_the_decrypted_token(service, oauth_client):
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    await service.disconnect_mailbox(mailbox.mailbox_id)

    assert oauth_client.revoked_tokens == ["fake-refresh-token"]


async def test_disconnect_missing_mailbox_raises(service):
    with pytest.raises(MailboxNotFound):
        await service.disconnect_mailbox("does-not-exist")


async def test_mailbox_still_appears_in_list_after_disconnect(service):
    """Terminal status, not deletion -- matches MailCampaignStatus.ARCHIVED's
    precedent of keeping the row for audit history."""
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    await service.disconnect_mailbox(mailbox.mailbox_id)

    listed = await service.list_mailboxes()
    assert len(listed) == 1
    assert listed[0].status == MailboxStatus.DISCONNECTED


async def test_disconnect_revocation_failure_does_not_block_disconnect(service, oauth_client):
    async def failing_revoke(token):
        raise RuntimeError("Google is down")

    oauth_client.revoke_token = failing_revoke
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)

    disconnected = await service.disconnect_mailbox(mailbox.mailbox_id)

    assert disconnected.status == MailboxStatus.DISCONNECTED
    assert await service.credential_store.get(mailbox.mailbox_id) is None


async def test_disconnect_with_no_stored_credential_is_still_safe(service):
    """A mailbox somehow missing its credential (e.g. re-run of a partial
    connect) must still be disconnectable without error."""
    state = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state, error=None)
    await service.credential_store.delete(mailbox.mailbox_id)

    disconnected = await service.disconnect_mailbox(mailbox.mailbox_id)

    assert disconnected.status == MailboxStatus.DISCONNECTED


# --- not configured ----------------------------------------------------------


async def test_begin_oauth_propagates_not_configured_error():
    from app.google.oauth_client import GoogleOAuthClient

    real_client_but_unconfigured = GoogleOAuthClient()
    service = MailboxService(
        mailbox_store=MemoryMailboxStore(),
        credential_store=MemoryMailboxCredentialStore(),
        oauth_client=real_client_but_unconfigured,
    )

    with pytest.raises(GoogleOAuthNotConfiguredError):
        service.begin_google_oauth()


# --- credential-encryption failure never leaves a "connected" mailbox with --
# --- no stored credential (production incident regression) -----------------
#
# Root cause of the incident this guards against: MAILBOX_TOKEN_ENCRYPTION_KEY
# was set to an invalid value in production. handle_google_callback() used to
# create/save the Mailbox as CONNECTED *before* attempting to encrypt the
# refresh token -- so the encryption failure (raised after that write) left a
# real mailbox row showing "Connected" with no credential ever stored, while
# the user was redirected to an error page. These tests pin the fixed
# ordering: encryption happens first, so a failure here writes nothing.


async def test_encryption_failure_on_new_mailbox_creates_no_row(service, monkeypatch):
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", None)
    state = service.begin_google_oauth().split("state=")[1]

    from app.services.token_encryption import TokenEncryptionNotConfiguredError

    with pytest.raises(TokenEncryptionNotConfiguredError):
        await service.handle_google_callback(code="abc", state=state, error=None)

    assert await service.list_mailboxes() == []


async def test_encryption_failure_on_reconnect_leaves_existing_mailbox_untouched(service, monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    pre_existing = Mailbox(
        mailbox_id="mb-victoria",
        provider=MailboxProvider.GOOGLE,
        email="victoria@astronomicconnect.com",
        display_name="Victoria Bennett",
        status=MailboxStatus.NEEDS_REAUTH,
        google_user_id="google-sub-1",
        connected_at=now,
        updated_at=now,
    )
    await service.mailbox_store.create(pre_existing)

    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", None)
    state = service.begin_google_oauth().split("state=")[1]

    from app.services.token_encryption import TokenEncryptionNotConfiguredError

    with pytest.raises(TokenEncryptionNotConfiguredError):
        await service.handle_google_callback(code="abc", state=state, error=None)

    unchanged = await service.mailbox_store.get("mb-victoria")
    assert unchanged == pre_existing
    assert unchanged.status == MailboxStatus.NEEDS_REAUTH  # never flipped to CONNECTED


async def test_retry_after_fixing_the_key_succeeds_with_no_duplicate_row(service, monkeypatch):
    """The exact recovery path for the production incident: once the key is
    fixed, clicking Connect Email again must update the SAME mailbox (by
    google_user_id) with a real stored credential -- not create a second row."""
    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", None)
    state1 = service.begin_google_oauth().split("state=")[1]
    from app.services.token_encryption import TokenEncryptionNotConfiguredError

    with pytest.raises(TokenEncryptionNotConfiguredError):
        await service.handle_google_callback(code="abc", state=state1, error=None)
    assert await service.list_mailboxes() == []

    from cryptography.fernet import Fernet

    monkeypatch.setattr(token_encryption.settings, "mailbox_token_encryption_key", Fernet.generate_key().decode())
    state2 = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="def", state=state2, error=None)

    all_mailboxes = await service.list_mailboxes()
    assert len(all_mailboxes) == 1
    assert mailbox.status == MailboxStatus.CONNECTED
    credential = await service.credential_store.get(mailbox.mailbox_id)
    assert credential is not None


# =============================================================================
# Phase B1 -- Gmail send scope upgrade + token refresh foundation
# =============================================================================
#
# FlakyMailboxCredentialStore/FlakyMailboxStore below simulate a write
# raising partway through handle_google_callback()'s credential-first,
# mailbox-second write order (see that method's own module-docstring
# explanation of why this order is what makes such a failure safe) --
# proving the PREPARE/COMMIT contract empirically, not just asserting it.


class FlakyMailboxCredentialStore(MemoryMailboxCredentialStore):
    def __init__(self, fail_on: str | None = None):
        super().__init__()
        self.fail_on = fail_on

    async def create(self, credential):
        if self.fail_on == "create":
            raise RuntimeError("simulated credential create failure")
        return await super().create(credential)

    async def save(self, credential):
        if self.fail_on == "save":
            raise RuntimeError("simulated credential save failure")
        return await super().save(credential)


class FlakyMailboxStore(MemoryMailboxStore):
    def __init__(self, fail_on: str | None = None):
        super().__init__()
        self.fail_on = fail_on

    async def create(self, mailbox):
        if self.fail_on == "create":
            raise RuntimeError("simulated mailbox create failure")
        return await super().create(mailbox)

    async def save(self, mailbox):
        if self.fail_on == "save":
            raise RuntimeError("simulated mailbox save failure")
        return await super().save(mailbox)


def _make_connected_mailbox(mailbox_id="mb-victoria", google_user_id="google-sub-victoria", email="victoria@astronomicconnect.com"):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Mailbox(
        mailbox_id=mailbox_id,
        provider=MailboxProvider.GOOGLE,
        email=email,
        display_name="Victoria Bennett",
        status=MailboxStatus.CONNECTED,
        google_user_id=google_user_id,
        granted_scopes=list(SCOPES),
        connected_at=now,
        updated_at=now,
    )


@pytest_asyncio.fixture
async def connected_mailbox_service(oauth_client):
    """A service with ONE pre-existing, connected mailbox (Victoria-shaped)
    already holding a working base-scope credential -- the starting point
    for every upgrade-flow test below."""
    from cryptography.fernet import Fernet as _Fernet

    from app.models.mailbox import MailboxCredential

    mailbox_store = MemoryMailboxStore()
    credential_store = MemoryMailboxCredentialStore()
    service = MailboxService(mailbox_store=mailbox_store, credential_store=credential_store, oauth_client=oauth_client)

    mailbox = _make_connected_mailbox()
    await mailbox_store.create(mailbox)
    encrypted = encrypt_refresh_token_for_test("original-refresh-token")
    await credential_store.create(
        MailboxCredential(mailbox_id=mailbox.mailbox_id, encrypted_refresh_token=encrypted, created_at=mailbox.connected_at, updated_at=mailbox.connected_at)
    )
    return service, mailbox


def encrypt_refresh_token_for_test(raw: str) -> str:
    from app.services.token_encryption import encrypt_refresh_token

    return encrypt_refresh_token(raw)


# --- Ordinary connect continues using base scopes (regression) -------------


async def test_ordinary_connect_requests_only_base_scopes(service, oauth_client):
    service.begin_google_oauth()
    assert oauth_client.requested_scopes == [SCOPES]
    assert GMAIL_SEND_SCOPE not in oauth_client.requested_scopes[0]


# --- Gmail upgrade -- authorize URL -----------------------------------------


async def test_upgrade_requests_exact_desired_scopes(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    await service.begin_gmail_send_upgrade(mailbox.mailbox_id)

    assert oauth_client.requested_scopes == [(*SCOPES, GMAIL_SEND_SCOPE)]


async def test_upgrade_for_unknown_mailbox_raises(service):
    with pytest.raises(MailboxNotFound):
        await service.begin_gmail_send_upgrade("does-not-exist")


async def test_upgrade_authorize_url_uses_the_configured_production_redirect_uri(monkeypatch):
    """Exercises the REAL GoogleOAuthClient (not FakeGoogleOAuthClient,
    which doesn't embed redirect_uri in its stub URL at all -- see that
    class's own docstring), configured with the actual production
    GOOGLE_OAUTH_REDIRECT_URI value, to prove the upgrade flow's
    authorize URL is built from that same setting -- the identical
    code path the ordinary connect flow already uses (build_authorize_url()),
    just with a different scope tuple."""
    from urllib.parse import parse_qs, urlparse

    from app.google import oauth_client as oauth_client_module
    from app.google.oauth_client import GoogleOAuthClient

    monkeypatch.setattr(oauth_client_module.settings, "google_oauth_client_id", "test-client-id")
    monkeypatch.setattr(oauth_client_module.settings, "google_oauth_client_secret", "test-client-secret")
    monkeypatch.setattr(
        oauth_client_module.settings,
        "google_oauth_redirect_uri",
        "https://api.astronomicconnect.com/mailboxes/google/callback",
    )

    mailbox_store = MemoryMailboxStore()
    await mailbox_store.create(_make_connected_mailbox())
    service = MailboxService(
        mailbox_store=mailbox_store, credential_store=MemoryMailboxCredentialStore(), oauth_client=GoogleOAuthClient()
    )

    url = await service.begin_gmail_send_upgrade("mb-victoria")

    redirect_uri = parse_qs(urlparse(url).query)["redirect_uri"][0]
    assert redirect_uri == "https://api.astronomicconnect.com/mailboxes/google/callback"


# --- Gmail upgrade -- state remains opaque/single-use/expiring -------------


async def test_upgrade_state_is_single_use(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {"access_token": "fake-access-token", "refresh_token": "new-refresh-token", "scope": "openid email profile https://www.googleapis.com/auth/gmail.send"}

    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    await service.handle_google_callback(code="abc", state=state, error=None)

    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state=state, error=None)


async def test_upgrade_expired_state_is_rejected(connected_mailbox_service):
    from datetime import datetime, timedelta, timezone

    service, mailbox = connected_mailbox_service
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    service._pending_states[state].created_at = datetime.now(timezone.utc) - timedelta(minutes=11)

    with pytest.raises(MailboxOAuthStateError):
        await service.handle_google_callback(code="abc", state=state, error=None)


# --- Gmail upgrade -- account binding ---------------------------------------


async def test_upgrade_succeeds_when_same_google_account_authorizes(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    updated = await service.handle_google_callback(code="abc", state=state, error=None)

    assert updated.mailbox_id == mailbox.mailbox_id
    assert GMAIL_SEND_SCOPE in updated.granted_scopes
    assert len(await service.list_mailboxes()) == 1  # no duplicate mailbox


async def test_upgrade_with_wrong_google_account_is_rejected_with_zero_mutation(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    # A DIFFERENT Google account completes the consent screen.
    oauth_client.userinfo_response = {"sub": "google-sub-someone-else", "email": "someone-else@example.com", "name": "Someone Else"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(MailboxOAuthAccountMismatchError) as excinfo:
        await service.handle_google_callback(code="abc", state=state, error=None)
    assert excinfo.value.mailbox_id == mailbox.mailbox_id
    assert excinfo.value.expected_google_user_id == mailbox.google_user_id
    assert excinfo.value.actual_google_user_id == "google-sub-someone-else"

    # Zero mutation: target mailbox unchanged, no second mailbox created,
    # target's credential unchanged.
    unchanged = await service.mailbox_store.get(mailbox.mailbox_id)
    assert unchanged == mailbox
    assert len(await service.list_mailboxes()) == 1
    credential = await service.credential_store.get(mailbox.mailbox_id)
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "original-refresh-token"
    assert credential.previous_encrypted_refresh_token is None, "a failure BEFORE PREPARE must never even touch this field"


# --- Gmail upgrade -- scope truth --------------------------------------------


async def test_upgrade_without_gmail_send_in_granted_scopes_preserves_prior_state(connected_mailbox_service, oauth_client):
    """Google callback succeeds (right account, code exchanges fine), but
    the token response's `scope` field does NOT include gmail.send (e.g.
    the consent screen didn't actually present/grant it). The upgrade must
    be treated as unsuccessful and the mailbox's prior state preserved --
    granted_scopes must never claim more than Google actually granted."""
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile",  # gmail.send silently absent
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(MailboxOAuthScopeNotGrantedError) as excinfo:
        await service.handle_google_callback(code="abc", state=state, error=None)
    assert excinfo.value.required_scope == GMAIL_SEND_SCOPE

    unchanged = await service.mailbox_store.get(mailbox.mailbox_id)
    assert unchanged == mailbox
    assert GMAIL_SEND_SCOPE not in unchanged.granted_scopes
    credential = await service.credential_store.get(mailbox.mailbox_id)
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "original-refresh-token"
    assert credential.previous_encrypted_refresh_token is None, "a failure BEFORE PREPARE must never even touch this field"


async def test_upgrade_missing_refresh_token_does_not_destroy_existing_credential(connected_mailbox_service, oauth_client):
    """gmail.send IS present in this exchange's granted scope, but Google
    issued no new refresh_token -- treated as a failed upgrade (stricter
    than the ordinary reconnect flow's "keep the old one" leniency, since
    a refresh token minted under the OLD scope grant can't be assumed to
    back the NEW scope later)."""
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        # no refresh_token key at all
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(MailboxOAuthUpgradeMissingRefreshTokenError):
        await service.handle_google_callback(code="abc", state=state, error=None)

    unchanged = await service.mailbox_store.get(mailbox.mailbox_id)
    assert unchanged == mailbox
    assert GMAIL_SEND_SCOPE not in unchanged.granted_scopes
    credential = await service.credential_store.get(mailbox.mailbox_id)
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "original-refresh-token"
    assert credential.previous_encrypted_refresh_token is None, "a failure BEFORE PREPARE must never even touch this field"


async def test_upgrade_cancel_leaves_old_mailbox_and_credential_intact(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(MailboxOAuthDeniedError):
        await service.handle_google_callback(code=None, state=state, error="access_denied")

    unchanged = await service.mailbox_store.get(mailbox.mailbox_id)
    assert unchanged == mailbox
    credential = await service.credential_store.get(mailbox.mailbox_id)
    assert credential is not None
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "original-refresh-token"
    assert credential.previous_encrypted_refresh_token is None, "a failure BEFORE PREPARE must never even touch this field"


# --- Gmail upgrade -- fault injection across write boundaries ---------------


async def test_upgrade_credential_write_failure_leaves_mailbox_completely_untouched():
    """Credential is written FIRST -- if THAT write fails, the public
    Mailbox row must never be touched at all (old granted_scopes, old
    status, exactly as before)."""
    oauth_client = FakeGoogleOAuthClient()
    mailbox_store = MemoryMailboxStore()
    credential_store = FlakyMailboxCredentialStore(fail_on="save")
    service = MailboxService(mailbox_store=mailbox_store, credential_store=credential_store, oauth_client=oauth_client)

    mailbox = _make_connected_mailbox()
    await mailbox_store.create(mailbox)
    from app.models.mailbox import MailboxCredential

    await MemoryMailboxCredentialStore.create(
        credential_store, MailboxCredential(mailbox_id=mailbox.mailbox_id, encrypted_refresh_token=encrypt_refresh_token_for_test("original-refresh-token"), created_at=mailbox.connected_at, updated_at=mailbox.connected_at)
    )

    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(RuntimeError, match="simulated credential save failure"):
        await service.handle_google_callback(code="abc", state=state, error=None)

    unchanged = await mailbox_store.get(mailbox.mailbox_id)
    assert unchanged == mailbox
    assert GMAIL_SEND_SCOPE not in unchanged.granted_scopes, "mailbox must NEVER claim gmail.send when the credential write never landed"


async def test_upgrade_mailbox_write_failure_leaves_credential_upgraded_but_mailbox_underreporting():
    """The safe failure direction: credential write (FIRST) succeeds, then
    the mailbox write (SECOND) fails. The credential now holds the NEW
    refresh token, but the public Mailbox row still shows the OLD
    granted_scopes -- UNDER-reporting capability, never over-claiming it.
    This is the exact invariant a production audit required."""
    oauth_client = FakeGoogleOAuthClient()
    mailbox_store = FlakyMailboxStore(fail_on="save")
    credential_store = MemoryMailboxCredentialStore()
    service = MailboxService(mailbox_store=mailbox_store, credential_store=credential_store, oauth_client=oauth_client)

    mailbox = _make_connected_mailbox()
    await MemoryMailboxStore.create(mailbox_store, mailbox)
    from app.models.mailbox import MailboxCredential

    await credential_store.create(
        MailboxCredential(mailbox_id=mailbox.mailbox_id, encrypted_refresh_token=encrypt_refresh_token_for_test("original-refresh-token"), created_at=mailbox.connected_at, updated_at=mailbox.connected_at)
    )

    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(RuntimeError, match="simulated mailbox save failure"):
        await service.handle_google_callback(code="abc", state=state, error=None)

    # Credential IS upgraded (the "risky" write landed)...
    credential = await credential_store.get(mailbox.mailbox_id)
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "new-refresh-token"
    # ...but the public row still under-reports -- safe direction, never
    # claims a capability the (at-this-exact-instant, mid-failure) visible
    # state hasn't confirmed end-to-end.
    still_old = await MemoryMailboxStore.get(mailbox_store, mailbox.mailbox_id)
    assert GMAIL_SEND_SCOPE not in still_old.granted_scopes
    # AND (the durability correction): the PREVIOUSLY-WORKING credential is
    # not destroyed -- it is preserved, decryptable, recoverable, even
    # though the active slot now holds the new one.
    assert credential.previous_encrypted_refresh_token is not None
    assert decrypt_refresh_token(credential.previous_encrypted_refresh_token) == "original-refresh-token"


async def test_successful_upgrade_still_preserves_the_previous_credential_for_recovery(connected_mailbox_service, oauth_client):
    """A FULLY successful upgrade (no fault injected at all) still leaves
    the previous credential recoverable in `previous_encrypted_refresh_
    token` -- this field is deliberately never cleared on success either
    (see MailboxCredential's own docstring for why: avoiding a third write
    purely for tidiness), so a human/repair path always has it available,
    not only in the crash-window case."""
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": mailbox.google_user_id, "email": mailbox.email, "name": "Victoria Bennett"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    updated = await service.handle_google_callback(code="abc", state=state, error=None)
    assert GMAIL_SEND_SCOPE in updated.granted_scopes

    credential = await service.credential_store.get(mailbox.mailbox_id)
    from app.services.token_encryption import decrypt_refresh_token

    assert decrypt_refresh_token(credential.encrypted_refresh_token) == "new-refresh-token"
    assert decrypt_refresh_token(credential.previous_encrypted_refresh_token) == "original-refresh-token"


async def test_repeated_ordinary_reconnects_rotate_previous_credential_without_accumulating_history(service, oauth_client):
    """Three ordinary reconnects in a row, each issuing a genuinely new
    refresh token -- `previous_encrypted_refresh_token` must always hold
    exactly the ONE generation immediately prior, never a growing chain
    and never a stale value from two reconnects ago."""
    from app.services.token_encryption import decrypt_refresh_token

    oauth_client.token_response = {"access_token": "fake-access-token", "refresh_token": "token-1", "scope": "openid email profile"}
    state1 = service.begin_google_oauth().split("state=")[1]
    mailbox = await service.handle_google_callback(code="abc", state=state1, error=None)

    credential_after_1 = await service.credential_store.get(mailbox.mailbox_id)
    assert decrypt_refresh_token(credential_after_1.encrypted_refresh_token) == "token-1"
    assert credential_after_1.previous_encrypted_refresh_token is None  # brand-new mailbox -- nothing to preserve yet

    oauth_client.token_response = {"access_token": "fake-access-token", "refresh_token": "token-2", "scope": "openid email profile"}
    state2 = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="def", state=state2, error=None)

    credential_after_2 = await service.credential_store.get(mailbox.mailbox_id)
    assert decrypt_refresh_token(credential_after_2.encrypted_refresh_token) == "token-2"
    assert decrypt_refresh_token(credential_after_2.previous_encrypted_refresh_token) == "token-1"

    oauth_client.token_response = {"access_token": "fake-access-token", "refresh_token": "token-3", "scope": "openid email profile"}
    state3 = service.begin_google_oauth().split("state=")[1]
    await service.handle_google_callback(code="ghi", state=state3, error=None)

    credential_after_3 = await service.credential_store.get(mailbox.mailbox_id)
    assert decrypt_refresh_token(credential_after_3.encrypted_refresh_token) == "token-3"
    # Rotated to token-2 -- NOT token-1, and not any multi-entry history.
    assert decrypt_refresh_token(credential_after_3.previous_encrypted_refresh_token) == "token-2"


# --- Access-token refresh ----------------------------------------------------


async def test_refresh_success_returns_access_token(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "success"
    oauth_client.refresh_response = {"access_token": "fresh-access-token", "expires_in": 3600}

    token = await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    assert token == "fresh-access-token"
    assert oauth_client.refreshed_tokens == ["original-refresh-token"]


async def test_refresh_invalid_grant_propagates(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "invalid_grant"

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)


async def test_refresh_ordinary_provider_failure_propagates(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "provider_error"

    with pytest.raises(GoogleTokenRefreshError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)


async def test_refresh_malformed_response_propagates(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "malformed"

    with pytest.raises(GoogleTokenRefreshMalformedResponseError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)


async def test_refresh_for_mailbox_with_no_credential_raises(service):
    mailbox = _make_connected_mailbox()
    await service.mailbox_store.create(mailbox)  # no credential ever created

    with pytest.raises(MailboxCredentialMissingError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)


async def test_refresh_for_unknown_mailbox_raises(service):
    with pytest.raises(MailboxNotFound):
        await service.refresh_mailbox_access_token("does-not-exist")


# --- NEEDS_REAUTH: reachable ONLY from a confirmed invalid_grant ------------


async def test_invalid_grant_moves_mailbox_to_needs_reauth(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "invalid_grant"

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    updated = await service.mailbox_store.get(mailbox.mailbox_id)
    assert updated.status == MailboxStatus.NEEDS_REAUTH


@pytest.mark.parametrize("outcome", ["provider_error", "malformed"])
async def test_other_refresh_failures_never_move_to_needs_reauth(connected_mailbox_service, oauth_client, outcome):
    """429/5xx-shaped provider failures and malformed responses must NEVER
    be treated as proof the grant itself is broken -- only invalid_grant
    is. (Gmail-send-specific errors like domainPolicy/429/UNKNOWN don't
    exist as inputs to this method at all yet -- no sender exists in
    Phase B1 -- so they cannot be confused with invalid_grant by
    construction, not merely by convention.)"""
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = outcome

    with pytest.raises(GoogleTokenRefreshError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    unchanged = await service.mailbox_store.get(mailbox.mailbox_id)
    assert unchanged.status == MailboxStatus.CONNECTED


async def test_invalid_grant_logs_a_needs_reauth_activity_event():
    """Phase C: when an activity_log is wired in, an invalid_grant must
    produce a structural mailbox.needs_reauth event -- required so the
    Phase C worker's recovery sweep and any future ops dashboard has a
    durable record of every mailbox that dropped out. Must be skippable
    (no activity_log) without breaking anything -- see the other test
    below."""
    from app.models.mailbox import MailboxCredential
    from app.repositories.activity_event_store import MemoryActivityEventStore
    from app.services.activity_log_service import ActivityLogService

    oauth_client = FakeGoogleOAuthClient()
    mailbox_store = MemoryMailboxStore()
    credential_store = MemoryMailboxCredentialStore()
    activity_log = ActivityLogService(MemoryActivityEventStore())
    service = MailboxService(
        mailbox_store=mailbox_store, credential_store=credential_store, oauth_client=oauth_client, activity_log=activity_log
    )

    mailbox = _make_connected_mailbox()
    await mailbox_store.create(mailbox)
    encrypted = encrypt_refresh_token_for_test("original-refresh-token")
    await credential_store.create(
        MailboxCredential(mailbox_id=mailbox.mailbox_id, encrypted_refresh_token=encrypted, created_at=mailbox.connected_at, updated_at=mailbox.connected_at)
    )
    oauth_client.refresh_outcome = "invalid_grant"

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    events = await activity_log.store.list()
    matching = [e for e in events if e.event_type == "mailbox.needs_reauth"]
    assert len(matching) == 1
    assert matching[0].entity_id == mailbox.mailbox_id
    assert matching[0].entity_name == mailbox.email
    # No token material of any kind ever reaches the summary.
    assert "original-refresh-token" not in matching[0].summary


async def test_invalid_grant_without_activity_log_still_moves_to_needs_reauth(connected_mailbox_service, oauth_client):
    """activity_log stays fully optional -- every pre-Phase-C call site
    that never passes it (e.g. the module-level `service` fixture, and
    connected_mailbox_service itself) must keep working exactly as
    before."""
    service, mailbox = connected_mailbox_service
    assert service.activity_log is None
    oauth_client.refresh_outcome = "invalid_grant"

    with pytest.raises(GoogleRefreshTokenInvalidError):
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    updated = await service.mailbox_store.get(mailbox.mailbox_id)
    assert updated.status == MailboxStatus.NEEDS_REAUTH


# --- No secrets in logs/errors ------------------------------------------------


async def test_refresh_failure_exceptions_never_contain_the_refresh_token(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.refresh_outcome = "invalid_grant"

    with pytest.raises(GoogleRefreshTokenInvalidError) as excinfo:
        await service.refresh_mailbox_access_token(mailbox.mailbox_id)

    assert "original-refresh-token" not in str(excinfo.value)


async def test_upgrade_mismatch_error_never_contains_tokens_or_secrets(connected_mailbox_service, oauth_client):
    service, mailbox = connected_mailbox_service
    oauth_client.userinfo_response = {"sub": "someone-else", "email": "someone-else@example.com", "name": "Someone Else"}
    oauth_client.token_response = {
        "access_token": "fake-access-token",
        "refresh_token": "new-refresh-token",
        "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
    }
    url = await service.begin_gmail_send_upgrade(mailbox.mailbox_id)
    state = url.split("state=")[1]

    with pytest.raises(MailboxOAuthAccountMismatchError) as excinfo:
        await service.handle_google_callback(code="abc", state=state, error=None)

    message = str(excinfo.value)
    assert "new-refresh-token" not in message
    assert "fake-access-token" not in message


def test_oauth_client_source_never_logs_refresh_or_access_tokens():
    """Static proof, mirroring test_mailbox_sending_safety.py's own
    convention: scans app/google/oauth_client.py's actual source for any
    logger call that could plausibly interpolate a raw token variable."""
    import re

    source = pathlib.Path("app/google/oauth_client.py").read_text()
    logger_calls = re.findall(r"logger\.\w+\(([^)]*)\)", source)
    for call in logger_calls:
        assert "access_token" not in call
        assert "refresh_token" not in call
        assert "client_secret" not in call
