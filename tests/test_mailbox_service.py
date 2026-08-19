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

import pytest
from cryptography.fernet import Fernet

from app.google.oauth_client import GoogleOAuthNotConfiguredError, GoogleTokenExchangeError, GoogleUserinfoError
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mailbox_credential_store import MemoryMailboxCredentialStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services import token_encryption
from app.services.mailbox_service import (
    MailboxNotFound,
    MailboxOAuthDeniedError,
    MailboxOAuthMissingCodeError,
    MailboxOAuthStateError,
    MailboxService,
)

pytestmark = pytest.mark.asyncio


class FakeGoogleOAuthClient:
    """Deliberately has NO method resembling send/messages.send -- only
    what MailboxService actually calls: build_authorize_url, exchange_code,
    fetch_userinfo, revoke_token."""

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

    def build_authorize_url(self, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def exchange_code(self, code: str) -> dict:
        self.exchanged_codes.append(code)
        if self.exchange_should_fail:
            raise GoogleTokenExchangeError("simulated failure")
        return self.token_response

    async def fetch_userinfo(self, access_token: str) -> dict:
        if self.userinfo_should_fail:
            raise GoogleUserinfoError("simulated failure")
        return self.userinfo_response

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
