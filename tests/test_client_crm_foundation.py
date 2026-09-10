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

import aiosqlite
import pytest
import pytest_asyncio

from pydantic import ValidationError

from app.models.client_crm import (
    Client,
    ClientContact,
    ClientNote,
    ClientNoteType,
    ClientRelationshipClassification,
    ClientStatus,
    ClientTouchpoint,
    ContactType,
    DinnerType,
    Engagement,
    EngagementCloseout,
    EngagementContractStatus,
    EngagementParticipant,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
    ParticipantAttendanceStatus,
    ParticipantRole,
    ParticipantRsvpStatus,
    ParticipantSource,
)
from app.repositories.client_contact_store import ClientContactNotFoundError, MemoryClientContactStore
from app.repositories.client_note_store import ClientNoteNotFoundError, MemoryClientNoteStore
from app.repositories.client_store import ClientNotFoundError, MemoryClientStore
from app.repositories.client_touchpoint_store import ClientTouchpointNotFoundError, MemoryClientTouchpointStore
from app.repositories.engagement_closeout_store import EngagementCloseoutNotFoundError, MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import (
    EngagementParticipantDuplicateError,
    EngagementParticipantNotFoundError,
    MemoryEngagementParticipantStore,
)
from app.repositories.engagement_store import EngagementLumaEventAlreadyLinkedError, EngagementNotFoundError, MemoryEngagementStore
from app.repositories.sqlite_client_contact_store import SQLiteClientContactStore
from app.repositories.sqlite_client_note_store import SQLiteClientNoteStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.repositories.sqlite_client_touchpoint_store import SQLiteClientTouchpointStore
from app.repositories.sqlite_engagement_closeout_store import SQLiteEngagementCloseoutStore
from app.repositories.sqlite_engagement_participant_store import SQLiteEngagementParticipantStore
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


def _closeout(closeout_id="co1", engagement_id="e1", client_id="c1", created_at=NOW, updated_at=NOW, **overrides) -> EngagementCloseout:
    return EngagementCloseout(
        closeout_id=closeout_id,
        engagement_id=engagement_id,
        client_id=client_id,
        created_at=created_at,
        updated_at=updated_at,
        **overrides,
    )


def _participant(
    participant_id="p1", engagement_id="e1", client_id="c1", created_at=NOW, updated_at=NOW, **overrides
) -> EngagementParticipant:
    # The model itself has no identity-completeness constraint (that's a
    # service-layer business rule, not a schema constraint -- same
    # convention as Client.name's own blank-check) -- first_name="Jane"
    # here is purely a readable default for tests, not a requirement.
    overrides.setdefault("first_name", "Jane")
    return EngagementParticipant(
        participant_id=participant_id,
        engagement_id=engagement_id,
        client_id=client_id,
        created_at=created_at,
        updated_at=updated_at,
        **overrides,
    )


def _touchpoint(
    touchpoint_id="t1", client_id="c1", occurred_at=NOW, created_at=NOW, updated_at=NOW, **overrides
) -> ClientTouchpoint:
    overrides.setdefault("contact_type", ContactType.EMAIL)
    overrides.setdefault("contacted_by", "Ria")
    return ClientTouchpoint(
        touchpoint_id=touchpoint_id,
        client_id=client_id,
        occurred_at=occurred_at,
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
async def sqlite_engagement_closeout_store(tmp_path):
    s = SQLiteEngagementCloseoutStore(str(tmp_path / "engagement_closeouts.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_engagement_participant_store(tmp_path):
    s = SQLiteEngagementParticipantStore(str(tmp_path / "engagement_participants.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_client_note_store(tmp_path):
    s = SQLiteClientNoteStore(str(tmp_path / "client_notes.db"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def sqlite_client_touchpoint_store(tmp_path):
    s = SQLiteClientTouchpointStore(str(tmp_path / "client_touchpoints.db"))
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
    assert dinner_type_values == {"investor_dinner", "fireside_dinner", "bizdev_dinner", "donor_dinner", "custom_dinner"}
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


# =====================================================================
# EngagementCloseout -- Client CRM Stage 1F (2026-09-08)
# =====================================================================


def test_engagement_closeout_json_round_trip_preserves_every_field():
    closeout = _closeout(
        confirmed_guest_count=12,
        attended_count=10,
        no_show_count=2,
        cancelled_count=0,
        unexpected_attendee_count=1,
        guest_quality="Strong",
        dinner_dynamics="Energetic",
        initial_client_experience="Thrilled",
        immediate_outcomes="Two intros",
        notable_signals="Co-investing interest",
        issues="Ran late",
        referrals="One referral offered",
        future_opportunities="Possible Q1 follow-on",
        internal_notes="Reuse venue",
        completed_at=NOW,
        completed_by="Chris",
    )
    restored = EngagementCloseout.model_validate_json(closeout.model_dump_json())
    assert restored == closeout


def test_engagement_closeout_counts_default_to_none_not_zero():
    closeout = _closeout()
    assert closeout.confirmed_guest_count is None
    assert closeout.attended_count is None
    assert closeout.no_show_count is None
    assert closeout.cancelled_count is None
    assert closeout.unexpected_attendee_count is None


def test_engagement_closeout_distinguishes_explicit_zero_from_null():
    closeout = _closeout(no_show_count=0, cancelled_count=None)
    assert closeout.no_show_count == 0
    assert closeout.cancelled_count is None
    restored = EngagementCloseout.model_validate_json(closeout.model_dump_json())
    assert restored.no_show_count == 0
    assert restored.cancelled_count is None


@pytest.mark.parametrize(
    "field",
    ["confirmed_guest_count", "attended_count", "no_show_count", "cancelled_count", "unexpected_attendee_count"],
)
def test_engagement_closeout_rejects_a_negative_count_for_every_count_field(field):
    with pytest.raises(ValidationError):
        _closeout(**{field: -1})


def test_engagement_closeout_defaults_not_completed_and_not_archived():
    closeout = _closeout()
    assert closeout.completed_at is None
    assert closeout.completed_by is None
    assert closeout.archived is False


def test_engagement_closeout_has_no_status_enum_field():
    """No dedicated Closeout status enum was invented -- see this
    model's own docstring and the Stage 1F investigation report:
    completed_at (nullable timestamp) already answers "recorded or not"
    without a redundant enum."""
    assert "status" not in EngagementCloseout.model_fields


# =====================================================================
# EngagementParticipant -- Client CRM Stage 1G (2026-09-08)
# =====================================================================


def test_engagement_participant_json_round_trip_preserves_every_field():
    participant = _participant(
        crm_contact_id="ethan-1",
        first_name="Ethan",
        last_name="Wong",
        email="ethan@hiveasmbld.example.com",
        title="Co-CEO",
        company="Hive ASMBLD",
        role=ParticipantRole.HOST,
        rsvp_status=ParticipantRsvpStatus.CONFIRMED,
        attendance_status=ParticipantAttendanceStatus.ATTENDED,
        is_walk_in=True,
    )
    restored = EngagementParticipant.model_validate_json(participant.model_dump_json())
    assert restored == participant


def test_engagement_participant_defaults():
    participant = _participant()
    assert participant.crm_contact_id is None
    assert participant.role == ParticipantRole.GUEST
    assert participant.rsvp_status is None
    assert participant.attendance_status is None
    assert participant.is_walk_in is False
    assert participant.source == ParticipantSource.MANUAL
    assert participant.archived is False


def test_engagement_participant_walk_in_and_attended_is_a_valid_combination():
    """The exact case this stage's own model must not treat as a
    contradiction -- is_walk_in is provenance, not attendance."""
    participant = _participant(is_walk_in=True, attendance_status=ParticipantAttendanceStatus.ATTENDED)
    assert participant.is_walk_in is True
    assert participant.attendance_status == ParticipantAttendanceStatus.ATTENDED


def test_engagement_participant_rsvp_and_attendance_are_independent_fields():
    participant = _participant(rsvp_status=ParticipantRsvpStatus.CONFIRMED, attendance_status=ParticipantAttendanceStatus.CANCELLED)
    assert participant.rsvp_status == ParticipantRsvpStatus.CONFIRMED
    assert participant.attendance_status == ParticipantAttendanceStatus.CANCELLED


@pytest.mark.parametrize(
    "role", [ParticipantRole.GUEST, ParticipantRole.CLIENT, ParticipantRole.HOST,
             ParticipantRole.SPEAKER_PANELIST, ParticipantRole.ASTRONOMIC_TEAM, ParticipantRole.OTHER]
)
def test_engagement_participant_every_role_value_round_trips(role):
    participant = _participant(role=role)
    assert EngagementParticipant.model_validate_json(participant.model_dump_json()).role == role


def test_participant_role_client_and_host_are_distinct_values():
    """Stage 1G's own approved refinement: Client and Host are NOT
    combined into one value."""
    assert ParticipantRole.CLIENT != ParticipantRole.HOST
    assert {ParticipantRole.CLIENT.value, ParticipantRole.HOST.value} == {"client", "host"}


def test_participant_role_has_no_dedicated_moderator_value():
    """Moderator stays folded into SPEAKER_PANELIST for V1 -- no concrete
    need for the distinction was found."""
    assert {member.value for member in ParticipantRole} == {
        "guest", "client", "host", "speaker_panelist", "astronomic_team", "other",
    }


def test_participant_source_enum_has_manual_and_luma_but_model_defaults_to_manual():
    assert {member.value for member in ParticipantSource} == {"manual", "luma"}
    assert _participant().source == ParticipantSource.MANUAL


def test_engagement_participant_has_no_luma_guest_id_field():
    """Stage 1G's own approved refinement: no reserved Luma identifier is
    added until the Luma relationship is explicitly designed."""
    assert "luma_guest_id" not in EngagementParticipant.model_fields


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
# Engagement.list_for_clients -- bulk cross-client lookup, Stage 2C
# =====================================================================


async def test_memory_engagement_list_for_clients_groups_correctly_and_excludes_unrelated():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c2"))
    await store.create(_engagement("e3", "c3"))  # unrelated -- not in the requested id list

    found = await store.list_for_clients(["c1", "c2"])
    assert {e.engagement_id for e in found} == {"e1", "e2"}


async def test_sqlite_engagement_list_for_clients_groups_correctly_and_excludes_unrelated(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c2"))
    await store.create(_engagement("e3", "c3"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {e.engagement_id for e in found} == {"e1", "e2"}


async def test_memory_engagement_list_for_clients_multiple_engagements_per_client():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))
    await store.create(_engagement("e3", "c2"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {e.engagement_id for e in found} == {"e1", "e2", "e3"}


async def test_sqlite_engagement_list_for_clients_multiple_engagements_per_client(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))
    await store.create(_engagement("e3", "c2"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {e.engagement_id for e in found} == {"e1", "e2", "e3"}


async def test_memory_engagement_list_for_clients_empty_id_list_returns_empty():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1"))
    assert await store.list_for_clients([]) == []


async def test_sqlite_engagement_list_for_clients_empty_id_list_returns_empty(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    assert await store.list_for_clients([]) == []


async def test_sqlite_engagement_list_for_clients_unmatched_id_returns_empty(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    assert await store.list_for_clients(["c-does-not-exist"]) == []


# =====================================================================
# Engagement <-> Luma event link -- Client CRM Stage 1H-A (2026-09-09)
# =====================================================================


async def test_memory_get_by_luma_event_id():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_luma_event_id("luma-123")
    assert found.engagement_id == "e1"
    assert await store.get_by_luma_event_id("does-not-exist") is None


async def test_sqlite_get_by_luma_event_id(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    await store.create(_engagement("e2", "c1"))

    found = await store.get_by_luma_event_id("luma-123")
    assert found.engagement_id == "e1"
    assert await store.get_by_luma_event_id("does-not-exist") is None


async def test_memory_store_rejects_a_second_engagement_linked_to_the_same_luma_event():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.create(_engagement("e2", "c1", luma_event_id="luma-123"))


async def test_sqlite_store_rejects_a_second_engagement_linked_to_the_same_luma_event(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.create(_engagement("e2", "c1", luma_event_id="luma-123"))
    # And the rejected write left no trace -- e2 was never persisted.
    assert await store.get("e2") is None


async def test_sqlite_store_rejects_an_update_that_would_create_a_duplicate_luma_link(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    await store.create(_engagement("e2", "c1"))

    e2 = (await store.get("e2")).model_copy(update={"luma_event_id": "luma-123"})
    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.save(e2)
    # And the failed save left e2's own row untouched.
    assert (await store.get("e2")).luma_event_id is None


async def test_sqlite_store_two_null_luma_event_ids_never_conflict(sqlite_engagement_store):
    """The partial unique index excludes NULL rows entirely -- unlimited
    unlinked Engagements must coexist, same "NULLs excluded" behavior
    already proven for EngagementParticipant.crm_contact_id."""
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))  # must not raise
    assert await store.get("e2") is not None


async def test_sqlite_store_archiving_an_engagement_does_not_free_its_luma_link(sqlite_engagement_store):
    """Archiving an Engagement must not silently free its luma_event_id
    for a different Engagement to claim -- same "the unique index doesn't
    distinguish archived from active" convention already established for
    EngagementParticipant.crm_contact_id."""
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    archived = (await store.get("e1")).model_copy(update={"archived": True})
    await store.save(archived)

    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.create(_engagement("e2", "c1", luma_event_id="luma-123"))


async def test_sqlite_store_relinking_an_engagement_to_a_new_luma_event_frees_the_old_one(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1", luma_event_id="luma-123"))
    relinked = (await store.get("e1")).model_copy(update={"luma_event_id": "luma-456"})
    await store.save(relinked)

    # e1's old luma_event_id is free again for a different Engagement.
    await store.create(_engagement("e2", "c1", luma_event_id="luma-123"))
    assert (await store.get("e2")).luma_event_id == "luma-123"


async def test_existing_engagements_survive_the_luma_event_id_column_migration(tmp_path):
    """Requirement: Engagement rows created before luma_event_id existed
    as a real column on this table (i.e. every row on production today --
    the field has been reserved-and-unwritten since Stage 1E) must still
    be readable, and the table still writable, after
    SQLiteEngagementStore.connect() runs its ALTER TABLE migration --
    proving the migration is additive, not destructive. Same convention as
    test_sqlite_lead_store.py's own
    test_existing_campaign_leads_survive_the_score_column_migration."""
    db_path = str(tmp_path / "pre_migration.db")

    # Simulate the OLD schema (before this change) with a real row in it,
    # written with raw SQL exactly like the pre-migration code would have.
    old_conn = await aiosqlite.connect(db_path)
    await old_conn.execute(
        """
        CREATE TABLE engagements (
            engagement_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL
        )
        """
    )
    pre_existing = _engagement("pre-existing-engagement", "pre-existing-client")
    await old_conn.execute(
        "INSERT INTO engagements (engagement_id, client_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
        (
            pre_existing.engagement_id,
            pre_existing.client_id,
            pre_existing.created_at.isoformat(),
            pre_existing.updated_at.isoformat(),
            pre_existing.model_dump_json(),
        ),
    )
    await old_conn.commit()
    await old_conn.close()

    # Now open it through the current store -- this is what happens on the
    # next app startup against a real, already-populated database.
    store = SQLiteEngagementStore(db_path)
    await store.connect()

    surviving = await store.get("pre-existing-engagement")
    assert surviving is not None
    assert surviving.luma_event_id is None  # not fabricated -- genuinely never recorded pre-migration

    # The table is still fully writable post-migration, including the new
    # column and its uniqueness constraint.
    await store.create(_engagement("new-engagement", "pre-existing-client", luma_event_id="luma-123"))
    assert (await store.get("new-engagement")).luma_event_id == "luma-123"
    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.create(_engagement("another-new-engagement", "pre-existing-client", luma_event_id="luma-123"))
    await store.close()


async def test_luma_event_id_column_migration_backfills_from_a_pre_existing_json_value(tmp_path):
    """Belt-and-suspenders: even though every real production row has
    luma_event_id unset today (see the module-level note above), the
    backfill itself must be correct for a row that DID happen to have one
    serialized into its JSON `data` blob before the column existed --
    proving the migration reads real data, not "safe because it's always
    empty."""
    db_path = str(tmp_path / "pre_migration_with_value.db")

    old_conn = await aiosqlite.connect(db_path)
    await old_conn.execute(
        """
        CREATE TABLE engagements (
            engagement_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL
        )
        """
    )
    pre_existing = _engagement("pre-existing-engagement", "pre-existing-client", luma_event_id="already-linked-in-json")
    await old_conn.execute(
        "INSERT INTO engagements (engagement_id, client_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
        (
            pre_existing.engagement_id,
            pre_existing.client_id,
            pre_existing.created_at.isoformat(),
            pre_existing.updated_at.isoformat(),
            pre_existing.model_dump_json(),
        ),
    )
    await old_conn.commit()
    await old_conn.close()

    store = SQLiteEngagementStore(db_path)
    await store.connect()

    assert (await store.get_by_luma_event_id("already-linked-in-json")).engagement_id == "pre-existing-engagement"
    # And the backfilled uniqueness constraint is live -- a second
    # Engagement can't claim the same (now-backfilled) luma_event_id.
    with pytest.raises(EngagementLumaEventAlreadyLinkedError):
        await store.create(_engagement("new-engagement", "pre-existing-client", luma_event_id="already-linked-in-json"))
    await store.close()


# =====================================================================
# EngagementCloseout store -- Memory + SQLite parity
# =====================================================================


async def test_memory_engagement_closeout_create_and_get_for_engagement():
    store = MemoryEngagementCloseoutStore()
    await store.create(_closeout("co1", "e1", "c1"))

    assert (await store.get_for_engagement("e1")).closeout_id == "co1"
    assert await store.get_for_engagement("e-missing") is None


async def test_sqlite_engagement_closeout_create_and_get_for_engagement(sqlite_engagement_closeout_store):
    store = sqlite_engagement_closeout_store
    await store.create(_closeout("co1", "e1", "c1"))

    assert (await store.get_for_engagement("e1")).closeout_id == "co1"
    assert await store.get_for_engagement("e-missing") is None


async def test_memory_engagement_closeout_only_matches_its_own_engagement():
    store = MemoryEngagementCloseoutStore()
    await store.create(_closeout("co1", "e1", "c1"))
    await store.create(_closeout("co2", "e2", "c1"))

    assert (await store.get_for_engagement("e1")).closeout_id == "co1"
    assert (await store.get_for_engagement("e2")).closeout_id == "co2"


async def test_sqlite_engagement_closeout_only_matches_its_own_engagement(sqlite_engagement_closeout_store):
    store = sqlite_engagement_closeout_store
    await store.create(_closeout("co1", "e1", "c1"))
    await store.create(_closeout("co2", "e2", "c1"))

    assert (await store.get_for_engagement("e1")).closeout_id == "co1"
    assert (await store.get_for_engagement("e2")).closeout_id == "co2"


async def test_memory_engagement_closeout_save_and_archive():
    store = MemoryEngagementCloseoutStore()
    await store.create(_closeout())
    updated = (await store.get("co1")).model_copy(update={"archived": True, "attended_count": 10})
    await store.save(updated)
    fresh = await store.get("co1")
    assert fresh.archived is True
    assert fresh.attended_count == 10
    with pytest.raises(EngagementCloseoutNotFoundError):
        await store.save(_closeout("co-missing"))


async def test_sqlite_engagement_closeout_save_and_archive(sqlite_engagement_closeout_store):
    store = sqlite_engagement_closeout_store
    await store.create(_closeout())
    updated = (await store.get("co1")).model_copy(update={"archived": True, "attended_count": 10})
    await store.save(updated)
    fresh = await store.get("co1")
    assert fresh.archived is True
    assert fresh.attended_count == 10
    with pytest.raises(EngagementCloseoutNotFoundError):
        await store.save(_closeout("co-missing"))


async def test_sqlite_engagement_closeout_preserves_null_vs_zero_across_a_real_roundtrip(sqlite_engagement_closeout_store):
    store = sqlite_engagement_closeout_store
    await store.create(_closeout(no_show_count=0, cancelled_count=None))
    fresh = await store.get("co1")
    assert fresh.no_show_count == 0
    assert fresh.cancelled_count is None


# =====================================================================
# EngagementParticipant store -- Memory + SQLite parity
# =====================================================================


async def test_memory_engagement_participant_create_and_list_for_engagement():
    store = MemoryEngagementParticipantStore()
    await store.create(_participant("p1", "e1", "c1"))
    await store.create(_participant("p2", "e1", "c1", first_name="Jane", crm_contact_id=None))
    await store.create(_participant("p3", "e2", "c1"))

    for_e1 = await store.list_for_engagement("e1")
    assert [p.participant_id for p in for_e1] == ["p1", "p2"]
    assert await store.list_for_engagement("e-missing") == []


async def test_sqlite_engagement_participant_create_and_list_for_engagement(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1"))
    await store.create(_participant("p2", "e1", "c1", first_name="Jane"))
    await store.create(_participant("p3", "e2", "c1"))

    for_e1 = await store.list_for_engagement("e1")
    assert [p.participant_id for p in for_e1] == ["p1", "p2"]
    assert await store.list_for_engagement("e-missing") == []


async def test_memory_engagement_participant_save_and_archive():
    store = MemoryEngagementParticipantStore()
    await store.create(_participant())
    updated = (await store.get("p1")).model_copy(update={"archived": True, "attendance_status": ParticipantAttendanceStatus.ATTENDED})
    await store.save(updated)
    fresh = await store.get("p1")
    assert fresh.archived is True
    assert fresh.attendance_status == ParticipantAttendanceStatus.ATTENDED
    with pytest.raises(EngagementParticipantNotFoundError):
        await store.save(_participant("p-missing"))


async def test_sqlite_engagement_participant_save_and_archive(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    await store.create(_participant())
    updated = (await store.get("p1")).model_copy(update={"archived": True, "attendance_status": ParticipantAttendanceStatus.ATTENDED})
    await store.save(updated)
    fresh = await store.get("p1")
    assert fresh.archived is True
    assert fresh.attendance_status == ParticipantAttendanceStatus.ATTENDED
    with pytest.raises(EngagementParticipantNotFoundError):
        await store.save(_participant("p-missing"))


# --- Duplicate prevention: the real SQLite partial unique index --------


async def test_memory_store_rejects_duplicate_active_crm_contact_on_same_engagement():
    store = MemoryEngagementParticipantStore()
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    with pytest.raises(EngagementParticipantDuplicateError):
        await store.create(_participant("p2", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))


async def test_sqlite_store_rejects_duplicate_active_crm_contact_on_same_engagement(sqlite_engagement_participant_store):
    """The real SQLite partial unique index, not just an application-level
    check -- confirms this stage's own empirical investigation holds
    through the actual store implementation, not only a standalone script."""
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    with pytest.raises(EngagementParticipantDuplicateError):
        await store.create(_participant("p2", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))


async def test_sqlite_store_allows_multiple_unresolved_participants_on_the_same_engagement(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", first_name="Jane", crm_contact_id=None))
    await store.create(_participant("p2", "e1", "c1", first_name="Jane", crm_contact_id=None))
    for_e1 = await store.list_for_engagement("e1")
    assert len(for_e1) == 2


async def test_sqlite_store_allows_same_crm_contact_on_a_different_engagement(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    await store.create(_participant("p2", "e2", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    assert (await store.get("p2")).crm_contact_id == "ethan-1"


async def test_sqlite_store_rejects_an_update_that_would_create_a_duplicate(sqlite_engagement_participant_store):
    """The exact "unresolved participant later linked to an already-active
    Contact" case -- caught on save(), not just create()."""
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    await store.create(_participant("p2", "e1", "c1", first_name="Ethan", crm_contact_id=None))

    unresolved = await store.get("p2")
    relinked = unresolved.model_copy(update={"crm_contact_id": "ethan-1"})
    with pytest.raises(EngagementParticipantDuplicateError):
        await store.save(relinked)


async def test_sqlite_store_archiving_a_participant_does_not_free_the_unique_slot(sqlite_engagement_participant_store):
    """Approved design: restore an archived participant, never create a
    new one for the same person -- the unique index does not distinguish
    archived from active rows, so a fresh duplicate is still rejected."""
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))
    archived = (await store.get("p1")).model_copy(update={"archived": True})
    await store.save(archived)

    with pytest.raises(EngagementParticipantDuplicateError):
        await store.create(_participant("p2", "e1", "c1", crm_contact_id="ethan-1", first_name="Ethan"))


# =====================================================================
# EngagementParticipant.list_for_contact -- Contacts CRM Stage 3A
# =====================================================================


async def test_memory_engagement_participant_list_for_contact_groups_correctly_and_excludes_unrelated():
    store = MemoryEngagementParticipantStore()
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="kevin-1"))
    await store.create(_participant("p2", "e2", "c1", crm_contact_id="kevin-1"))
    await store.create(_participant("p3", "e1", "c1", crm_contact_id="other-contact"))  # unrelated

    found = await store.list_for_contact("kevin-1")
    assert {p.participant_id for p in found} == {"p1", "p2"}
    assert await store.list_for_contact("no-such-contact") == []


async def test_sqlite_engagement_participant_list_for_contact_groups_correctly_and_excludes_unrelated(
    sqlite_engagement_participant_store,
):
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="kevin-1"))
    await store.create(_participant("p2", "e2", "c1", crm_contact_id="kevin-1"))
    await store.create(_participant("p3", "e1", "c1", crm_contact_id="other-contact"))

    found = await store.list_for_contact("kevin-1")
    assert {p.participant_id for p in found} == {"p1", "p2"}
    assert await store.list_for_contact("no-such-contact") == []


async def test_memory_engagement_participant_list_for_contact_includes_archived():
    """The store returns everything -- filtering archived out is the
    caller's (service layer's) job, same convention as every other Client
    CRM list_for_x()."""
    store = MemoryEngagementParticipantStore()
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="kevin-1"))
    archived = (await store.get("p1")).model_copy(update={"archived": True})
    await store.save(archived)

    found = await store.list_for_contact("kevin-1")
    assert [p.participant_id for p in found] == ["p1"]
    assert found[0].archived is True


async def test_sqlite_engagement_participant_list_for_contact_includes_archived(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    await store.create(_participant("p1", "e1", "c1", crm_contact_id="kevin-1"))
    archived = (await store.get("p1")).model_copy(update={"archived": True})
    await store.save(archived)

    found = await store.list_for_contact("kevin-1")
    assert [p.participant_id for p in found] == ["p1"]
    assert found[0].archived is True


async def test_sqlite_engagement_participant_contact_index_exists_after_connect(sqlite_engagement_participant_store):
    store = sqlite_engagement_participant_store
    cursor = await store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='engagement_participants'"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    index_names = {r["name"] for r in rows}
    assert "idx_engagement_participants_contact" in index_names


# =====================================================================
# Engagement.list_by_ids -- Contacts CRM Stage 3A
# =====================================================================


async def test_memory_engagement_list_by_ids_returns_only_the_requested_engagements():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))
    await store.create(_engagement("e3", "c1"))  # not requested

    found = await store.list_by_ids(["e1", "e2"])
    assert {e.engagement_id for e in found} == {"e1", "e2"}


async def test_sqlite_engagement_list_by_ids_returns_only_the_requested_engagements(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    await store.create(_engagement("e2", "c1"))
    await store.create(_engagement("e3", "c1"))

    found = await store.list_by_ids(["e1", "e2"])
    assert {e.engagement_id for e in found} == {"e1", "e2"}


async def test_memory_engagement_list_by_ids_empty_list_returns_empty():
    store = MemoryEngagementStore()
    await store.create(_engagement("e1", "c1"))
    assert await store.list_by_ids([]) == []


async def test_sqlite_engagement_list_by_ids_empty_list_returns_empty(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    assert await store.list_by_ids([]) == []


async def test_sqlite_engagement_list_by_ids_unmatched_id_is_simply_absent(sqlite_engagement_store):
    store = sqlite_engagement_store
    await store.create(_engagement("e1", "c1"))
    found = await store.list_by_ids(["e1", "e-does-not-exist"])
    assert [e.engagement_id for e in found] == ["e1"]


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


# =====================================================================
# ClientTouchpoint model + ContactType -- Client CRM Stage 2A (2026-09-11)
# =====================================================================


def test_contact_type_v1_values():
    assert {member.value for member in ContactType} == {"email", "call", "slack", "linkedin", "in_person"}


def test_client_touchpoint_json_round_trip_preserves_every_field():
    touchpoint = _touchpoint(
        crm_contact_id="contact-1",
        contact_name="Sid Atkinson",
        occurred_at=NOW,
        contact_type=ContactType.CALL,
        contacted_by="Ria",
        note="Intro call, went well.",
        archived=True,
    )
    restored = ClientTouchpoint.model_validate_json(touchpoint.model_dump_json())
    assert restored == touchpoint


def test_client_touchpoint_contact_fields_default_to_none():
    touchpoint = _touchpoint()
    assert touchpoint.crm_contact_id is None
    assert touchpoint.contact_name is None
    assert touchpoint.note is None
    assert touchpoint.archived is False


# =====================================================================
# ClientTouchpoint store -- Memory + SQLite parity
# =====================================================================


async def test_memory_client_touchpoint_create_get_and_list_scoped_by_client():
    store = MemoryClientTouchpointStore()
    await store.create(_touchpoint("t1", "c1", occurred_at=NOW))
    await store.create(_touchpoint("t2", "c1", occurred_at=LATER))
    await store.create(_touchpoint("t3", "c2", occurred_at=NOW))  # different client

    assert (await store.get("t1")).touchpoint_id == "t1"
    assert await store.get("t-missing") is None

    for_c1 = await store.list_for_client("c1")
    assert {t.touchpoint_id for t in for_c1} == {"t1", "t2"}
    assert await store.list_for_client("c-missing") == []


async def test_sqlite_client_touchpoint_create_get_and_list_scoped_by_client(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t1", "c1", occurred_at=NOW))
    await store.create(_touchpoint("t2", "c1", occurred_at=LATER))
    await store.create(_touchpoint("t3", "c2", occurred_at=NOW))

    assert (await store.get("t1")).touchpoint_id == "t1"
    assert await store.get("t-missing") is None

    for_c1 = await store.list_for_client("c1")
    assert {t.touchpoint_id for t in for_c1} == {"t1", "t2"}
    assert await store.list_for_client("c-missing") == []


async def test_memory_client_touchpoint_deterministic_newest_first_ordering():
    store = MemoryClientTouchpointStore()
    # Distinct occurred_at -- the primary sort key.
    await store.create(_touchpoint("t-old", "c1", occurred_at=NOW))
    await store.create(_touchpoint("t-new", "c1", occurred_at=LATER))
    # Same occurred_at as t-tie-a, but a LATER created_at -- the tiebreak.
    await store.create(_touchpoint("t-tie-a", "c1", occurred_at=EVEN_LATER, created_at=NOW))
    await store.create(_touchpoint("t-tie-b", "c1", occurred_at=EVEN_LATER, created_at=LATER))
    # Same occurred_at AND created_at as each other -- touchpoint_id is the final tiebreak.
    await store.create(_touchpoint("t-final-a", "c1", occurred_at=NOW, created_at=NOW))
    await store.create(_touchpoint("t-final-z", "c1", occurred_at=NOW, created_at=NOW))

    ordered = await store.list_for_client("c1")
    ids = [t.touchpoint_id for t in ordered]
    # The EVEN_LATER-occurred_at pair sorts first (occurred_at DESC is the
    # primary key), ordered between themselves by created_at DESC
    # (t-tie-b's LATER created_at before t-tie-a's NOW); then t-new
    # (LATER occurred_at); then the NOW-occurred_at group, where t-old (no
    # created_at tie) and the final-a/final-z pair (tied on both
    # occurred_at AND created_at) sort by touchpoint_id DESC as the last
    # resort.
    assert ids[0:2] == ["t-tie-b", "t-tie-a"]
    assert ids[2] == "t-new"
    assert set(ids[3:]) == {"t-old", "t-final-a", "t-final-z"}
    # Within the fully-tied (NOW, NOW) pair specifically, touchpoint_id DESC.
    final_pair_order = [i for i in ids if i in ("t-final-a", "t-final-z")]
    assert final_pair_order == ["t-final-z", "t-final-a"]


async def test_sqlite_client_touchpoint_deterministic_newest_first_ordering(sqlite_client_touchpoint_store):
    """Same scenario as the Memory test, but ALSO inserted in a
    deliberately shuffled order -- proving the SQLite store sorts
    explicitly rather than relying on insertion/row order."""
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t-final-z", "c1", occurred_at=NOW, created_at=NOW))
    await store.create(_touchpoint("t-tie-a", "c1", occurred_at=EVEN_LATER, created_at=NOW))
    await store.create(_touchpoint("t-new", "c1", occurred_at=LATER))
    await store.create(_touchpoint("t-final-a", "c1", occurred_at=NOW, created_at=NOW))
    await store.create(_touchpoint("t-old", "c1", occurred_at=NOW))
    await store.create(_touchpoint("t-tie-b", "c1", occurred_at=EVEN_LATER, created_at=LATER))

    ordered = await store.list_for_client("c1")
    ids = [t.touchpoint_id for t in ordered]
    assert ids[0:2] == ["t-tie-b", "t-tie-a"]
    assert ids[2] == "t-new"
    final_pair_order = [i for i in ids if i in ("t-final-a", "t-final-z")]
    assert final_pair_order == ["t-final-z", "t-final-a"]


async def test_memory_client_touchpoint_save_and_archive():
    store = MemoryClientTouchpointStore()
    await store.create(_touchpoint())
    updated = (await store.get("t1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("t1")).archived is True
    with pytest.raises(ClientTouchpointNotFoundError):
        await store.save(_touchpoint("t-missing"))


async def test_sqlite_client_touchpoint_save_and_archive(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint())
    updated = (await store.get("t1")).model_copy(update={"archived": True})
    await store.save(updated)
    assert (await store.get("t1")).archived is True
    with pytest.raises(ClientTouchpointNotFoundError):
        await store.save(_touchpoint("t-missing"))


async def test_sqlite_client_touchpoint_save_and_restore_round_trips_optional_contact_fields(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint(crm_contact_id="contact-1", contact_name="Sid Atkinson", note="Dinner planning."))
    fresh = await store.get("t1")
    assert fresh.crm_contact_id == "contact-1"
    assert fresh.contact_name == "Sid Atkinson"
    assert fresh.note == "Dinner planning."

    cleared = fresh.model_copy(update={"crm_contact_id": None, "contact_name": None})
    await store.save(cleared)
    fresh_again = await store.get("t1")
    assert fresh_again.crm_contact_id is None
    assert fresh_again.contact_name is None


# =====================================================================
# ClientTouchpoint.list_for_clients -- bulk cross-client lookup, Stage 2C
# =====================================================================


async def test_memory_touchpoint_list_for_clients_groups_correctly_and_excludes_unrelated():
    store = MemoryClientTouchpointStore()
    await store.create(_touchpoint("t1", "c1"))
    await store.create(_touchpoint("t2", "c2"))
    await store.create(_touchpoint("t3", "c3"))  # unrelated -- not in the requested id list

    found = await store.list_for_clients(["c1", "c2"])
    assert {t.touchpoint_id for t in found} == {"t1", "t2"}


async def test_sqlite_touchpoint_list_for_clients_groups_correctly_and_excludes_unrelated(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t1", "c1"))
    await store.create(_touchpoint("t2", "c2"))
    await store.create(_touchpoint("t3", "c3"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {t.touchpoint_id for t in found} == {"t1", "t2"}


async def test_memory_touchpoint_list_for_clients_multiple_touchpoints_per_client():
    store = MemoryClientTouchpointStore()
    await store.create(_touchpoint("t1", "c1"))
    await store.create(_touchpoint("t2", "c1"))
    await store.create(_touchpoint("t3", "c2"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {t.touchpoint_id for t in found} == {"t1", "t2", "t3"}


async def test_sqlite_touchpoint_list_for_clients_multiple_touchpoints_per_client(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t1", "c1"))
    await store.create(_touchpoint("t2", "c1"))
    await store.create(_touchpoint("t3", "c2"))

    found = await store.list_for_clients(["c1", "c2"])
    assert {t.touchpoint_id for t in found} == {"t1", "t2", "t3"}


async def test_memory_touchpoint_list_for_clients_empty_id_list_returns_empty():
    store = MemoryClientTouchpointStore()
    await store.create(_touchpoint("t1", "c1"))
    assert await store.list_for_clients([]) == []


async def test_sqlite_touchpoint_list_for_clients_empty_id_list_returns_empty(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t1", "c1"))
    assert await store.list_for_clients([]) == []


async def test_sqlite_touchpoint_list_for_clients_unmatched_id_returns_empty(sqlite_client_touchpoint_store):
    store = sqlite_client_touchpoint_store
    await store.create(_touchpoint("t1", "c1"))
    assert await store.list_for_clients(["c-does-not-exist"]) == []
