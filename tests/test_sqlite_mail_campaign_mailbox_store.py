"""
Direct SQLite-layer tests for MailCampaignMailboxStore -- same tmp_path-
backed SQLite file convention as test_sqlite_mailbox_stores.py. Proves the
Channels join table round-trips, de-duplicates, and (most importantly)
that replace_for_campaign() is atomic -- a failure partway through leaves
the previous selection completely intact.
"""

import aiosqlite
import pytest
import pytest_asyncio

from app.repositories.sqlite_mail_campaign_mailbox_store import SQLiteMailCampaignMailboxStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteMailCampaignMailboxStore(str(tmp_path / "channels.db"))
    await s.connect()
    yield s
    await s.close()


async def test_list_starts_empty(store):
    assert await store.list_mailbox_ids_for_campaign("c1") == []


async def test_replace_sets_the_selection(store):
    await store.replace_for_campaign("c1", ["mbx-a", "mbx-b"])
    assert set(await store.list_mailbox_ids_for_campaign("c1")) == {"mbx-a", "mbx-b"}


async def test_replace_deduplicates_input(store):
    await store.replace_for_campaign("c1", ["mbx-a", "mbx-a", "mbx-a"])
    assert await store.list_mailbox_ids_for_campaign("c1") == ["mbx-a"]


async def test_replace_is_a_full_replace_not_an_incremental_add(store):
    await store.replace_for_campaign("c1", ["mbx-a", "mbx-b"])
    await store.replace_for_campaign("c1", ["mbx-b", "mbx-c"])
    assert set(await store.list_mailbox_ids_for_campaign("c1")) == {"mbx-b", "mbx-c"}


async def test_replace_never_touches_other_campaigns(store):
    await store.replace_for_campaign("c1", ["mbx-a"])
    await store.replace_for_campaign("c2", ["mbx-b"])
    await store.replace_for_campaign("c1", [])
    assert await store.list_mailbox_ids_for_campaign("c1") == []
    assert await store.list_mailbox_ids_for_campaign("c2") == ["mbx-b"]


async def test_replace_preserves_added_at_for_surviving_mailboxes(store):
    """A mailbox that stays selected across two saves keeps its ORIGINAL
    added_at rather than getting bumped to "now" on every save -- real
    history, not a last-saved timestamp."""
    await store.replace_for_campaign("c1", ["mbx-a"])
    cursor = await store._connection.execute(
        "SELECT added_at FROM mail_campaign_mailboxes WHERE mail_campaign_id = ? AND mailbox_id = ?", ("c1", "mbx-a")
    )
    first_added_at = (await cursor.fetchone())["added_at"]
    await cursor.close()

    await store.replace_for_campaign("c1", ["mbx-a", "mbx-b"])
    cursor = await store._connection.execute(
        "SELECT added_at FROM mail_campaign_mailboxes WHERE mail_campaign_id = ? AND mailbox_id = ?", ("c1", "mbx-a")
    )
    second_added_at = (await cursor.fetchone())["added_at"]
    await cursor.close()

    assert first_added_at == second_added_at


async def test_replace_is_atomic_on_failure(store, monkeypatch):
    """A forced failure partway through replace_for_campaign() (after the
    DELETE but before the INSERT completes) must leave the PREVIOUS
    selection completely intact, not a half-replaced one -- see
    sqlite_txn.sqlite_write's rollback guarantee."""
    await store.replace_for_campaign("c1", ["mbx-a", "mbx-b"])

    original_executemany = store._connection.executemany

    async def failing_executemany(*args, **kwargs):
        raise aiosqlite.OperationalError("simulated failure")

    monkeypatch.setattr(store._connection, "executemany", failing_executemany)

    with pytest.raises(aiosqlite.OperationalError):
        await store.replace_for_campaign("c1", ["mbx-c"])

    monkeypatch.setattr(store._connection, "executemany", original_executemany)
    assert set(await store.list_mailbox_ids_for_campaign("c1")) == {"mbx-a", "mbx-b"}


async def test_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "channels.db")
    store1 = SQLiteMailCampaignMailboxStore(db_path)
    await store1.connect()
    await store1.replace_for_campaign("c1", ["mbx-a"])
    await store1.close()

    store2 = SQLiteMailCampaignMailboxStore(db_path)
    await store2.connect()
    assert await store2.list_mailbox_ids_for_campaign("c1") == ["mbx-a"]
    await store2.close()
