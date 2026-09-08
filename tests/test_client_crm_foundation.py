"""
Client CRM -- Stage 1A foundation tests (2026-09-07).

Stage 1A is deliberately execution-inert: it adds durable schema (Client,
ClientContact, Engagement, ClientNote) and persistence only. No API route,
service, or frontend reads or writes any of this yet -- see each model's
own docstring in app/models/client_crm.py for the full scope rationale.
Deal/Pipeline is explicitly out of scope for this stage.

This file proves, for each of the 4 entities: JSON round-trip via the
model itself, create/get/save/list_for_x against BOTH the Memory and
SQLite store implementations (parity), archive-via-save behavior,
association by client_id (and, where applicable, crm_contact_id/
engagement_id), multiple-rows-per-parent, and created_at-ascending
ordering -- matching the exact same testing conventions established by
test_mail_trigger_foundation.py (tmp_path-backed real sqlite files, no
conftest.py, each test file redeclares its own fixtures/helpers).
"""

from datetime import date, datetime, timezone

import pytest
import pytest_asyncio

from app.models.client_crm import (
    Client,
    ClientContact,
    ClientNote,
    ClientNoteType,
    ClientRelationshipClassification,
    ClientStatus,
    DinnerType,
    Engagement,
    EngagementContractStatus,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
)
from app.repositories.client_contact_store import ClientContactNotFoundError, MemoryClientContactStore
from app.repositories.client_note_store import ClientNoteNotFoundError, MemoryClientNoteStore
from app.repositories.client_store import ClientNotFoundError, MemoryClientStore
from app.repositories.engagement_store import EngagementNotFoundError, MemoryEngagementStore
from app.repositories.sqlite_client_contact_store import SQLiteClientContactStore
from app.repositories.sqlite_client_note_store import SQLiteClientNoteStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.repositories.sqlite_engagement_store import SQLiteEngagementStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)
EVEN_LATER = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)


def _client(client_id="c1", name="Hive ASMBLD", created_at=NOW, updated_at=NOW, **overrides) -> Client:
    return Client(client_id=client_id, name=name, created_at=created_at, updated_at=updated_at, **overrides)


def _contact(client_contact_id="cc1", client_id="c1", created_at=NOW, updated_at=NOW, **overrides) -> ClientContact:
    return ClientContact(
        client_contact_id=client_contact_id, client_id=client_id, created_at=created_at, updated_at=updated_at, **overrides
    )


def _engagement(engagement_id="e1", client_id="c1", created_at=NOW, updated_at=NOW, **overrides) -> Engagement:
    return Engagement(
        engagement_id=engagement_id,
        client_id=client_id,
        title=overrides.pop("title", "SF Investor Dinner"),
        engagement_type=overrides.pop("engagement_type", EngagementType.DINNER),
        created_at=created_at,
        updated_at=updated_at,
        **overrides,
    )


def _note(client_note_id="n1", client_id="c1", occurred_at=NOW, created_at=NOW, updated_at=NOW, **overrides) -> ClientNote:
    return ClientNote(
        client_note_id=client_note_id,
        client_id=client_id,
        body=overrides.pop("body", "Great energy at the dinner."),
        occurred_at=occurred_at,
        created_at=created_at,
        updated_at=updated_at,
        **overrides,
    )


@pytest_asyncio.fixture
async def sqlite_client_store(tmp_path):
    s = SQLiteClientStore(str(tmp_path / "clients.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_client_contact_store(tmp_path):
    s = SQLiteClientContactStore(str(tmp_path / "client_contacts.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_engagement_store(tmp_path):
    s = SQLiteEngagementStore(str(tmp_path / "engagements.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_client_note_store(tmp_path):
    s = SQLiteClientNoteStore(str(tmp_path / "client_notes.db"))
    await s.connect()
    yield s
    await s.close()


# =====================================================================
# Model validation / JSON round-trip
# =====================================================================


def test_client_json_round_trip_preserves_every_field():
    client = _client(
        website="https://hiveasmbld.com",
        industry="Venture",
        status=ClientStatus.ACTIVE,
        relationship_classification=ClientRelationshipClassification.OPPORTUNITY,
        owner="Chris",
        next_action="Schedule Q1 check-in",
        next_action_due=date(2027, 1, 15),
        archived=False,
    )
    restored = Client.model_validate_json(client.model_dump_json())
    assert restored == client


def test_client_relationship_classification_defaults_to_unassessed():
    client = _client()
    assert client.relationship_classification is None
    assert client.status == ClientStatus.ACTIVE


def test_client_contact_json_round_trip_preserves_every_field():
    contact = _contact(
        crm_contact_id="crm-1",
        first_name="Sid",
        last_name="Atkinson",
        email="sid@example.com",
        phone="+1-555-0100",
        title="VP of BD",
        is_primary_contact=True,
        is_decision_maker=True,
        role_notes="Introduced us to the CFO.",
    )
    restored = ClientContact.model_validate_json(contact.model_dump_json())
    assert restored == contact


def test_client_contact_crm_contact_id_defaults_to_none():
    contact = _contact()
    assert contact.crm_contact_id is None


def test_engagement_json_round_trip_preserves_every_field():
    engagement = _engagement(
        dinner_type=DinnerType.INVESTOR_DINNER,
        engagement_date=date(2026, 9, 22),
        location="San Francisco",
        status=EngagementStatus.COMPLETED,
        owner="Chris",
        fee=15000.0,
        contract_status=EngagementContractStatus.SIGNED,
        contract_url="https://drive.example.com/contract",
        signed_date=date(2026, 9, 1),
        payment_status=EngagementPaymentStatus.PAID,
        luma_event_id="evt-123",
    )
    restored = Engagement.model_validate_json(engagement.model_dump_json())
    assert restored == engagement


def test_engagement_status_includes_confirmed():
    """Stage 1E: PLANNED -> CONFIRMED -> COMPLETED, with CANCELLED
    reachable from either PLANNED or CONFIRMED."""
    engagement = _engagement(status=EngagementStatus.CONFIRMED)
    assert engagement.status == EngagementStatus.CONFIRMED


def test_dinner_type_defaults_to_none():
    engagement = _engagement()
    assert engagement.dinner_type is None


def test_dinner_type_every_value_round_trips():
    for dinner_type in DinnerType:
        engagement = _engagement(dinner_type=dinner_type)
        assert Engagement.model_validate_json(engagement.model_dump_json()).dinner_type == dinner_type


def test_retired_supernova_galaxy_aurora_terminology_is_gone():
    """Stage 1E.1: Supernova/Galaxy/Aurora were retired internal program
    names -- DinnerType names the underlying dinner kind directly instead."""
    dinner_type_values = {member.value for member in DinnerType}
    assert dinner_type_values == {"investor_dinner", "fireside_dinner", "bizdev_dinner"}
    assert not any("supernova" in v or "galaxy" in v or "aurora" in v for v in dinner_type_values)


def test_engagement_owner_defaults_to_none():
    engagement = _engagement()
    assert engagement.owner is None


def test_engagement_defaults_to_planned_with_no_deal_relationship_field():
    engagement = _engagement()
    assert engagement.status == EngagementStatus.PLANNED
    assert engagement.contract_status == EngagementContractStatus.NOT_SENT
    assert engagement.payment_status == EngagementPaymentStatus.UNPAID
    # Deal/Pipeline is explicitly deferred (see this stage's own STOP
    # report) -- Engagement carries no deal_id or other Deal-shaped field
    # at all, not even a reserved one, until that domain is designed.
    assert "deal_id" not in Engagement.model_fields


def test_client_note_json_round_trip_preserves_every_field():
    note = _note(
        engagement_id="e1",
        note_type=ClientNoteType.DAY_3,
        created_by="Ria",
    )
    restored = ClientNote.model_validate_json(note.model_dump_json())
    assert restored == note


def test_client_note_has_no_structured_data_field():
    """Confirms the Stage 1A recommendation was actually followed --
    structured Day 3/30/90 business data does NOT live on ClientNote (see
    ClientNote's own docstring and this stage's STOP report)."""
    assert "structured_data" not in ClientNote.model_fields


def test_client_note_defaults_to_general_and_unlinked_to_an_engagement():
    note = _note()
    assert note.note_type == ClientNoteType.GENERAL
    assert note.engagement_id is None


# =====================================================================
# Client store -- Memory + SQLite parity
# =====================================================================


async def test_memory_client_create_get_save_archive_and_list_ordering():
    store = MemoryClientStore()
    await store.create(_client("c1", created_at=NOW))
    await store.create(_client("c2", name="Acme Co", created_at=LATER))

    assert (await store.get("c1")).name == "Hive ASMBLD"
    assert await store.get("c-missing") is None

    listed = await store.list()
    assert [c.client_id for c in listed] == ["c1", "c2"]  # created_at ascending

    updated = (await store.get("c1")).model_copy(update={"archived": True, "updated_at": LATER})
    await store.save(updated)
    assert (await store.get("c1")).archived is True

    with pytest.raises(ClientNotFoundError):
        await store.save(_client("c-missing"))


async def test_sqlite_client_create_get_save_archive_and_list_ordering(sqlite_client_store):
    store = sqlite_client_store
    await store.create(_client("c1", created_at=NOW))
    await store.create(_client("c2", name="Acme Co", created_at=LATER))

    assert (await store.get("c1")).name == "Hive ASMBLD"
    assert await store.get("c-missing") is None

    listed = await store.list()
    assert [c.client_id for c in listed] == ["c1", "c2"]

    updated = (await store.get("c1")).model_copy(update={"archived": True, "updated_at": LATER})
    await store.save(updated)
    assert (await store.get("c1")).archived is True

    with pytest.raises(ClientNotFoundError):
        await store.save(_client("c-missing"))


async def test_sqlite_client_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "clients_reconnect.db")
    store1 = SQLiteClientStore(db_path)
    await store1.connect()
    await store1.create(_client(relationship_classification=ClientRelationshipClassification.NURTURE))
    await store1.close()

    store2 = SQLiteClientStore(db_path)
    await store2.connect()
    restored = await store2.get("c1")
    assert restored is not None
    assert restored.relationship_classification == ClientRelationshipClassification.NURTURE
    await store2.close()


# =====================================================================
# ClientContact store -- Memory + SQLite parity
# =====================================================================


async def test_memory_client_contact_multiple_per_client_and_ordering():
    store = MemoryClientContactStore()
    await store.create(_contact("cc1", "c1", first_name="Sid", created_at=NOW))
    await store.create(_contact("cc2", "c1", first_name="Jamie", created_at=LATER))
    await store.create(_contact("cc3", "c2", first_name="Other Client's Contact", created_at=NOW))

    for_c1 = await store.list_for_client("c1")
    assert [c.client_contact_id for c in for_c1] == ["cc1", "cc2"]  # created_at ascending, scoped to c1 only

    assert await store.list_for_client("c-missing") == []


async def test_sqlite_client_contact_multiple_per_client_and_ordering(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create(_contact("cc1", "c1", first_name="Sid", created_at=NOW))
    await store.create(_contact("cc2", "c1", first_name="Jamie", created_at=LATER))
    await store.create(_contact("cc3", "c2", first_name="Other Client's Contact", created_at=NOW))

    for_c1 = await store.list_for_client("c1")
    assert [c.client_contact_id for c in for_c1] == ["cc1", "cc2"]
    assert await store.list_for_client("c-missing") == []


async def test_memory_client_contact_optional_crm_contact_id_and_lookup():
    store = MemoryClientContactStore()
    await store.create(_contact("cc1", "c1", crm_contact_id="crm-1"))
    await store.create(_contact("cc2", "c2", crm_contact_id=None))

    assert await store.list_for_crm_contact("crm-1") == [await store.get("cc1")]
    assert await store.list_for_crm_contact("crm-missing") == []
    assert (await store.get("cc2")).crm_contact_id is None


async def test_sqlite_client_contact_optional_crm_contact_id_and_lookup(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create(_contact("cc1", "c1", crm_contact_id="crm-1"))
    await store.create(_contact("cc2", "c2", crm_contact_id=None))

    assert await store.list_for_crm_contact("crm-1") == [await store.get("cc1")]
    assert await store.list_for_crm_contact("crm-missing") == []
    assert (await store.get("cc2")).crm_contact_id is None


async def test_memory_client_contact_save_and_archive():
    store = MemoryClientContactStore()
    await store.create(_contact())
    updated = (await store.get("cc1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("cc1")).archived is True
    with pytest.raises(ClientContactNotFoundError):
        await store.save(_contact("cc-missing"))


async def test_sqlite_client_contact_save_and_archive(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create(_contact())
    updated = (await store.get("cc1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("cc1")).archived is True
    with pytest.raises(ClientContactNotFoundError):
        await store.save(_contact("cc-missing"))


# =====================================================================
# ClientContact store -- atomic Primary Contact invariant (Stage 1D,
# 2026-09-07). See ClientContactStore.create_as_primary/set_primary's own
# docstrings for the invariant these prove: at most one active Primary
# Contact per client_id, with no window where two rows are simultaneously
# primary.
# =====================================================================


async def test_memory_create_as_primary_demotes_the_existing_primary():
    store = MemoryClientContactStore()
    await store.create_as_primary(_contact("cc1", "c1", is_primary_contact=True))
    await store.create_as_primary(_contact("cc2", "c1", is_primary_contact=True))

    assert (await store.get("cc1")).is_primary_contact is False
    assert (await store.get("cc2")).is_primary_contact is True


async def test_sqlite_create_as_primary_demotes_the_existing_primary(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create_as_primary(_contact("cc1", "c1", is_primary_contact=True))
    await store.create_as_primary(_contact("cc2", "c1", is_primary_contact=True))

    assert (await store.get("cc1")).is_primary_contact is False
    assert (await store.get("cc2")).is_primary_contact is True


async def test_create_as_primary_never_demotes_a_different_clients_primary():
    store = MemoryClientContactStore()
    await store.create_as_primary(_contact("cc1", "c1", is_primary_contact=True))
    await store.create_as_primary(_contact("cc2", "c2", is_primary_contact=True))

    assert (await store.get("cc1")).is_primary_contact is True
    assert (await store.get("cc2")).is_primary_contact is True


async def test_create_as_primary_never_demotes_an_already_archived_contact_again():
    """Archived rows are excluded from the "clear other primaries" scan --
    there's nothing wrong with leaving an archived row's stale
    is_primary_contact value alone; the service layer never treats an
    archived contact as primary regardless of this flag's value."""
    store = MemoryClientContactStore()
    await store.create(_contact("cc1", "c1", is_primary_contact=True, archived=True))
    await store.create_as_primary(_contact("cc2", "c1", is_primary_contact=True))

    assert (await store.get("cc1")).is_primary_contact is True  # untouched, but archived
    assert (await store.get("cc2")).is_primary_contact is True


async def test_memory_set_primary_switches_and_demotes():
    store = MemoryClientContactStore()
    await store.create(_contact("cc1", "c1", is_primary_contact=True))
    await store.create(_contact("cc2", "c1", is_primary_contact=False))

    updated = await store.set_primary("c1", "cc2")

    assert updated.is_primary_contact is True
    assert (await store.get("cc1")).is_primary_contact is False
    assert (await store.get("cc2")).is_primary_contact is True


async def test_sqlite_set_primary_switches_and_demotes(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create(_contact("cc1", "c1", is_primary_contact=True))
    await store.create(_contact("cc2", "c1", is_primary_contact=False))

    updated = await store.set_primary("c1", "cc2")

    assert updated.is_primary_contact is True
    assert (await store.get("cc1")).is_primary_contact is False
    assert (await store.get("cc2")).is_primary_contact is True


async def test_memory_set_primary_missing_contact_raises():
    store = MemoryClientContactStore()
    with pytest.raises(ClientContactNotFoundError):
        await store.set_primary("c1", "does-not-exist")


async def test_sqlite_set_primary_missing_contact_raises(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    with pytest.raises(ClientContactNotFoundError):
        await store.set_primary("c1", "does-not-exist")


async def test_memory_set_primary_rejects_a_contact_from_a_different_client():
    store = MemoryClientContactStore()
    await store.create(_contact("cc1", "c1"))
    with pytest.raises(ClientContactNotFoundError):
        await store.set_primary("c-other", "cc1")


async def test_sqlite_set_primary_rejects_a_contact_from_a_different_client(sqlite_client_contact_store):
    store = sqlite_client_contact_store
    await store.create(_contact("cc1", "c1"))
    with pytest.raises(ClientContactNotFoundError):
        await store.set_primary("c-other", "cc1")


# =====================================================================
# Engagement store -- Memory + SQLite parity
# =====================================================================


async def test_memory_engagement_multiple_per_client_and_ordering():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", created_at=NOW))
    await store.create(_engagement("e2", "c1", title="Follow-up dinner", created_at=LATER))
    await store.create(_engagement("e3", "c2", created_at=NOW))

    for_c1 = await store.list_for_client("c1")
    assert [e.engagement_id for e in for_c1] == ["e1", "e2"]
    assert await store.list_for_client("c-missing") == []


async def test_sqlite_engagement_multiple_per_client_and_ordering(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", created_at=NOW))
    await store.create(_engagement("e2", "c1", title="Follow-up dinner", created_at=LATER))
    await store.create(_engagement("e3", "c2", created_at=NOW))

    for_c1 = await store.list_for_client("c1")
    assert [e.engagement_id for e in for_c1] == ["e1", "e2"]
    assert await store.list_for_client("c-missing") == []


async def test_memory_engagement_save_and_archive():
    store = MemoryEngagementStore()
    await store.create(_engagement())
    updated = (await store.get("e1")).model_copy(update={"archived": True, "status": EngagementStatus.CANCELLED})
    await store.save(updated)
    fresh = await store.get("e1")
    assert fresh.archived is True
    assert fresh.status == EngagementStatus.CANCELLED
    with pytest.raises(EngagementNotFoundError):
        await store.save(_engagement("e-missing"))


async def test_sqlite_engagement_save_and_archive(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement())
    updated = (await store.get("e1")).model_copy(update={"archived": True, "status": EngagementStatus.CANCELLED})
    await store.save(updated)
    fresh = await store.get("e1")
    assert fresh.archived is True
    assert fresh.status == EngagementStatus.CANCELLED
    with pytest.raises(EngagementNotFoundError):
        await store.save(_engagement("e-missing"))


# =====================================================================
# ClientNote store -- Memory + SQLite parity
# =====================================================================


async def test_memory_client_note_general_and_engagement_linked_and_ordering():
    store = MemoryClientNoteStore()
    await store.create(_note("n1", "c1", occurred_at=NOW, created_at=NOW))  # general
    await store.create(
        _note("n2", "c1", engagement_id="e1", note_type=ClientNoteType.DAY_3, occurred_at=LATER, created_at=LATER)
    )
    await store.create(_note("n3", "c2", occurred_at=NOW, created_at=NOW))  # different client

    for_c1 = await store.list_for_client("c1")
    assert [n.client_note_id for n in for_c1] == ["n1", "n2"]

    for_e1 = await store.list_for_engagement("e1")
    assert [n.client_note_id for n in for_e1] == ["n2"]

    assert await store.list_for_engagement("e-missing") == []
    assert (await store.get("n1")).engagement_id is None


async def test_sqlite_client_note_general_and_engagement_linked_and_ordering(sqlite_client_note_store):
    store = sqlite_client_note_store
    await store.create(_note("n1", "c1", occurred_at=NOW, created_at=NOW))
    await store.create(
        _note("n2", "c1", engagement_id="e1", note_type=ClientNoteType.DAY_3, occurred_at=LATER, created_at=LATER)
    )
    await store.create(_note("n3", "c2", occurred_at=NOW, created_at=NOW))

    for_c1 = await store.list_for_client("c1")
    assert [n.client_note_id for n in for_c1] == ["n1", "n2"]

    for_e1 = await store.list_for_engagement("e1")
    assert [n.client_note_id for n in for_e1] == ["n2"]

    assert await store.list_for_engagement("e-missing") == []
    assert (await store.get("n1")).engagement_id is None


async def test_memory_client_note_save_and_archive():
    store = MemoryClientNoteStore()
    await store.create(_note())
    updated = (await store.get("n1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("n1")).archived is True
    with pytest.raises(ClientNoteNotFoundError):
        await store.save(_note("n-missing"))


async def test_sqlite_client_note_save_and_archive(sqlite_client_note_store):
    store = sqlite_client_note_store
    await store.create(_note())
    updated = (await store.get("n1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("n1")).archived is True
    with pytest.raises(ClientNoteNotFoundError):
        await store.save(_note("n-missing"))


async def test_client_note_occurred_at_may_differ_from_created_at_for_a_backfilled_note():
    backfilled = _note(occurred_at=NOW, created_at=EVEN_LATER)
    assert backfilled.occurred_at != backfilled.created_at
    restored = ClientNote.model_validate_json(backfilled.model_dump_json())
    assert restored.occurred_at == NOW
    assert restored.created_at == EVEN_LATER
