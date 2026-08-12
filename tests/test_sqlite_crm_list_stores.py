"""
Persistence tests for SQLiteCrmContactListStore/SQLiteCrmContactListMemberStore --
same contract the Memory variants satisfy, plus idempotent membership add/remove
actually enforced by SQLite (INSERT OR IGNORE / composite PK), cascade delete of
memberships, and data surviving a fresh connection to the same file.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmContactList, CrmContactListMembership
from app.repositories.sqlite_crm_contact_list_member_store import SQLiteCrmContactListMemberStore
from app.repositories.sqlite_crm_contact_list_store import SQLiteCrmContactListStore


def make_list(list_id: str, **overrides) -> CrmContactList:
    now = datetime.now(timezone.utc)
    defaults = dict(list_id=list_id, name="Austin Family Offices", created_at=now, updated_at=now)
    defaults.update(overrides)
    return CrmContactList(**defaults)


def make_membership(list_id: str, crm_contact_id: str) -> CrmContactListMembership:
    return CrmContactListMembership(list_id=list_id, crm_contact_id=crm_contact_id, added_at=datetime.now(timezone.utc))


@pytest_asyncio.fixture
async def list_store(tmp_path):
    store = SQLiteCrmContactListStore(str(tmp_path / "crm_contact_lists.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def member_store(tmp_path):
    store = SQLiteCrmContactListMemberStore(str(tmp_path / "crm_contact_list_members.db"))
    await store.connect()
    yield store
    await store.close()


# --- CrmContactListStore ---


@pytest.mark.asyncio
async def test_list_create_and_get_roundtrip(list_store):
    await list_store.create(make_list("l1", name="Austin Family Offices", description="Prospecting"))
    fetched = await list_store.get("l1")
    assert fetched.name == "Austin Family Offices"
    assert fetched.description == "Prospecting"


@pytest.mark.asyncio
async def test_list_get_missing_returns_none(list_store):
    assert await list_store.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_list_duplicate_id_rejected(list_store):
    await list_store.create(make_list("l1"))
    with pytest.raises(ValueError):
        await list_store.create(make_list("l1"))


@pytest.mark.asyncio
async def test_list_name_uniqueness_not_enforced(list_store):
    """No unique constraint on name -- two lists may share a name."""
    await list_store.create(make_list("l1", name="Same Name"))
    await list_store.create(make_list("l2", name="Same Name"))  # must not raise


@pytest.mark.asyncio
async def test_list_save_persists_rename(list_store):
    await list_store.create(make_list("l1", name="Old Name"))
    contact_list = await list_store.get("l1")
    contact_list.name = "New Name"
    await list_store.save(contact_list)
    assert (await list_store.get("l1")).name == "New Name"


@pytest.mark.asyncio
async def test_list_delete_removes_row(list_store):
    await list_store.create(make_list("l1"))
    await list_store.delete("l1")
    assert await list_store.get("l1") is None


@pytest.mark.asyncio
async def test_list_delete_missing_is_a_noop(list_store):
    await list_store.delete("does-not-exist")  # must not raise


@pytest.mark.asyncio
async def test_list_list_orders_by_created_at(list_store):
    await list_store.create(make_list("l1"))
    await list_store.create(make_list("l2"))
    lists = await list_store.list()
    assert [l.list_id for l in lists] == ["l1", "l2"]


@pytest.mark.asyncio
async def test_list_survives_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist.db")
    first = SQLiteCrmContactListStore(db_path)
    await first.connect()
    await first.create(make_list("l1", name="Persisted List"))
    await first.close()

    second = SQLiteCrmContactListStore(db_path)
    await second.connect()
    fetched = await second.get("l1")
    await second.close()

    assert fetched is not None
    assert fetched.name == "Persisted List"


# --- CrmContactListMemberStore ---


@pytest.mark.asyncio
async def test_member_add_is_new_returns_true(member_store):
    is_new = await member_store.add(make_membership("l1", "c1"))
    assert is_new is True


@pytest.mark.asyncio
async def test_member_add_duplicate_is_idempotent(member_store):
    await member_store.add(make_membership("l1", "c1"))
    is_new = await member_store.add(make_membership("l1", "c1"))
    assert is_new is False
    assert await member_store.list_contact_ids_for_list("l1") == ["c1"]


@pytest.mark.asyncio
async def test_member_same_contact_multiple_lists(member_store):
    await member_store.add(make_membership("l1", "c1"))
    await member_store.add(make_membership("l2", "c1"))
    assert set(await member_store.list_ids_for_contact("c1")) == {"l1", "l2"}


@pytest.mark.asyncio
async def test_member_remove_existing_returns_true(member_store):
    await member_store.add(make_membership("l1", "c1"))
    removed = await member_store.remove("l1", "c1")
    assert removed is True
    assert await member_store.list_contact_ids_for_list("l1") == []


@pytest.mark.asyncio
async def test_member_remove_missing_returns_false(member_store):
    removed = await member_store.remove("l1", "does-not-exist")
    assert removed is False


@pytest.mark.asyncio
async def test_member_remove_all_for_list_only_touches_that_list(member_store):
    await member_store.add(make_membership("l1", "c1"))
    await member_store.add(make_membership("l1", "c2"))
    await member_store.add(make_membership("l2", "c1"))

    await member_store.remove_all_for_list("l1")

    assert await member_store.list_contact_ids_for_list("l1") == []
    assert await member_store.list_contact_ids_for_list("l2") == ["c1"]


@pytest.mark.asyncio
async def test_member_count_by_list(member_store):
    await member_store.add(make_membership("l1", "c1"))
    await member_store.add(make_membership("l1", "c2"))
    await member_store.add(make_membership("l2", "c1"))

    counts = await member_store.count_by_list()
    assert counts == {"l1": 2, "l2": 1}


@pytest.mark.asyncio
async def test_member_survives_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist.db")
    first = SQLiteCrmContactListMemberStore(db_path)
    await first.connect()
    await first.add(make_membership("l1", "c1"))
    await first.close()

    second = SQLiteCrmContactListMemberStore(db_path)
    await second.connect()
    ids = await second.list_contact_ids_for_list("l1")
    await second.close()

    assert ids == ["c1"]
