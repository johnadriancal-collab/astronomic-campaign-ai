"""
ClientCrmService tests -- Client CRM Stage 1B (2026-09-07). Client CRUD
only; ClientContact/Engagement/ClientNote are never touched by this
service (see client_crm_service.py's own module docstring) -- proven here
directly by constructing their OWN independent stores and confirming
archiving a Client never mutates rows in them.
"""

from datetime import date, datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.client_crm import ClientRelationshipClassification, ClientStatus
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_note_store import MemoryClientNoteStore
from app.repositories.client_store import ClientStore, MemoryClientStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.services.activity_log_service import ActivityLogService
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.client_crm_service import ClientCrmService, ClientNotFound

pytestmark = pytest.mark.asyncio


def _make_service(client_store: ClientStore) -> tuple[ClientCrmService, ActivityLogService]:
    activity_log = ActivityLogService(MemoryActivityEventStore())
    return ClientCrmService(client_store=client_store, activity_log=activity_log), activity_log


@pytest.fixture
def memory_service():
    service, activity_log = _make_service(MemoryClientStore())
    return service, activity_log


@pytest_asyncio.fixture
async def sqlite_service(tmp_path):
    store = SQLiteClientStore(str(tmp_path / "clients.db"))
    await store.connect()
    service, activity_log = _make_service(store)
    yield service, activity_log
    await store.close()


# =====================================================================
# Create -- generated id/timestamps/defaults, validation
# =====================================================================


async def test_create_client_generates_id_and_timestamps(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})

    assert client.client_id
    assert client.name == "Hive ASMBLD"
    assert client.created_at == client.updated_at
    assert client.status == ClientStatus.ACTIVE
    assert client.relationship_classification is None
    assert client.archived is False


async def test_create_client_accepts_every_optional_field(memory_service):
    service, _ = memory_service
    client = await service.create_client({
        "name": "Acme Co",
        "website": "https://acme.example.com",
        "industry": "Venture",
        "status": ClientStatus.INACTIVE,
        "relationship_classification": ClientRelationshipClassification.OPPORTUNITY,
        "owner": "Chris",
        "next_action": "Schedule Q1 check-in",
        "next_action_due": date(2027, 1, 15),
    })

    assert client.website == "https://acme.example.com"
    assert client.status == ClientStatus.INACTIVE
    assert client.relationship_classification == ClientRelationshipClassification.OPPORTUNITY
    assert client.owner == "Chris"
    assert client.next_action_due == date(2027, 1, 15)


async def test_create_client_rejects_blank_name(memory_service):
    service, _ = memory_service
    with pytest.raises(ValueError):
        await service.create_client({"name": ""})
    with pytest.raises(ValueError):
        await service.create_client({"name": "   "})


async def test_create_client_strips_surrounding_whitespace_from_name(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "  Hive ASMBLD  "})
    assert client.name == "Hive ASMBLD"


async def test_create_client_two_clients_with_the_same_name_are_both_allowed(memory_service):
    """Stage 1B's own approved duplicate-name behavior: no dedup check, no
    uniqueness constraint -- see this stage's STOP report."""
    service, _ = memory_service
    first = await service.create_client({"name": "Hive ASMBLD"})
    second = await service.create_client({"name": "Hive ASMBLD"})
    assert first.client_id != second.client_id
    assert first.name == second.name


async def test_create_client_emits_activity_event(memory_service):
    service, activity_log = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    assert page.total == 1
    assert page.items[0].event_type == "client.created"
    assert page.items[0].entity_id == client.client_id


# =====================================================================
# Get
# =====================================================================


async def test_get_client_existing(memory_service):
    service, _ = memory_service
    created = await service.create_client({"name": "Hive ASMBLD"})
    fetched = await service.get_client(created.client_id)
    assert fetched == created


async def test_get_client_missing_raises_not_found(memory_service):
    service, _ = memory_service
    with pytest.raises(ClientNotFound):
        await service.get_client("does-not-exist")


# =====================================================================
# List / search / filter / sort / paginate
# =====================================================================


async def test_list_clients_basic(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client({"name": "Acme Co"})
    page = await service.list_clients()
    assert page.total == 2
    assert len(page.items) == 2


async def test_list_clients_search_by_name(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client({"name": "Acme Co"})
    page = await service.list_clients(q="hive")
    assert page.total == 1
    assert page.items[0].name == "Hive ASMBLD"


async def test_list_clients_status_filter(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "Active Co", "status": ClientStatus.ACTIVE})
    await service.create_client({"name": "Inactive Co", "status": ClientStatus.INACTIVE})
    page = await service.list_clients(status=ClientStatus.INACTIVE)
    assert page.total == 1
    assert page.items[0].name == "Inactive Co"


async def test_list_clients_classification_filter(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "A", "relationship_classification": ClientRelationshipClassification.NURTURE})
    await service.create_client({"name": "B", "relationship_classification": ClientRelationshipClassification.OPPORTUNITY})
    await service.create_client({"name": "C"})  # unclassified

    page = await service.list_clients(relationship_classification=ClientRelationshipClassification.OPPORTUNITY)
    assert page.total == 1
    assert page.items[0].name == "B"


async def test_list_clients_owner_filter_case_insensitive(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "A", "owner": "Chris"})
    await service.create_client({"name": "B", "owner": "Ria"})
    page = await service.list_clients(owner="chris")
    assert page.total == 1
    assert page.items[0].name == "A"


async def test_list_clients_excludes_archived_by_default_and_can_include(memory_service):
    service, _ = memory_service
    active = await service.create_client({"name": "Active Co"})
    archived = await service.create_client({"name": "Archived Co"})
    await service.update_client(archived.client_id, {"archived": True})

    default_page = await service.list_clients()
    assert [c.client_id for c in default_page.items] == [active.client_id]

    included_page = await service.list_clients(include_archived=True)
    assert {c.client_id for c in included_page.items} == {active.client_id, archived.client_id}


async def test_list_clients_sort_by_name_asc_and_desc(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "Zeta"})
    await service.create_client({"name": "Alpha"})

    asc = await service.list_clients(sort_by="name", sort_dir="asc")
    assert [c.name for c in asc.items] == ["Alpha", "Zeta"]

    desc = await service.list_clients(sort_by="name", sort_dir="desc")
    assert [c.name for c in desc.items] == ["Zeta", "Alpha"]


async def test_list_clients_sort_by_next_action_due_puts_unset_last_both_directions(memory_service):
    service, _ = memory_service
    await service.create_client({"name": "No due date"})
    await service.create_client({"name": "Later", "next_action_due": date(2027, 6, 1)})
    await service.create_client({"name": "Sooner", "next_action_due": date(2027, 1, 1)})

    asc = await service.list_clients(sort_by="next_action_due", sort_dir="asc")
    assert [c.name for c in asc.items] == ["Sooner", "Later", "No due date"]

    desc = await service.list_clients(sort_by="next_action_due", sort_dir="desc")
    assert [c.name for c in desc.items] == ["Later", "Sooner", "No due date"]


async def test_list_clients_rejects_invalid_sort_by(memory_service):
    service, _ = memory_service
    with pytest.raises(ValueError):
        await service.list_clients(sort_by="not_a_real_field")


async def test_list_clients_rejects_invalid_sort_dir(memory_service):
    service, _ = memory_service
    with pytest.raises(ValueError):
        await service.list_clients(sort_dir="sideways")


async def test_list_clients_pagination(memory_service):
    service, _ = memory_service
    for i in range(5):
        await service.create_client({"name": f"Client {i}"})
    page1 = await service.list_clients(sort_by="name", page=1, page_size=2)
    assert page1.total == 5
    assert len(page1.items) == 2
    page3 = await service.list_clients(sort_by="name", page=3, page_size=2)
    assert len(page3.items) == 1


# =====================================================================
# Update -- partial patch, immutability, updated_at, archive/restore
# =====================================================================


async def test_update_client_partial_patch(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    updated = await service.update_client(client.client_id, {"industry": "Venture"})
    assert updated.industry == "Venture"
    assert updated.name == "Hive ASMBLD"  # untouched


async def test_update_client_updated_at_changes(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    updated = await service.update_client(client.client_id, {"industry": "Venture"})
    assert updated.updated_at > client.updated_at
    assert updated.created_at == client.created_at


async def test_update_client_created_at_is_never_overwritten_by_a_patch_value(memory_service):
    """created_at is always preserved through an update, even if a caller
    (bypassing the API layer's own ClientUpdateRequest, which has no
    created_at field at all) somehow got one into the patch dict --
    updated_at is always server-computed fresh, but nothing here ever
    touches created_at."""
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    updated = await service.update_client(client.client_id, {"created_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    assert updated.created_at == client.created_at


async def test_update_client_silently_ignores_an_attempted_client_id_change(memory_service):
    """The API layer's own ClientUpdateRequest has no client_id field at
    all, so this is purely a defense-in-depth check: even if something
    bypassing that layer smuggled a DIFFERENT client_id into the patch
    dict, the service strips it before it ever reaches the store -- the
    update still succeeds, against the ORIGINAL client_id, completely
    ignoring the smuggled value rather than erroring OR silently
    retargeting a different row."""
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    updated = await service.update_client(client.client_id, {"client_id": "different-id", "industry": "Venture"})
    assert updated.client_id == client.client_id
    assert updated.industry == "Venture"
    assert await service.client_store.get("different-id") is None


async def test_update_client_rejects_blank_name(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.update_client(client.client_id, {"name": "   "})


async def test_update_client_missing_raises_not_found(memory_service):
    service, _ = memory_service
    with pytest.raises(ClientNotFound):
        await service.update_client("does-not-exist", {"industry": "Venture"})


async def test_update_client_emits_updated_event_for_a_plain_edit(memory_service):
    service, activity_log = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.update_client(client.client_id, {"industry": "Venture"})
    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    event_types = [e.event_type for e in page.items]
    assert event_types == ["client.updated", "client.created"]  # newest first


async def test_archive_and_restore_via_patch_emit_distinct_events(memory_service):
    service, activity_log = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})

    archived = await service.update_client(client.client_id, {"archived": True})
    assert archived.archived is True

    restored = await service.update_client(client.client_id, {"archived": False})
    assert restored.archived is False

    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    event_types = [e.event_type for e in page.items]
    assert event_types == ["client.restored", "client.archived", "client.created"]  # newest first


async def test_archived_client_remains_readable_by_id(memory_service):
    service, _ = memory_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.update_client(client.client_id, {"archived": True})
    fetched = await service.get_client(client.client_id)
    assert fetched.archived is True
    assert fetched.name == "Hive ASMBLD"


async def test_archiving_a_client_does_not_mutate_client_contact_engagement_or_note_stores():
    """Client CRUD in Stage 1B never touches ClientContact/Engagement/
    ClientNote -- proven directly by giving those entities their OWN,
    completely independent stores that ClientCrmService never even
    receives a reference to."""
    client_store = MemoryClientStore()
    service, _ = _make_service(client_store)
    client = await service.create_client({"name": "Hive ASMBLD"})

    now = datetime.now(timezone.utc)
    contact_store = MemoryClientContactStore()
    engagement_store = MemoryEngagementStore()
    note_store = MemoryClientNoteStore()
    from app.models.client_crm import ClientContact, ClientNote, Engagement, EngagementType

    await contact_store.create(
        ClientContact(client_contact_id="cc1", client_id=client.client_id, created_at=now, updated_at=now)
    )
    await engagement_store.create(
        Engagement(
            engagement_id="e1", client_id=client.client_id, title="Dinner",
            engagement_type=EngagementType.INVESTOR_DINNER, created_at=now, updated_at=now,
        )
    )
    await note_store.create(
        ClientNote(client_note_id="n1", client_id=client.client_id, body="note", occurred_at=now, created_at=now, updated_at=now)
    )

    await service.update_client(client.client_id, {"archived": True})

    contact = await contact_store.get("cc1")
    engagement = await engagement_store.get("e1")
    note = await note_store.get("n1")
    assert contact.archived is False
    assert engagement.archived is False
    assert note.archived is False


# =====================================================================
# Memory/SQLite parity
# =====================================================================


async def test_sqlite_service_create_get_list_update_archive_parity(sqlite_service):
    service, activity_log = sqlite_service
    client = await service.create_client({"name": "Hive ASMBLD", "industry": "Venture"})
    assert (await service.get_client(client.client_id)).industry == "Venture"

    page = await service.list_clients()
    assert page.total == 1

    updated = await service.update_client(client.client_id, {"owner": "Chris"})
    assert updated.owner == "Chris"
    assert updated.updated_at > client.updated_at

    archived = await service.update_client(client.client_id, {"archived": True})
    assert archived.archived is True
    assert (await service.get_client(client.client_id)).archived is True

    with pytest.raises(ClientNotFound):
        await service.get_client("does-not-exist")

    activity_page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    assert [e.event_type for e in activity_page.items] == ["client.archived", "client.updated", "client.created"]  # newest first
