"""
Direct SQLite-layer tests for Mailbox/MailboxCredential persistence --
same tmp_path-backed SQLite file convention as test_sqlite_mail_stores.py.
Proves connected mailboxes and their encrypted credentials survive a
close()/reconnect() cycle (i.e. a Railway redeploy), and that
get_by_google_user_id/get_by_email correctly dedupe.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mailbox import Mailbox, MailboxCredential, MailboxProvider, MailboxStatus
from app.repositories.mailbox_store import MailboxNotFoundError
from app.repositories.sqlite_mailbox_credential_store import SQLiteMailboxCredentialStore
from app.repositories.sqlite_mailbox_store import SQLiteMailboxStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def mailbox_store(tmp_path):
    store = SQLiteMailboxStore(str(tmp_path / "mailboxes.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def credential_store(tmp_path):
    store = SQLiteMailboxCredentialStore(str(tmp_path / "mailboxes.db"))
    await store.connect()
    yield store
    await store.close()


def _now():
    return datetime.now(timezone.utc)


def make_mailbox(mailbox_id: str, email: str, google_user_id: str | None = "google-sub-1") -> Mailbox:
    now = _now()
    return Mailbox(
        mailbox_id=mailbox_id,
        provider=MailboxProvider.GOOGLE,
        email=email,
        display_name="Chris Beaman",
        status=MailboxStatus.CONNECTED,
        google_user_id=google_user_id,
        connected_at=now,
        updated_at=now,
    )


async def test_create_and_get_round_trip(mailbox_store):
    mailbox = make_mailbox("mb-1", "chris@astronomic.io")
    await mailbox_store.create(mailbox)

    fetched = await mailbox_store.get("mb-1")

    assert fetched == mailbox


async def test_get_missing_returns_none(mailbox_store):
    assert await mailbox_store.get("does-not-exist") is None


async def test_get_by_google_user_id(mailbox_store):
    await mailbox_store.create(make_mailbox("mb-1", "chris@astronomic.io", google_user_id="sub-abc"))
    await mailbox_store.create(make_mailbox("mb-2", "karla@astronomic.io", google_user_id="sub-xyz"))

    found = await mailbox_store.get_by_google_user_id("sub-abc")

    assert found is not None
    assert found.mailbox_id == "mb-1"


async def test_get_by_email(mailbox_store):
    await mailbox_store.create(make_mailbox("mb-1", "chris@astronomic.io"))

    found = await mailbox_store.get_by_email("chris@astronomic.io")

    assert found is not None
    assert found.mailbox_id == "mb-1"


async def test_get_by_google_user_id_no_match_returns_none(mailbox_store):
    await mailbox_store.create(make_mailbox("mb-1", "chris@astronomic.io"))

    assert await mailbox_store.get_by_google_user_id("nonexistent") is None


async def test_save_updates_existing_row(mailbox_store):
    mailbox = make_mailbox("mb-1", "chris@astronomic.io")
    await mailbox_store.create(mailbox)

    disconnected = mailbox.model_copy(update={"status": MailboxStatus.DISCONNECTED})
    await mailbox_store.save(disconnected)

    fetched = await mailbox_store.get("mb-1")
    assert fetched.status == MailboxStatus.DISCONNECTED


async def test_save_missing_mailbox_raises(mailbox_store):
    with pytest.raises(MailboxNotFoundError):
        await mailbox_store.save(make_mailbox("does-not-exist", "nobody@astronomic.io"))


async def test_list_sorted_by_connected_at(mailbox_store):
    now = _now()
    older = make_mailbox("mb-old", "old@astronomic.io").model_copy(update={"connected_at": now})
    newer = make_mailbox("mb-new", "new@astronomic.io").model_copy(
        update={"connected_at": now.replace(year=now.year + 1)}
    )
    await mailbox_store.create(older)
    await mailbox_store.create(newer)

    listed = await mailbox_store.list()

    assert [m.mailbox_id for m in listed] == ["mb-old", "mb-new"]


async def test_mailbox_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "mailboxes.db")
    store = SQLiteMailboxStore(db_path)
    await store.connect()
    await store.create(make_mailbox("mb-1", "chris@astronomic.io"))
    await store.close()

    store2 = SQLiteMailboxStore(db_path)
    await store2.connect()
    fetched = await store2.get("mb-1")
    await store2.close()

    assert fetched is not None
    assert fetched.email == "chris@astronomic.io"


async def test_credential_create_get_and_never_plaintext_by_construction(credential_store):
    now = _now()
    credential = MailboxCredential(
        mailbox_id="mb-1",
        encrypted_refresh_token="fernet-ciphertext-not-plaintext",
        created_at=now,
        updated_at=now,
    )
    await credential_store.create(credential)

    fetched = await credential_store.get("mb-1")

    assert fetched == credential
    assert fetched.encrypted_refresh_token == "fernet-ciphertext-not-plaintext"


async def test_credential_delete_removes_it(credential_store):
    now = _now()
    await credential_store.create(
        MailboxCredential(mailbox_id="mb-1", encrypted_refresh_token="ciphertext", created_at=now, updated_at=now)
    )

    await credential_store.delete("mb-1")

    assert await credential_store.get("mb-1") is None


async def test_credential_delete_missing_is_a_safe_noop(credential_store):
    await credential_store.delete("never-existed")  # must not raise


async def test_credential_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "mailboxes.db")
    now = _now()
    store = SQLiteMailboxCredentialStore(db_path)
    await store.connect()
    await store.create(MailboxCredential(mailbox_id="mb-1", encrypted_refresh_token="ciphertext", created_at=now, updated_at=now))
    await store.close()

    store2 = SQLiteMailboxCredentialStore(db_path)
    await store2.connect()
    fetched = await store2.get("mb-1")
    await store2.close()

    assert fetched is not None
    assert fetched.encrypted_refresh_token == "ciphertext"
