"""
Persistence tests for SQLiteCrmContactStore/SQLiteCrmCustomFieldStore/
SQLiteCrmImportBatchStore -- same contract the Memory variants satisfy,
plus the UNIQUE constraints being enforced by SQLite itself, and data
surviving a fresh connection to the same file.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmContact, CrmCustomFieldDefinition, CrmImportBatch, CustomFieldType
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_crm_custom_field_store import SQLiteCrmCustomFieldStore
from app.repositories.sqlite_crm_import_batch_store import SQLiteCrmImportBatchStore


def make_contact(crm_contact_id: str, **overrides) -> CrmContact:
    now = datetime.now(timezone.utc)
    defaults = dict(crm_contact_id=crm_contact_id, created_at=now, updated_at=now)
    defaults.update(overrides)
    return CrmContact(**defaults)


@pytest_asyncio.fixture
async def contact_store(tmp_path):
    store = SQLiteCrmContactStore(str(tmp_path / "crm_contacts.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def custom_field_store(tmp_path):
    store = SQLiteCrmCustomFieldStore(str(tmp_path / "crm_custom_fields.db"))
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def batch_store(tmp_path):
    store = SQLiteCrmImportBatchStore(str(tmp_path / "crm_import_batches.db"))
    await store.connect()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_contact_create_and_get_roundtrip(contact_store):
    await contact_store.create(make_contact("c1", email="ada@example.com", technologies=["Python"]))
    fetched = await contact_store.get("c1")
    assert fetched.email == "ada@example.com"
    assert fetched.technologies == ["Python"]


@pytest.mark.asyncio
async def test_contact_email_lookup_is_normalized(contact_store):
    await contact_store.create(make_contact("c1", email="Ada@Example.com"))
    found = await contact_store.get_by_email("ada@example.com")
    assert found is not None
    assert found.crm_contact_id == "c1"


@pytest.mark.asyncio
async def test_contact_duplicate_email_rejected_by_unique_constraint(contact_store):
    await contact_store.create(make_contact("c1", email="ada@example.com"))
    with pytest.raises(ValueError):
        await contact_store.create(make_contact("c2", email="ADA@EXAMPLE.COM"))


@pytest.mark.asyncio
async def test_contact_duplicate_apollo_contact_id_rejected(contact_store):
    await contact_store.create(make_contact("c1", apollo_contact_id="apollo-1"))
    with pytest.raises(ValueError):
        await contact_store.create(make_contact("c2", apollo_contact_id="apollo-1"))


@pytest.mark.asyncio
async def test_contact_multiple_null_emails_allowed(contact_store):
    """SQLite allows multiple NULLs in a UNIQUE column -- two contacts with no email must not collide."""
    await contact_store.create(make_contact("c1"))
    await contact_store.create(make_contact("c2"))  # must not raise


@pytest.mark.asyncio
async def test_contact_find_by_name_and_company_can_return_multiple(contact_store):
    await contact_store.create(make_contact("c1", first_name="Ada", last_name="Lovelace", company="Acme"))
    await contact_store.create(make_contact("c2", first_name="Ada", last_name="Lovelace", company="Acme"))
    from app.models.crm import normalize_name_company

    matches = await contact_store.find_by_name_and_company(normalize_name_company("Ada", "Lovelace", "Acme"))
    assert {c.crm_contact_id for c in matches} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_contact_save_persists_mutations(contact_store):
    await contact_store.create(make_contact("c1", company="Old Co"))
    contact = await contact_store.get("c1")
    contact.company = "New Co"
    await contact_store.save(contact)

    assert (await contact_store.get("c1")).company == "New Co"


@pytest.mark.asyncio
async def test_contact_list_orders_by_created_at(contact_store):
    await contact_store.create(make_contact("c1"))
    await contact_store.create(make_contact("c2"))
    contacts = await contact_store.list()
    assert [c.crm_contact_id for c in contacts] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_contact_survives_a_fresh_connection(tmp_path):
    db_path = str(tmp_path / "persist.db")
    first = SQLiteCrmContactStore(db_path)
    await first.connect()
    await first.create(make_contact("c1", email="ada@example.com"))
    await first.close()

    second = SQLiteCrmContactStore(db_path)
    await second.connect()
    fetched = await second.get_by_email("ada@example.com")
    await second.close()

    assert fetched is not None
    assert fetched.crm_contact_id == "c1"


@pytest.mark.asyncio
async def test_custom_field_create_and_get_by_key(custom_field_store):
    now = datetime.now(timezone.utc)
    definition = CrmCustomFieldDefinition(
        crm_custom_field_id="f1", field_key="fav_team", label="Favorite Team",
        field_type=CustomFieldType.TEXT, created_at=now, updated_at=now,
    )
    await custom_field_store.create(definition)
    found = await custom_field_store.get_by_field_key("fav_team")
    assert found.label == "Favorite Team"


@pytest.mark.asyncio
async def test_custom_field_duplicate_key_rejected(custom_field_store):
    now = datetime.now(timezone.utc)
    await custom_field_store.create(
        CrmCustomFieldDefinition(crm_custom_field_id="f1", field_key="k", label="A", field_type=CustomFieldType.TEXT, created_at=now, updated_at=now)
    )
    with pytest.raises(ValueError):
        await custom_field_store.create(
            CrmCustomFieldDefinition(crm_custom_field_id="f2", field_key="k", label="B", field_type=CustomFieldType.TEXT, created_at=now, updated_at=now)
        )


@pytest.mark.asyncio
async def test_import_batch_create_and_get(batch_store):
    batch = CrmImportBatch(
        import_batch_id="b1", filename="p.csv", uploaded_at=datetime.now(timezone.utc),
        headers=["Email"], rows=[{"Email": "a@example.com"}], row_count=1,
    )
    await batch_store.create(batch)
    fetched = await batch_store.get("b1")
    assert fetched.row_count == 1
    assert fetched.rows[0]["Email"] == "a@example.com"
