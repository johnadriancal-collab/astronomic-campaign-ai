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
from app.models.crm import CrmContact
from app.repositories.client_contact_store import ClientContactStore, MemoryClientContactStore
from app.repositories.client_note_store import MemoryClientNoteStore
from app.repositories.client_store import ClientStore, MemoryClientStore
from app.repositories.crm_contact_store import CrmContactStore, MemoryCrmContactStore
from app.repositories.engagement_store import EngagementStore, MemoryEngagementStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.services.activity_log_service import ActivityLogService
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.client_crm_service import ClientContactNotFound, ClientCrmService, ClientNotFound, EngagementNotFound

pytestmark = pytest.mark.asyncio


def _make_service(
    client_store: ClientStore,
    *,
    client_contact_store: ClientContactStore | None = None,
    crm_contact_store: CrmContactStore | None = None,
    engagement_store: EngagementStore | None = None,
) -> tuple[ClientCrmService, ActivityLogService]:
    activity_log = ActivityLogService(MemoryActivityEventStore())
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store or MemoryClientContactStore(),
        crm_contact_store=crm_contact_store or MemoryCrmContactStore(),
        engagement_store=engagement_store or MemoryEngagementStore(),
    )
    return service, activity_log


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
# ClientContact -- Client CRM Stage 1D (2026-09-07)
# =====================================================================


@pytest_asyncio.fixture
async def contact_service(tmp_path):
    """Same as memory_service, but ALSO exposes the ClientContactStore and
    CrmContactStore directly (needed to seed a canonical CrmContact before
    linking it, and to inspect ClientContact rows Stage 1D's own service
    methods don't otherwise return, e.g. list_for_crm_contact)."""
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    service, activity_log = _make_service(
        client_store, client_contact_store=client_contact_store, crm_contact_store=crm_contact_store
    )
    return service, activity_log, client_contact_store, crm_contact_store


async def _seed_crm_contact(crm_contact_store: CrmContactStore, **overrides) -> CrmContact:
    now = datetime.now(timezone.utc)
    fields = {
        "crm_contact_id": "ethan-1",
        "first_name": "Ethan",
        "last_name": "Wong",
        "email": "ethan@hiveasmbld.example.com",
        "phone": None,
        "title": "Co-CEO",
        "company": "Hive ASMBLD",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    contact = CrmContact(**fields)
    await crm_contact_store.create(contact)
    return contact


# =====================================================================
# Engagement -- Client CRM Stage 1E (2026-09-07)
# =====================================================================


@pytest.fixture
def engagement_service():
    """Same as memory_service, but ALSO exposes the EngagementStore
    directly (needed to inspect rows Stage 1E's own service methods
    don't otherwise return)."""
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    service, activity_log = _make_service(client_store, engagement_store=engagement_store)
    return service, activity_log, engagement_store


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
            engagement_type=EngagementType.DINNER, created_at=now, updated_at=now,
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


# =====================================================================
# ClientContact -- Client CRM Stage 1D (2026-09-07)
# =====================================================================


async def test_create_client_contact_requires_a_real_parent_client(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)

    with pytest.raises(ClientNotFound):
        await service.create_client_contact("does-not-exist", {"crm_contact_id": "ethan-1"})


async def test_list_client_contacts_requires_a_real_parent_client(contact_service):
    service, _activity_log, _client_contact_store, _crm_contact_store = contact_service
    with pytest.raises(ClientNotFound):
        await service.list_client_contacts("does-not-exist")


async def test_create_client_contact_requires_crm_contact_id(contact_service):
    service, _activity_log, _client_contact_store, _crm_contact_store = contact_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_contact(client.client_id, {})


async def test_create_client_contact_rejects_unknown_crm_contact_id(contact_service):
    service, _activity_log, _client_contact_store, _crm_contact_store = contact_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_contact(client.client_id, {"crm_contact_id": "does-not-exist"})


async def test_create_client_contact_rejects_archived_crm_contact(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store, archived=True)
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})


async def test_create_client_contact_links_existing_crm_contact_and_populates_snapshot(contact_service):
    service, activity_log, _client_contact_store, crm_contact_store = contact_service
    ethan = await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})

    contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True, "title": "Co-CEO"}
    )

    assert contact.client_id == client.client_id
    assert contact.crm_contact_id == "ethan-1"
    assert contact.first_name == "Ethan"
    assert contact.last_name == "Wong"
    assert contact.email == ethan.email
    assert contact.phone is None  # ethan has no phone on file -- never fabricated
    assert contact.title == "Co-CEO"  # relationship-specific, from the request, not copied from CrmContact.title
    assert contact.is_primary_contact is True
    assert contact.archived is False

    # The canonical Contact is completely unchanged.
    assert (await crm_contact_store.get("ethan-1")).model_dump() == ethan.model_dump()

    activity_page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    assert activity_page.items[0].event_type == "client_contact.created"


async def test_create_client_contact_never_creates_a_duplicate_crm_contact(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})

    assert len(await crm_contact_store.list()) == 1


async def test_client_detail_lists_linked_contact(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True})

    contacts = await service.list_client_contacts(client.client_id)
    assert len(contacts) == 1
    assert contacts[0].first_name == "Ethan"


async def test_first_primary_contact_created_is_primary(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    ethan_contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True}
    )
    assert ethan_contact.is_primary_contact is True


async def test_creating_a_second_primary_contact_demotes_the_first(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan", last_name="Wong")
    await _seed_crm_contact(crm_contact_store, crm_contact_id="tim-1", first_name="Tim", last_name="Lankau")
    client = await service.create_client({"name": "Hive ASMBLD"})
    ethan_contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True}
    )
    tim_contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "tim-1", "is_primary_contact": True}
    )

    contacts = {c.client_contact_id: c for c in await service.list_client_contacts(client.client_id)}
    assert contacts[tim_contact.client_contact_id].is_primary_contact is True
    assert contacts[ethan_contact.client_contact_id].is_primary_contact is False


async def test_switching_primary_contact_via_update_demotes_the_other(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan", last_name="Wong")
    await _seed_crm_contact(crm_contact_store, crm_contact_id="tim-1", first_name="Tim", last_name="Lankau")
    client = await service.create_client({"name": "Hive ASMBLD"})
    ethan_contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True}
    )
    tim_contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "tim-1"})
    assert tim_contact.is_primary_contact is False

    updated_tim = await service.update_client_contact(
        client.client_id, tim_contact.client_contact_id, {"is_primary_contact": True}
    )
    assert updated_tim.is_primary_contact is True

    refreshed_ethan = await _client_contact_store.get(ethan_contact.client_contact_id)
    assert refreshed_ethan.is_primary_contact is False


async def test_update_client_contact_is_a_genuine_partial_patch(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1", "title": "Co-CEO"})

    updated = await service.update_client_contact(client.client_id, contact.client_contact_id, {"role_notes": "Introduced us to the CFO"})
    assert updated.role_notes == "Introduced us to the CFO"
    assert updated.title == "Co-CEO"  # untouched
    assert updated.is_primary_contact is False  # untouched


async def test_archiving_a_client_contact_is_soft_and_reversible(contact_service):
    service, activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})

    archived = await service.update_client_contact(client.client_id, contact.client_contact_id, {"archived": True})
    assert archived.archived is True

    restored = await service.update_client_contact(client.client_id, contact.client_contact_id, {"archived": False})
    assert restored.archived is False

    events = [e.event_type for e in (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items]
    assert "client_contact.archived" in events
    assert "client_contact.restored" in events


async def test_archiving_the_primary_contact_clears_primary_status(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(
        client.client_id, {"crm_contact_id": "ethan-1", "is_primary_contact": True}
    )

    archived = await service.update_client_contact(client.client_id, contact.client_contact_id, {"archived": True})
    assert archived.archived is True
    assert archived.is_primary_contact is False


async def test_archived_contact_cannot_become_primary(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})
    await service.update_client_contact(client.client_id, contact.client_contact_id, {"archived": True})

    with pytest.raises(ValueError):
        await service.update_client_contact(client.client_id, contact.client_contact_id, {"is_primary_contact": True})


async def test_cannot_archive_and_set_primary_in_the_same_patch(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(ValueError):
        await service.update_client_contact(
            client.client_id, contact.client_contact_id, {"archived": True, "is_primary_contact": True}
        )


async def test_update_client_contact_requires_matching_parent_client(contact_service):
    service, _activity_log, _client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    client_b = await service.create_client({"name": "Other Co"})
    contact = await service.create_client_contact(client_a.client_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(ClientContactNotFound):
        await service.update_client_contact(client_b.client_id, contact.client_contact_id, {"role_notes": "x"})


async def test_no_hard_delete_of_client_contact(contact_service):
    service, _activity_log, client_contact_store, crm_contact_store = contact_service
    await _seed_crm_contact(crm_contact_store)
    client = await service.create_client({"name": "Hive ASMBLD"})
    contact = await service.create_client_contact(client.client_id, {"crm_contact_id": "ethan-1"})
    await service.update_client_contact(client.client_id, contact.client_contact_id, {"archived": True})

    assert not hasattr(client_contact_store, "delete")
    assert await client_contact_store.get(contact.client_contact_id) is not None


# =====================================================================
# Engagement -- Client CRM Stage 1E (2026-09-07)
# =====================================================================


async def test_create_engagement_requires_a_real_parent_client(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    with pytest.raises(ClientNotFound):
        await service.create_client_engagement("does-not-exist", {"title": "SF Investor Dinner", "engagement_type": "dinner"})


async def test_list_engagements_requires_a_real_parent_client(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    with pytest.raises(ClientNotFound):
        await service.list_client_engagements("does-not-exist")


async def test_create_engagement_rejects_blank_title(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_engagement(client.client_id, {"title": "   ", "engagement_type": "dinner"})


async def test_create_engagement_minimal_generates_id_and_defaults(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Investor Dinner", "engagement_type": "dinner"}
    )
    assert engagement.engagement_id
    assert engagement.client_id == client.client_id
    assert engagement.created_at == engagement.updated_at
    assert engagement.status == "planned"
    assert engagement.contract_status == "not_sent"
    assert engagement.payment_status == "unpaid"
    assert engagement.dinner_type is None
    assert engagement.owner is None
    assert engagement.archived is False


async def test_create_engagement_accepts_every_field(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id,
        {
            "title": "SF Investor Dinner",
            "engagement_type": "dinner",
            "dinner_type": "investor_dinner",
            "engagement_date": date(2026, 9, 22),
            "location": "The Battery, San Francisco",
            "status": "confirmed",
            "owner": "Chris",
            "fee": 5000.0,
            "contract_status": "signed",
            "contract_url": "https://drive.example.com/contract",
            "signed_date": date(2026, 9, 1),
            "payment_status": "partial",
            "luma_event_id": "luma-123",
        },
    )
    assert engagement.dinner_type == "investor_dinner"
    assert engagement.status == "confirmed"
    assert engagement.owner == "Chris"
    assert engagement.fee == 5000.0
    assert engagement.contract_status == "signed"
    assert engagement.contract_url == "https://drive.example.com/contract"
    assert engagement.signed_date == date(2026, 9, 1)
    assert engagement.payment_status == "partial"
    assert engagement.luma_event_id == "luma-123"


async def test_create_engagement_clears_dinner_type_for_non_dinner_type(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id,
        {"title": "Fall Sponsorship", "engagement_type": "sponsorship", "dinner_type": "investor_dinner"},
    )
    assert engagement.dinner_type is None


async def test_create_engagement_keeps_dinner_type_for_dinner_engagement(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id,
        {"title": "Fireside Night", "engagement_type": "dinner", "dinner_type": "fireside_dinner"},
    )
    assert engagement.dinner_type == "fireside_dinner"


async def test_create_engagement_emits_activity_event(engagement_service):
    service, activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Investor Dinner", "engagement_type": "dinner"}
    )
    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    assert page.items[0].event_type == "engagement.created"
    assert page.items[0].entity_id == engagement.engagement_id


async def test_list_client_engagements_scoped_to_client(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    client_b = await service.create_client({"name": "Other Co"})
    await service.create_client_engagement(client_a.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    await service.create_client_engagement(client_b.client_id, {"title": "Other Dinner", "engagement_type": "dinner"})

    engagements = await service.list_client_engagements(client_a.client_id)
    assert len(engagements) == 1
    assert engagements[0].title == "SF Dinner"


async def test_get_client_engagement_existing(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    created = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    fetched = await service.get_client_engagement(client.client_id, created.engagement_id)
    assert fetched == created


async def test_get_client_engagement_missing_raises_not_found(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(EngagementNotFound):
        await service.get_client_engagement(client.client_id, "does-not-exist")


async def test_engagement_requested_through_wrong_client_is_not_found(engagement_service):
    """An Engagement belonging to Client A must never be exposed or
    mutable through Client B's URL -- same isolation rule as
    ClientContact."""
    service, _activity_log, _engagement_store = engagement_service
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    client_b = await service.create_client({"name": "Other Co"})
    engagement = await service.create_client_engagement(client_a.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})

    with pytest.raises(EngagementNotFound):
        await service.get_client_engagement(client_b.client_id, engagement.engagement_id)
    with pytest.raises(EngagementNotFound):
        await service.update_client_engagement(client_b.client_id, engagement.engagement_id, {"title": "Hijacked"})


async def test_update_client_engagement_is_a_genuine_partial_patch(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "owner": "Chris"}
    )
    updated = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"status": "confirmed"})
    assert updated.status == "confirmed"
    assert updated.owner == "Chris"  # untouched
    assert updated.title == "SF Dinner"  # untouched
    assert updated.updated_at > engagement.updated_at


async def test_update_client_engagement_rejects_blank_title(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    with pytest.raises(ValueError):
        await service.update_client_engagement(client.client_id, engagement.engagement_id, {"title": "   "})


async def test_update_client_engagement_clears_dinner_type_when_type_changes_to_non_dinner(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "dinner_type": "investor_dinner"}
    )
    updated = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"engagement_type": "sponsorship"})
    assert updated.dinner_type is None


async def test_archive_and_restore_engagement_via_patch(engagement_service):
    service, activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})

    archived = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"archived": True})
    assert archived.archived is True
    restored = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"archived": False})
    assert restored.archived is False

    events = [e.event_type for e in (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items]
    assert "engagement.archived" in events
    assert "engagement.restored" in events


async def test_no_hard_delete_of_engagement(engagement_service):
    service, _activity_log, engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    await service.update_client_engagement(client.client_id, engagement.engagement_id, {"archived": True})

    assert not hasattr(engagement_store, "delete")
    assert await engagement_store.get(engagement.engagement_id) is not None


async def test_engagement_update_never_mutates_client(engagement_service):
    """Engagement is historical/commercial delivery data -- updating one
    must never write to Client.relationship_classification/next_action/
    owner (see ClientCrmService's own Stage 1E module docstring)."""
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD", "owner": "Chris", "next_action": "Schedule Q1 check-in"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "owner": "Ria"}
    )
    await service.update_client_engagement(client.client_id, engagement.engagement_id, {"status": "completed"})

    refreshed_client = await service.get_client(client.client_id)
    assert refreshed_client.owner == "Chris"
    assert refreshed_client.next_action == "Schedule Q1 check-in"
    assert refreshed_client.relationship_classification is None


def test_engagement_type_and_dinner_type_enum_values_match_stage_1e1_taxonomy():
    """Stage 1E.1: engagement_type collapsed to DINNER/SPONSORSHIP/OTHER;
    dinner_type replaces the retired DinnerProgram (Supernova/Galaxy/
    Aurora) with the underlying dinner kind named directly."""
    from app.models.client_crm import DinnerType, EngagementType

    assert {member.value for member in EngagementType} == {"dinner", "sponsorship", "other"}
    assert {member.value for member in DinnerType} == {"investor_dinner", "fireside_dinner", "bizdev_dinner"}
