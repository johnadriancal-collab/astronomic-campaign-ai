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
from app.models.luma import LumaEvent
from app.repositories.client_contact_store import ClientContactStore, MemoryClientContactStore
from app.repositories.client_note_store import MemoryClientNoteStore
from app.repositories.client_store import ClientStore, MemoryClientStore
from app.repositories.crm_contact_store import CrmContactStore, MemoryCrmContactStore
from app.repositories.engagement_closeout_store import EngagementCloseoutStore, MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import EngagementParticipantStore, MemoryEngagementParticipantStore
from app.repositories.engagement_store import EngagementStore, MemoryEngagementStore
from app.repositories.luma_event_store import LumaEventStore, MemoryLumaEventStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.services.activity_log_service import ActivityLogService
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.client_crm_service import (
    ClientContactNotFound,
    ClientCrmService,
    ClientNotFound,
    EngagementCloseoutAlreadyExists,
    EngagementCloseoutNotFound,
    EngagementLumaEventAlreadyLinked,
    EngagementNotFound,
    EngagementParticipantDuplicate,
    EngagementParticipantNotFound,
    LumaEventNotFound,
)

pytestmark = pytest.mark.asyncio


def _make_service(
    client_store: ClientStore,
    *,
    client_contact_store: ClientContactStore | None = None,
    crm_contact_store: CrmContactStore | None = None,
    engagement_store: EngagementStore | None = None,
    engagement_closeout_store: EngagementCloseoutStore | None = None,
    engagement_participant_store: EngagementParticipantStore | None = None,
    luma_event_store: LumaEventStore | None = None,
) -> tuple[ClientCrmService, ActivityLogService]:
    activity_log = ActivityLogService(MemoryActivityEventStore())
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store or MemoryClientContactStore(),
        crm_contact_store=crm_contact_store or MemoryCrmContactStore(),
        engagement_store=engagement_store or MemoryEngagementStore(),
        engagement_closeout_store=engagement_closeout_store or MemoryEngagementCloseoutStore(),
        engagement_participant_store=engagement_participant_store or MemoryEngagementParticipantStore(),
        luma_event_store=luma_event_store or MemoryLumaEventStore(),
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


def _luma_event(luma_event_id="luma-123", name="Hive ASMBLD SF Investor Dinner", start_at=None, **overrides) -> LumaEvent:
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return LumaEvent(
        luma_event_id=luma_event_id,
        name=name,
        start_at=start_at,
        synced_at=now,
        updated_at=now,
        **overrides,
    )


@pytest.fixture
def engagement_service():
    """Same as memory_service, but ALSO exposes the EngagementStore
    directly (needed to inspect rows Stage 1E's own service methods
    don't otherwise return)."""
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    service, activity_log = _make_service(client_store, engagement_store=engagement_store)
    return service, activity_log, engagement_store


@pytest.fixture
def engagement_closeout_service():
    """Same as engagement_service, but ALSO exposes the
    EngagementCloseoutStore directly."""
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    service, activity_log = _make_service(
        client_store, engagement_store=engagement_store, engagement_closeout_store=engagement_closeout_store
    )
    return service, activity_log, engagement_closeout_store


@pytest.fixture
def participant_service():
    """Same as engagement_service, but ALSO exposes the
    EngagementParticipantStore and CrmContactStore directly (needed to
    seed a canonical CrmContact before linking it, mirroring
    contact_service's own precedent for ClientContact)."""
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    crm_contact_store = MemoryCrmContactStore()
    service, activity_log = _make_service(
        client_store,
        engagement_store=engagement_store,
        engagement_participant_store=engagement_participant_store,
        crm_contact_store=crm_contact_store,
    )
    return service, activity_log, engagement_participant_store, crm_contact_store


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
    await service.luma_event_store.save(_luma_event("luma-123"))
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
    assert {member.value for member in DinnerType} == {
        "investor_dinner", "fireside_dinner", "bizdev_dinner", "donor_dinner", "custom_dinner",
    }


# =====================================================================
# Engagement <-> Luma event link -- Client CRM Stage 1H-A (2026-09-09)
# =====================================================================


async def test_create_engagement_rejects_a_nonexistent_luma_event_id(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_engagement(
            client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "does-not-exist"}
        )


async def test_create_engagement_never_persists_a_raw_unverified_luma_event_id(engagement_service):
    """The rejected create() above must never reach the store at all."""
    service, _activity_log, engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_engagement(
            client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "does-not-exist"}
        )
    assert await engagement_store.get_by_luma_event_id("does-not-exist") is None


async def test_create_engagement_rejects_a_luma_event_already_linked_elsewhere(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123"))
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"}
    )
    with pytest.raises(EngagementLumaEventAlreadyLinked):
        await service.create_client_engagement(
            client.client_id, {"title": "A Second Engagement", "engagement_type": "dinner", "luma_event_id": "luma-123"}
        )


async def test_create_engagement_with_no_luma_event_id_is_unlinked(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    assert engagement.luma_event_id is None


async def test_update_client_engagement_links_a_valid_luma_event(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123"))
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})

    updated = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"luma_event_id": "luma-123"})
    assert updated.luma_event_id == "luma-123"


async def test_update_client_engagement_rejects_a_nonexistent_luma_event_id(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    with pytest.raises(ValueError):
        await service.update_client_engagement(client.client_id, engagement.engagement_id, {"luma_event_id": "does-not-exist"})


async def test_update_client_engagement_rejects_a_luma_event_already_linked_to_a_different_engagement(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123"))
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"}
    )
    other = await service.create_client_engagement(client.client_id, {"title": "Other Dinner", "engagement_type": "dinner"})

    with pytest.raises(EngagementLumaEventAlreadyLinked):
        await service.update_client_engagement(client.client_id, other.engagement_id, {"luma_event_id": "luma-123"})


async def test_update_client_engagement_repatching_its_own_luma_event_id_does_not_conflict_with_itself(engagement_service):
    """A caller re-sending the SAME luma_event_id an Engagement is already
    linked to (e.g. re-submitting an unchanged form) must not be rejected
    as "already linked to another Engagement" -- it's linked to THIS one."""
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123"))
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"}
    )
    updated = await service.update_client_engagement(
        client.client_id, engagement.engagement_id, {"luma_event_id": "luma-123", "owner": "Chris"}
    )
    assert updated.luma_event_id == "luma-123"
    assert updated.owner == "Chris"


async def test_update_client_engagement_unlinks_via_null(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123"))
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(
        client.client_id, {"title": "SF Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"}
    )
    unlinked = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"luma_event_id": None})
    assert unlinked.luma_event_id is None

    # And the slot is free again -- a different Engagement can now claim it.
    other = await service.create_client_engagement(
        client.client_id, {"title": "Other Dinner", "engagement_type": "dinner", "luma_event_id": "luma-123"}
    )
    assert other.luma_event_id == "luma-123"


async def test_update_client_engagement_other_fields_never_require_a_luma_event_id(engagement_service):
    """PATCHing a field unrelated to luma_event_id must never trigger Luma
    validation at all -- omitted means "leave it alone," not "clear it"."""
    service, _activity_log, _engagement_store = engagement_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    engagement = await service.create_client_engagement(client.client_id, {"title": "SF Dinner", "engagement_type": "dinner"})
    updated = await service.update_client_engagement(client.client_id, engagement.engagement_id, {"status": "confirmed"})
    assert updated.luma_event_id is None
    assert updated.status == "confirmed"


async def test_list_luma_events_orders_most_recent_upcoming_first_with_nulls_last(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("e-past", name="Past Dinner", start_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await service.luma_event_store.save(_luma_event("e-future", name="Future Dinner", start_at=datetime(2026, 12, 1, tzinfo=timezone.utc)))
    await service.luma_event_store.save(_luma_event("e-no-date", name="Undated Dinner", start_at=None))

    events = await service.list_luma_events()
    assert [e.luma_event_id for e in events] == ["e-future", "e-past", "e-no-date"]


async def test_list_luma_events_filters_case_insensitively_by_name_substring(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("e1", name="SF Investor Dinner"))
    await service.luma_event_store.save(_luma_event("e2", name="NY Founder Breakfast"))

    events = await service.list_luma_events(q="investor")
    assert [e.luma_event_id for e in events] == ["e1"]
    assert await service.list_luma_events(q="INVESTOR") == events
    assert await service.list_luma_events(q="nonexistent") == []


async def test_list_luma_events_returns_only_picker_relevant_fields(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(
        _luma_event("e1", name="SF Investor Dinner", location_summary="The Battery", url="https://lu.ma/e1", calendar_id="cal-1")
    )
    events = await service.list_luma_events()
    assert events[0].location_summary == "The Battery"
    assert events[0].url == "https://lu.ma/e1"
    assert not hasattr(events[0], "calendar_id")


async def test_get_luma_event_returns_the_stored_summary(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    await service.luma_event_store.save(_luma_event("luma-123", name="SF Investor Dinner"))
    summary = await service.get_luma_event("luma-123")
    assert summary.luma_event_id == "luma-123"
    assert summary.name == "SF Investor Dinner"


async def test_get_luma_event_missing_raises_not_found(engagement_service):
    service, _activity_log, _engagement_store = engagement_service
    with pytest.raises(LumaEventNotFound):
        await service.get_luma_event("does-not-exist")


# =====================================================================
# EngagementCloseout -- Client CRM Stage 1F (2026-09-08)
# =====================================================================


async def _make_client_and_engagement(service, **engagement_overrides):
    client = await service.create_client({"name": "Hive ASMBLD"})
    fields = {"title": "SF Investor Dinner", "engagement_type": "dinner", "dinner_type": "investor_dinner"}
    fields.update(engagement_overrides)
    engagement = await service.create_client_engagement(client.client_id, fields)
    return client, engagement


async def test_get_engagement_closeout_raises_not_found_when_none_recorded_yet(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(EngagementCloseoutNotFound):
        await service.get_engagement_closeout(client.client_id, engagement.engagement_id)


async def test_get_engagement_closeout_requires_a_real_engagement(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client = await service.create_client({"name": "Hive ASMBLD"})

    with pytest.raises(EngagementNotFound):
        await service.get_engagement_closeout(client.client_id, "does-not-exist")


async def test_create_engagement_closeout_minimal(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    closeout = await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})

    assert closeout.closeout_id
    assert closeout.engagement_id == engagement.engagement_id
    assert closeout.client_id == client.client_id
    assert closeout.confirmed_guest_count is None
    assert closeout.attended_count is None
    assert closeout.archived is False
    assert closeout.created_at == closeout.updated_at


async def test_create_engagement_closeout_distinguishes_null_from_explicit_zero(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    closeout = await service.create_engagement_closeout(
        client.client_id,
        engagement.engagement_id,
        {"confirmed_guest_count": 10, "no_show_count": 0, "cancelled_count": None},
    )

    assert closeout.confirmed_guest_count == 10
    assert closeout.no_show_count == 0  # explicit zero, not None
    assert closeout.cancelled_count is None  # not entered


async def test_create_engagement_closeout_accepts_every_field(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    now = datetime.now(timezone.utc)

    closeout = await service.create_engagement_closeout(
        client.client_id,
        engagement.engagement_id,
        {
            "confirmed_guest_count": 12,
            "attended_count": 10,
            "no_show_count": 2,
            "cancelled_count": 1,
            "unexpected_attendee_count": 1,
            "guest_quality": "Strong investor turnout",
            "dinner_dynamics": "Energetic, good cross-table conversation",
            "initial_client_experience": "Client seemed thrilled",
            "immediate_outcomes": "Two intro requests",
            "notable_signals": "One guest asked about co-investing",
            "issues": "Ran 20 minutes over",
            "referrals": "Guest offered to refer a colleague",
            "future_opportunities": "Possible follow-on dinner in Q1",
            "internal_notes": "Use this venue again",
            "completed_at": now,
            "completed_by": "Chris",
        },
    )

    assert closeout.confirmed_guest_count == 12
    assert closeout.attended_count == 10
    assert closeout.no_show_count == 2
    assert closeout.cancelled_count == 1
    assert closeout.unexpected_attendee_count == 1
    assert closeout.guest_quality == "Strong investor turnout"
    assert closeout.completed_by == "Chris"
    assert closeout.completed_at == now


async def test_create_engagement_closeout_rejects_negative_counts(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {"attended_count": -1})


async def test_create_engagement_closeout_requires_a_real_engagement(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client = await service.create_client({"name": "Hive ASMBLD"})

    with pytest.raises(EngagementNotFound):
        await service.create_engagement_closeout(client.client_id, "does-not-exist", {})


async def test_create_engagement_closeout_emits_activity_event_without_qualitative_content(engagement_closeout_service):
    service, activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_closeout(
        client.client_id,
        engagement.engagement_id,
        {"guest_quality": "Very sensitive commercial detail", "attended_count": 10},
    )

    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    created_event = next(e for e in page.items if e.event_type == "engagement_closeout.created")
    assert created_event.metadata == {"client_id": client.client_id, "engagement_id": engagement.engagement_id}
    # The qualitative text must never leak into Activity Log metadata/summary.
    assert "Very sensitive commercial detail" not in created_event.summary
    assert "Very sensitive commercial detail" not in str(created_event.metadata)
    assert "10" not in created_event.summary  # counts excluded too


# --- 1:1 enforcement ---------------------------------------------------


async def test_create_engagement_closeout_second_attempt_raises_already_exists(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})

    with pytest.raises(EngagementCloseoutAlreadyExists):
        await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})


async def test_create_engagement_closeout_second_attempt_does_not_overwrite_the_first(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    first = await service.create_engagement_closeout(
        client.client_id, engagement.engagement_id, {"attended_count": 10}
    )

    with pytest.raises(EngagementCloseoutAlreadyExists):
        await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {"attended_count": 999})

    refreshed = await service.get_engagement_closeout(client.client_id, engagement.engagement_id)
    assert refreshed.closeout_id == first.closeout_id
    assert refreshed.attended_count == 10


async def test_create_engagement_closeout_still_blocked_after_archiving(engagement_closeout_service):
    """At most one Closeout is EVER created per Engagement -- archiving
    the existing one does not free up a slot for a new one; PATCH/restore
    the existing one instead."""
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})
    await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"archived": True})

    with pytest.raises(EngagementCloseoutAlreadyExists):
        await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})


# --- Partial update / archive / restore ---------------------------------


async def test_update_engagement_closeout_is_a_genuine_partial_patch(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_closeout(
        client.client_id, engagement.engagement_id, {"attended_count": 10, "guest_quality": "Strong"}
    )

    updated = await service.update_engagement_closeout(
        client.client_id, engagement.engagement_id, {"attended_count": 11}
    )

    assert updated.attended_count == 11
    assert updated.guest_quality == "Strong"  # untouched


async def test_update_engagement_closeout_rejects_negative_counts(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})

    with pytest.raises(ValueError):
        await service.update_engagement_closeout(
            client.client_id, engagement.engagement_id, {"no_show_count": -1}
        )


async def test_update_engagement_closeout_missing_raises_not_found(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(EngagementCloseoutNotFound):
        await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"attended_count": 5})


async def test_archive_and_restore_engagement_closeout_via_patch(engagement_closeout_service):
    service, activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})

    archived = await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"archived": True})
    assert archived.archived is True
    restored = await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"archived": False})
    assert restored.archived is False

    events = [e.event_type for e in (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items]
    assert "engagement_closeout.archived" in events
    assert "engagement_closeout.restored" in events


async def test_no_hard_delete_of_engagement_closeout(engagement_closeout_service):
    service, _activity_log, store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service)
    closeout = await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {})
    await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"archived": True})

    assert not hasattr(store, "delete")
    assert await store.get(closeout.closeout_id) is not None


# --- Cross-client / cross-engagement isolation ---------------------------


async def test_engagement_closeout_requested_through_wrong_client_is_not_found(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client_a, engagement_a = await _make_client_and_engagement(service)
    client_b = await service.create_client({"name": "Other Co"})
    await service.create_engagement_closeout(client_a.client_id, engagement_a.engagement_id, {"attended_count": 10})

    with pytest.raises(EngagementNotFound):
        await service.get_engagement_closeout(client_b.client_id, engagement_a.engagement_id)
    with pytest.raises(EngagementNotFound):
        await service.update_engagement_closeout(client_b.client_id, engagement_a.engagement_id, {"attended_count": 1})
    with pytest.raises(EngagementNotFound):
        await service.create_engagement_closeout(client_b.client_id, engagement_a.engagement_id, {})


async def test_engagement_closeout_does_not_leak_across_two_engagements_on_the_same_client(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement_a = await _make_client_and_engagement(service)
    engagement_b = await service.create_client_engagement(
        client.client_id, {"title": "Second Dinner", "engagement_type": "dinner"}
    )
    await service.create_engagement_closeout(client.client_id, engagement_a.engagement_id, {"attended_count": 10})

    with pytest.raises(EngagementCloseoutNotFound):
        await service.get_engagement_closeout(client.client_id, engagement_b.engagement_id)
    # Creating one for engagement_b is still allowed -- it's a different Engagement.
    closeout_b = await service.create_engagement_closeout(client.client_id, engagement_b.engagement_id, {})
    assert closeout_b.engagement_id == engagement_b.engagement_id


# --- Never a side-effect source -------------------------------------------


async def test_engagement_closeout_never_mutates_engagement_or_client(engagement_closeout_service):
    service, _activity_log, _store = engagement_closeout_service
    client, engagement = await _make_client_and_engagement(service, status="confirmed", owner="Chris")

    await service.create_engagement_closeout(client.client_id, engagement.engagement_id, {"attended_count": 10})
    await service.update_engagement_closeout(client.client_id, engagement.engagement_id, {"attended_count": 11})

    refreshed_engagement = await service.get_client_engagement(client.client_id, engagement.engagement_id)
    assert refreshed_engagement.status == "confirmed"
    assert refreshed_engagement.owner == "Chris"

    refreshed_client = await service.get_client(client.client_id)
    assert refreshed_client.relationship_classification is None
    assert refreshed_client.next_action is None


# =====================================================================
# EngagementParticipant -- Client CRM Stage 1G (2026-09-08)
# =====================================================================


async def test_create_resolved_participant_snapshots_from_canonical_contact(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"}
    )

    assert participant.crm_contact_id == "ethan-1"
    assert participant.first_name == "Ethan"
    assert participant.last_name == "Wong"
    assert participant.email == "ethan@hiveasmbld.example.com"
    assert participant.title == "Co-CEO"
    assert participant.company == "Hive ASMBLD"
    assert participant.role == "guest"
    assert participant.source == "manual"
    assert participant.archived is False


async def test_create_resolved_participant_ignores_manually_sent_identity_fields(participant_service):
    """The canonical Contact's own data always wins -- any name/email
    also present in the request is ignored when crm_contact_id is set."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        {"crm_contact_id": "ethan-1", "first_name": "Someone Else", "email": "fake@example.com"},
    )

    assert participant.first_name == "Ethan"
    assert participant.email == "ethan@hiveasmbld.example.com"


async def test_create_resolved_participant_never_mutates_the_canonical_contact(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    contact = await crm_contact_store.get("ethan-1")
    assert contact.first_name == "Ethan"
    assert contact.company == "Hive ASMBLD"


async def test_create_resolved_participant_rejects_missing_crm_contact(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "does-not-exist"})


async def test_create_resolved_participant_rejects_archived_contact(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, archived=True)
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})


# --- Unresolved participant creation/validation ---------------------------


async def test_create_unresolved_participant_with_full_name(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "last_name": "Doe"}
    )

    assert participant.crm_contact_id is None
    assert participant.first_name == "Jane"
    assert participant.last_name == "Doe"


async def test_create_unresolved_participant_with_last_name_only(participant_service):
    """A surname-only historical record is a legitimate, meaningful
    identity -- not specifically first_name that's required."""
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"last_name": "Doe"}
    )
    assert participant.last_name == "Doe"


async def test_create_unresolved_participant_with_email_only(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"email": "guest@example.com"}
    )
    assert participant.email == "guest@example.com"


async def test_create_unresolved_participant_with_no_identity_at_all_is_rejected(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_participant(client.client_id, engagement.engagement_id, {})


async def test_create_unresolved_participant_with_only_whitespace_is_rejected(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_participant(
            client.client_id, engagement.engagement_id, {"first_name": "   ", "email": "   "}
        )


async def test_create_unresolved_participant_with_only_company_and_title_is_rejected(participant_service):
    """Company/title alone -- with no name or email -- is not a
    meaningful identity."""
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    with pytest.raises(ValueError):
        await service.create_engagement_participant(
            client.client_id, engagement.engagement_id, {"company": "Acme Co", "title": "VP"}
        )


# --- RSVP vs attendance independence, walk-in combination -----------------


async def test_rsvp_and_attendance_status_are_independent(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        {"first_name": "Jane", "rsvp_status": "confirmed", "attendance_status": "cancelled"},
    )
    assert participant.rsvp_status == "confirmed"
    assert participant.attendance_status == "cancelled"


async def test_walk_in_and_attended_is_a_valid_combination(participant_service):
    """The exact case the model must NOT treat as a contradiction --
    is_walk_in is provenance, not attendance."""
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id,
        engagement.engagement_id,
        {"first_name": "Jane", "is_walk_in": True, "attendance_status": "attended"},
    )
    assert participant.is_walk_in is True
    assert participant.attendance_status == "attended"


async def test_rsvp_status_and_attendance_status_default_to_null_not_guessed(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane"})
    assert participant.rsvp_status is None
    assert participant.attendance_status is None
    assert participant.is_walk_in is False


# --- All six role values ---------------------------------------------------


@pytest.mark.parametrize(
    "role", ["guest", "client", "host", "speaker_panelist", "astronomic_team", "other"]
)
async def test_create_participant_accepts_every_role_value(participant_service, role):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "role": role}
    )
    assert participant.role == role


async def test_create_participant_defaults_to_guest_role(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane"})
    assert participant.role == "guest"


# --- Duplicate prevention (resolved) ---------------------------------------


async def test_create_resolved_participant_duplicate_is_rejected(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(EngagementParticipantDuplicate):
        await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})


async def test_same_crm_contact_id_allowed_on_a_different_engagement(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement_a = await _make_client_and_engagement(service)
    engagement_b = await service.create_client_engagement(client.client_id, {"title": "Second Dinner", "engagement_type": "dinner"})

    await service.create_engagement_participant(client.client_id, engagement_a.engagement_id, {"crm_contact_id": "ethan-1"})
    participant_b = await service.create_engagement_participant(
        client.client_id, engagement_b.engagement_id, {"crm_contact_id": "ethan-1"}
    )
    assert participant_b.crm_contact_id == "ethan-1"


# --- Duplicate prevention when unresolved -> resolved ----------------------


async def test_linking_an_unresolved_participant_to_an_already_active_contact_is_rejected(participant_service):
    """The exact case this stage's own approved design calls out: Ethan
    already exists as a resolved participant; linking a SEPARATE
    unresolved row to Ethan must be a clean conflict, never two Ethan
    relationships."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})
    unresolved = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Ethan", "last_name": "W."}
    )

    with pytest.raises(EngagementParticipantDuplicate):
        await service.update_engagement_participant(
            client.client_id, engagement.engagement_id, unresolved.participant_id, {"crm_contact_id": "ethan-1"}
        )


async def test_linking_an_unresolved_participant_to_a_new_contact_succeeds_and_refreshes_snapshot(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    unresolved = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Ethan", "email": "guessed@example.com"}
    )

    linked = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, unresolved.participant_id, {"crm_contact_id": "ethan-1"}
    )

    assert linked.crm_contact_id == "ethan-1"
    assert linked.email == "ethan@hiveasmbld.example.com"  # refreshed from canonical, not the guessed one
    assert linked.company == "Hive ASMBLD"


async def test_linking_to_a_missing_or_archived_contact_is_rejected(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="archived-1", archived=True)
    client, engagement = await _make_client_and_engagement(service)
    unresolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane"})

    with pytest.raises(ValueError):
        await service.update_engagement_participant(
            client.client_id, engagement.engagement_id, unresolved.participant_id, {"crm_contact_id": "does-not-exist"}
        )
    with pytest.raises(ValueError):
        await service.update_engagement_participant(
            client.client_id, engagement.engagement_id, unresolved.participant_id, {"crm_contact_id": "archived-1"}
        )


# --- Resolved -> unresolved is explicitly rejected --------------------------


async def test_clearing_crm_contact_id_on_a_resolved_participant_is_rejected(participant_service):
    """The one correction the user asked for after the original STOP
    report: a resolved participant must never be turned back into an
    unresolved one by PATCHing crm_contact_id back to null."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    resolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(ValueError):
        await service.update_engagement_participant(
            client.client_id, engagement.engagement_id, resolved.participant_id, {"crm_contact_id": None}
        )

    # Rejected before any mutation -- the participant is still resolved.
    participants = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    unchanged = next(p for p in participants if p.participant_id == resolved.participant_id)
    assert unchanged.crm_contact_id == "ethan-1"


async def test_clearing_crm_contact_id_is_rejected_even_alongside_other_field_changes(participant_service):
    """The rejection must hold even when crm_contact_id=null is bundled
    with otherwise-legitimate field changes in the same PATCH -- no
    partial application of the unlink."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    resolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(ValueError):
        await service.update_engagement_participant(
            client.client_id,
            engagement.engagement_id,
            resolved.participant_id,
            {"crm_contact_id": None, "role": "host", "is_walk_in": True},
        )

    participants = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    unchanged = next(p for p in participants if p.participant_id == resolved.participant_id)
    assert unchanged.role == "guest"
    assert unchanged.is_walk_in is False


async def test_resolved_participant_can_still_be_relinked_to_a_different_contact(participant_service):
    """resolved -> resolved (a correction/relink) stays allowed for V1 --
    only resolved -> unresolved is closed off. The snapshot refreshes
    from the NEW Contact and the same duplicate check applies."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan", email="ethan@hiveasmbld.example.com")
    await _seed_crm_contact(crm_contact_store, crm_contact_id="priya-1", first_name="Priya", email="priya@hiveasmbld.example.com")
    client, engagement = await _make_client_and_engagement(service)
    resolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    relinked = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, resolved.participant_id, {"crm_contact_id": "priya-1"}
    )

    assert relinked.crm_contact_id == "priya-1"
    assert relinked.first_name == "Priya"
    assert relinked.email == "priya@hiveasmbld.example.com"


async def test_relinking_a_resolved_participant_to_an_already_active_contact_is_rejected(participant_service):
    """The relink path goes through the SAME duplicate protection as
    everything else -- relinking Participant A to a Contact already
    actively linked as Participant B on this Engagement must conflict."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan")
    await _seed_crm_contact(crm_contact_store, crm_contact_id="priya-1", first_name="Priya")
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "priya-1"})
    resolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(EngagementParticipantDuplicate):
        await service.update_engagement_participant(
            client.client_id, engagement.engagement_id, resolved.participant_id, {"crm_contact_id": "priya-1"}
        )


async def test_patch_with_no_crm_contact_id_key_at_all_leaves_a_resolved_participant_untouched(participant_service):
    """Omitting crm_contact_id from the patch entirely (the normal case
    for every non-identity edit) must never be confused with explicitly
    clearing it -- exclude_unset semantics, not "absent means null"."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    resolved = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})

    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, resolved.participant_id, {"role": "host"}
    )

    assert updated.crm_contact_id == "ethan-1"
    assert updated.role == "host"


# --- Unresolved records are never fuzzy-deduped -----------------------------


async def test_two_unresolved_participants_with_the_same_name_are_both_allowed(participant_service):
    """No fuzzy matching -- two "Jane Doe" entries might be the same
    person or two different people; Stage 1G never guesses."""
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    first = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane", "last_name": "Doe"})
    second = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane", "last_name": "Doe"})

    assert first.participant_id != second.participant_id


# --- Cross-client / cross-engagement isolation ------------------------------


async def test_participant_requested_through_wrong_client_is_not_found(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client_a, engagement_a = await _make_client_and_engagement(service)
    client_b = await service.create_client({"name": "Other Co"})
    participant = await service.create_engagement_participant(client_a.client_id, engagement_a.engagement_id, {"crm_contact_id": "ethan-1"})

    with pytest.raises(EngagementNotFound):
        await service.list_engagement_participants(client_b.client_id, engagement_a.engagement_id)
    with pytest.raises(EngagementNotFound):
        await service.update_engagement_participant(client_b.client_id, engagement_a.engagement_id, participant.participant_id, {"role": "host"})


async def test_participant_does_not_leak_across_two_engagements_on_the_same_client(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement_a = await _make_client_and_engagement(service)
    engagement_b = await service.create_client_engagement(client.client_id, {"title": "Second Dinner", "engagement_type": "dinner"})
    participant = await service.create_engagement_participant(client.client_id, engagement_a.engagement_id, {"first_name": "Jane"})

    with pytest.raises(EngagementParticipantNotFound):
        await service.update_engagement_participant(client.client_id, engagement_b.engagement_id, participant.participant_id, {"role": "host"})

    participants_a = await service.list_engagement_participants(client.client_id, engagement_a.engagement_id)
    participants_b = await service.list_engagement_participants(client.client_id, engagement_b.engagement_id)
    assert len(participants_a) == 1
    assert len(participants_b) == 0


# --- Archive / restore -------------------------------------------------


async def test_archive_and_restore_participant_via_patch(participant_service):
    service, activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane"})

    archived = await service.update_engagement_participant(client.client_id, engagement.engagement_id, participant.participant_id, {"archived": True})
    assert archived.archived is True
    restored = await service.update_engagement_participant(client.client_id, engagement.engagement_id, participant.participant_id, {"archived": False})
    assert restored.archived is False

    events = [e.event_type for e in (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items]
    assert "engagement_participant.archived" in events
    assert "engagement_participant.restored" in events


async def test_archiving_a_resolved_participant_still_blocks_a_new_duplicate(participant_service):
    """Approved design: re-adding someone after archiving means
    restoring the existing row, never creating a new one."""
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})
    await service.update_engagement_participant(client.client_id, engagement.engagement_id, participant.participant_id, {"archived": True})

    with pytest.raises(EngagementParticipantDuplicate):
        await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1"})


async def test_no_hard_delete_of_participant(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"first_name": "Jane"})
    await service.update_engagement_participant(client.client_id, engagement.engagement_id, participant.participant_id, {"archived": True})

    assert not hasattr(store, "delete")
    assert await store.get(participant.participant_id) is not None


# --- Activity Log privacy ---------------------------------------------------


async def test_participant_activity_log_never_includes_email_or_contact_details(participant_service):
    service, activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
    )

    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    created_event = next(e for e in page.items if e.event_type == "engagement_participant.created")
    assert created_event.metadata == {"client_id": client.client_id, "engagement_id": engagement.engagement_id}
    assert "jane@example.com" not in created_event.summary
    assert "jane@example.com" not in (created_event.entity_name or "")
    assert "jane@example.com" not in str(created_event.metadata)


async def test_participant_activity_log_entity_name_never_falls_back_to_email(participant_service):
    """An email-only unresolved participant must not have their email
    address end up as entity_name either."""
    service, activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(client.client_id, engagement.engagement_id, {"email": "jane@example.com"})

    page = await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)
    created_event = next(e for e in page.items if e.event_type == "engagement_participant.created")
    assert created_event.entity_name == "Unnamed participant"


# --- No Luma behavior ---------------------------------------------------


async def test_source_is_always_manual_regardless_of_what_the_caller_sends(participant_service):
    """`source` is entirely server-owned -- Stage 1G creates ONLY MANUAL
    records; a caller cannot smuggle in LUMA via the fields dict."""
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "source": "luma"}
    )
    assert participant.source == "manual"


# --- No automatic Closeout mutation -----------------------------------------


async def test_creating_participants_never_mutates_the_engagement_closeout(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    closeout = await service.create_engagement_closeout(
        client.client_id, engagement.engagement_id, {"confirmed_guest_count": 24, "attended_count": 24}
    )

    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "attendance_status": "attended"}
    )

    refreshed_closeout = await service.get_engagement_closeout(client.client_id, engagement.engagement_id)
    assert refreshed_closeout.confirmed_guest_count == 24
    assert refreshed_closeout.attended_count == 24
    assert refreshed_closeout.updated_at == closeout.updated_at
