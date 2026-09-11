"""
ClientCrmService tests -- Client CRM Stage 1B (2026-09-07). Client CRUD
only; ClientContact/Engagement/ClientNote are never touched by this
service (see client_crm_service.py's own module docstring) -- proven here
directly by constructing their OWN independent stores and confirming
archiving a Client never mutates rows in them.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.config import settings
from app.models.activity import ActivityCategory
from app.models.client_crm import (
    ClientRelationshipClassification,
    ClientStatus,
    ClientTouchpoint,
    ContactType,
    Engagement,
    EngagementParticipant,
    EngagementParticipantView,
    EngagementStatus,
    EngagementType,
)
from app.models.crm import CrmContact
from app.models.luma import LumaEvent
from app.repositories.client_contact_store import ClientContactStore, MemoryClientContactStore
from app.repositories.client_note_store import MemoryClientNoteStore
from app.repositories.client_store import ClientStore, MemoryClientStore
from app.repositories.crm_contact_store import CrmContactStore, MemoryCrmContactStore
from app.repositories.engagement_closeout_store import EngagementCloseoutStore, MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import EngagementParticipantStore, MemoryEngagementParticipantStore
from app.repositories.engagement_store import EngagementStore, MemoryEngagementStore
from app.repositories.client_touchpoint_store import ClientTouchpointStore, MemoryClientTouchpointStore
from app.repositories.luma_event_store import LumaEventStore, MemoryLumaEventStore
from app.repositories.sqlite_client_store import SQLiteClientStore
from app.services.activity_log_service import ActivityLogService
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.services.client_crm_service import (
    ClientContactNotFound,
    ClientCrmService,
    ClientNotFound,
    ClientTouchpointNotFound,
    EngagementCloseoutAlreadyExists,
    EngagementCloseoutNotFound,
    EngagementLumaEventAlreadyLinked,
    EngagementNotFound,
    EngagementParticipantDuplicate,
    EngagementParticipantNotFound,
    LumaEventNotFound,
    _BUSINESS_TIMEZONE,
    _business_today,
    _last_contacted,
    _next_dinner,
)
from app.services.contact_engagement_signal_service import (
    ContactEngagementSignalOutcome,
    ContactEngagementSignalResult,
    ContactEngagementSignalService,
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
    client_touchpoint_store: ClientTouchpointStore | None = None,
    contact_engagement_signal_service: ContactEngagementSignalService | None = None,
) -> tuple[ClientCrmService, ActivityLogService]:
    activity_log = ActivityLogService(MemoryActivityEventStore())
    resolved_crm_contact_store = crm_contact_store or MemoryCrmContactStore()
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store or MemoryClientContactStore(),
        crm_contact_store=resolved_crm_contact_store,
        engagement_store=engagement_store or MemoryEngagementStore(),
        engagement_closeout_store=engagement_closeout_store or MemoryEngagementCloseoutStore(),
        engagement_participant_store=engagement_participant_store or MemoryEngagementParticipantStore(),
        luma_event_store=luma_event_store or MemoryLumaEventStore(),
        client_touchpoint_store=client_touchpoint_store or MemoryClientTouchpointStore(),
        contact_engagement_signal_service=contact_engagement_signal_service
        or ContactEngagementSignalService(crm_contact_store=resolved_crm_contact_store, activity_log=activity_log),
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


@pytest.fixture
def touchpoint_service():
    """Same as contact_service, but ALSO exposes the ClientTouchpointStore
    directly (needed to inspect rows Stage 2A's own service methods don't
    otherwise return, e.g. archived ones)."""
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    service, activity_log = _make_service(
        client_store,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        client_touchpoint_store=client_touchpoint_store,
    )
    return service, activity_log, client_contact_store, crm_contact_store, client_touchpoint_store


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
# next_dinner / last_contacted derivation -- Client CRM Stage 2C
# =====================================================================


@pytest.fixture
def master_crm_service():
    """Same as memory_service, but ALSO exposes the EngagementStore and
    ClientTouchpointStore directly -- Stage 2C's Next Dinner/Last
    Contacted derivation needs both seeded independently of any
    dedicated create_engagement()/create_client_touchpoint() service
    method (those enforce extra validation, e.g. a required Client
    existence check or contacted_by non-blank rule, that's irrelevant to
    testing list_clients()'s own derivation)."""
    client_store = MemoryClientStore()
    engagement_store = MemoryEngagementStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    service, activity_log = _make_service(
        client_store, engagement_store=engagement_store, client_touchpoint_store=client_touchpoint_store
    )
    return service, activity_log, engagement_store, client_touchpoint_store


def _svc_engagement(engagement_id="e1", client_id="c1", **overrides) -> Engagement:
    now = datetime.now(timezone.utc)
    overrides.setdefault("title", "SF Investor Dinner")
    overrides.setdefault("engagement_type", EngagementType.DINNER)
    overrides.setdefault("created_at", now)
    overrides.setdefault("updated_at", now)
    return Engagement(engagement_id=engagement_id, client_id=client_id, **overrides)


def _svc_touchpoint(touchpoint_id="t1", client_id="c1", **overrides) -> ClientTouchpoint:
    now = datetime.now(timezone.utc)
    overrides.setdefault("occurred_at", now)
    overrides.setdefault("contact_type", ContactType.EMAIL)
    overrides.setdefault("contacted_by", "Ria")
    overrides.setdefault("created_at", now)
    overrides.setdefault("updated_at", now)
    return ClientTouchpoint(touchpoint_id=touchpoint_id, client_id=client_id, **overrides)


TODAY = date(2026, 9, 10)


# --- _next_dinner (pure function) -------------------------------------------


def test_next_dinner_qualifying_dinner_is_chosen():
    e = _svc_engagement(engagement_date=TODAY + timedelta(days=5))
    assert _next_dinner([e], TODAY) == TODAY + timedelta(days=5)


def test_next_dinner_excludes_sponsorship():
    e = _svc_engagement(engagement_type=EngagementType.SPONSORSHIP, engagement_date=TODAY + timedelta(days=5))
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_excludes_other():
    e = _svc_engagement(engagement_type=EngagementType.OTHER, engagement_date=TODAY + timedelta(days=5))
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_excludes_archived():
    e = _svc_engagement(engagement_date=TODAY + timedelta(days=5), archived=True)
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_excludes_cancelled():
    e = _svc_engagement(engagement_date=TODAY + timedelta(days=5), status=EngagementStatus.CANCELLED)
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_excludes_past():
    e = _svc_engagement(engagement_date=TODAY - timedelta(days=1))
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_includes_today():
    """Today is inclusive -- a dinner happening later today still qualifies."""
    e = _svc_engagement(engagement_date=TODAY)
    assert _next_dinner([e], TODAY) == TODAY


def test_next_dinner_includes_future():
    e = _svc_engagement(engagement_date=TODAY + timedelta(days=30))
    assert _next_dinner([e], TODAY) == TODAY + timedelta(days=30)


def test_next_dinner_excludes_null_engagement_date():
    e = _svc_engagement(engagement_date=None)
    assert _next_dinner([e], TODAY) is None


def test_next_dinner_earliest_future_dinner_wins():
    later = _svc_engagement("e1", engagement_date=TODAY + timedelta(days=30))
    sooner = _svc_engagement("e2", engagement_date=TODAY + timedelta(days=5))
    assert _next_dinner([later, sooner], TODAY) == TODAY + timedelta(days=5)


def test_next_dinner_same_date_ties_still_resolve_to_that_shared_date():
    a = _svc_engagement("e-a", engagement_date=TODAY + timedelta(days=5))
    b = _svc_engagement("e-b", engagement_date=TODAY + timedelta(days=5))
    assert _next_dinner([a, b], TODAY) == TODAY + timedelta(days=5)
    assert _next_dinner([b, a], TODAY) == TODAY + timedelta(days=5)


def test_next_dinner_same_date_tie_break_is_engagement_id_ascending():
    """_next_dinner only returns a date (two tied Engagements share the
    same date either way), so the tie-break rule itself is verified
    directly against the exact sort key _next_dinner uses."""
    a = _svc_engagement("e-a", engagement_date=TODAY + timedelta(days=5))
    b = _svc_engagement("e-b", engagement_date=TODAY + timedelta(days=5))
    ordered = sorted([b, a], key=lambda e: (e.engagement_date, e.engagement_id))
    assert ordered[0].engagement_id == "e-a"


def test_next_dinner_no_qualifying_dinner_returns_none():
    assert _next_dinner([], TODAY) is None
    non_qualifying = _svc_engagement(engagement_type=EngagementType.OTHER, engagement_date=TODAY)
    assert _next_dinner([non_qualifying], TODAY) is None


# --- _last_contacted (pure function) ----------------------------------------


def test_last_contacted_active_touchpoint_qualifies():
    now = datetime.now(timezone.utc)
    t = _svc_touchpoint(occurred_at=now)
    assert _last_contacted([t]) == now


def test_last_contacted_excludes_archived():
    t = _svc_touchpoint(archived=True)
    assert _last_contacted([t]) is None


def test_last_contacted_newest_occurred_at_wins():
    older = _svc_touchpoint("t1", occurred_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    newer = _svc_touchpoint("t2", occurred_at=datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert _last_contacted([older, newer]) == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert _last_contacted([newer, older]) == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_last_contacted_preserves_existing_touchpoint_sort_key_tie_semantics():
    """Same occurred_at -- touchpoint_sort_key's next tie-break
    (created_at, then touchpoint_id) is what actually decides the winner,
    the EXACT same shared helper the Client detail page's own Last
    Contact already uses, never a second, independently-written rule."""
    from app.repositories.client_touchpoint_store import touchpoint_sort_key

    same_time = datetime(2026, 9, 10, tzinfo=timezone.utc)
    earlier_created = _svc_touchpoint("t1", occurred_at=same_time, created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    later_created = _svc_touchpoint("t2", occurred_at=same_time, created_at=datetime(2026, 9, 2, tzinfo=timezone.utc))

    ordered = sorted([earlier_created, later_created], key=touchpoint_sort_key, reverse=True)
    assert ordered[0].touchpoint_id == "t2"
    assert _last_contacted([earlier_created, later_created]) == same_time


def test_last_contacted_no_touchpoint_returns_none():
    assert _last_contacted([]) is None


# --- _business_today (America/Chicago boundary) -----------------------------


def test_business_today_uses_america_chicago_not_utc_date():
    """2026-03-15T03:00:00 UTC = 2026-03-14T22:00:00-05:00 in
    America/Chicago (CDT, in effect after 2026's spring-forward on Mar 8)
    -- still March 14 in Chicago while UTC has already rolled over to
    March 15. This is exactly why UTC was rejected as the V1 boundary: a
    dinner scheduled for March 14 (the real, Chicago-local "today") must
    not vanish from Next Dinner just because UTC's calendar date already
    flipped during the U.S. evening."""
    utc_instant = datetime(2026, 3, 15, 3, 0, tzinfo=timezone.utc)
    assert _business_today(now=utc_instant) == date(2026, 3, 14)


def test_business_today_matches_utc_date_well_inside_the_business_day():
    """Confirms this isn't simply 'always one day behind UTC' -- an
    instant with no boundary ambiguity agrees with the UTC date too."""
    utc_instant = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)  # 1pm Central (CDT)
    assert _business_today(now=utc_instant) == date(2026, 6, 15)


def test_business_today_timezone_constant_is_america_chicago():
    assert str(_BUSINESS_TIMEZONE) == "America/Chicago"


# --- list_clients() integration: derived fields + bulk-query shape ---------


async def test_list_clients_populates_next_dinner_and_last_contacted(master_crm_service):
    service, _, engagement_store, client_touchpoint_store = master_crm_service
    client = await service.create_client({"name": "Hive ASMBLD"})

    now = datetime.now(timezone.utc)
    await engagement_store.create(
        _svc_engagement("e1", client.client_id, engagement_date=date.today() + timedelta(days=10))
    )
    await client_touchpoint_store.create(_svc_touchpoint("t1", client.client_id, occurred_at=now))

    page = await service.list_clients()
    item = page.items[0]
    assert item.next_dinner == date.today() + timedelta(days=10)
    assert item.last_contacted == now


async def test_list_clients_next_dinner_and_last_contacted_are_none_with_no_data(master_crm_service):
    service, _, _engagement_store, _client_touchpoint_store = master_crm_service
    await service.create_client({"name": "Hive ASMBLD"})

    page = await service.list_clients()
    assert page.items[0].next_dinner is None
    assert page.items[0].last_contacted is None


async def test_list_clients_derivation_is_scoped_per_client_not_shared_across_clients(master_crm_service):
    service, _, engagement_store, client_touchpoint_store = master_crm_service
    client_a = await service.create_client({"name": "A"})
    client_b = await service.create_client({"name": "B"})

    await engagement_store.create(
        _svc_engagement("e1", client_a.client_id, engagement_date=date.today() + timedelta(days=5))
    )
    await client_touchpoint_store.create(_svc_touchpoint("t1", client_a.client_id))
    # Client B has neither an Engagement nor a Touchpoint.

    page = await service.list_clients(sort_by="name")
    by_name = {c.name: c for c in page.items}
    assert by_name["A"].next_dinner == date.today() + timedelta(days=5)
    assert by_name["A"].last_contacted is not None
    assert by_name["B"].next_dinner is None
    assert by_name["B"].last_contacted is None


async def test_list_clients_derivation_only_covers_the_current_page(master_crm_service):
    """Derivation must be computed for the RETURNED page's Clients only,
    never the full filtered/unpaginated set -- confirmed indirectly here
    by checking the paginated-out Client's own data is simply never
    touched (no error, no cross-contamination) while the page's own
    Client is correctly enriched."""
    service, _, engagement_store, client_touchpoint_store = master_crm_service
    await service.create_client({"name": "Alpha"})
    beta = await service.create_client({"name": "Beta"})
    await engagement_store.create(
        _svc_engagement("e1", beta.client_id, engagement_date=date.today() + timedelta(days=5))
    )

    page1 = await service.list_clients(sort_by="name", page=1, page_size=1)
    assert page1.items[0].name == "Alpha"
    assert page1.items[0].next_dinner is None  # Alpha has no Engagement

    page2 = await service.list_clients(sort_by="name", page=2, page_size=1)
    assert page2.items[0].name == "Beta"
    assert page2.items[0].next_dinner == date.today() + timedelta(days=5)


async def test_list_clients_derivation_never_calls_list_for_client_per_client(master_crm_service):
    """Structural regression guard: list_clients() must use the bulk
    list_for_clients() methods, never fall back to (or additionally call)
    list_for_client() once per Client -- that would silently reintroduce
    the exact N+1 shape Stage 2C's bulk methods exist to avoid."""
    service, _, engagement_store, client_touchpoint_store = master_crm_service

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("list_for_client() must never be called by list_clients() -- use list_for_clients().")

    engagement_store.list_for_client = _forbidden
    client_touchpoint_store.list_for_client = _forbidden

    for i in range(5):
        client = await service.create_client({"name": f"Client {i}"})
        await engagement_store.create(_svc_engagement(f"e{i}", client.client_id))
        await client_touchpoint_store.create(_svc_touchpoint(f"t{i}", client.client_id))

    page = await service.list_clients(sort_by="name")
    assert len(page.items) == 5


async def test_list_clients_calls_each_bulk_method_exactly_once_regardless_of_client_count(master_crm_service):
    """Precise bounded-query proof: exactly ONE list_for_clients() call
    per entity type per list_clients() call, regardless of how many
    Clients are on the page -- never one query per Client."""
    service, _, engagement_store, client_touchpoint_store = master_crm_service

    call_counts = {"engagements": 0, "touchpoints": 0}
    original_engagements = engagement_store.list_for_clients
    original_touchpoints = client_touchpoint_store.list_for_clients

    async def _counted_engagements(client_ids):
        call_counts["engagements"] += 1
        return await original_engagements(client_ids)

    async def _counted_touchpoints(client_ids):
        call_counts["touchpoints"] += 1
        return await original_touchpoints(client_ids)

    engagement_store.list_for_clients = _counted_engagements
    client_touchpoint_store.list_for_clients = _counted_touchpoints

    for i in range(5):
        await service.create_client({"name": f"Client {i}"})

    await service.list_clients(sort_by="name")

    assert call_counts == {"engagements": 1, "touchpoints": 1}


async def test_list_clients_empty_result_still_calls_bulk_methods_safely(master_crm_service):
    """No Clients at all -- page_client_ids is [] -- must not error."""
    service, _, _engagement_store, _client_touchpoint_store = master_crm_service
    page = await service.list_clients()
    assert page.items == []
    assert page.total == 0


# =====================================================================
# Default ordering by Next Dinner -- Client CRM Stage 2C.1
# =====================================================================


async def _client_with_dinner(service, engagement_store, name, engagement_date=None, **engagement_overrides):
    """Creates a Client and, unless engagement_date/engagement_overrides
    explicitly describe a non-qualifying Engagement, a single qualifying
    Dinner Engagement dated `engagement_date` for it."""
    client = await service.create_client({"name": name})
    await engagement_store.create(
        _svc_engagement(f"e-{client.client_id}", client.client_id, engagement_date=engagement_date, **engagement_overrides)
    )
    return client


async def test_next_dinner_sort_sep17_before_sep22(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    sep22 = await _client_with_dinner(service, engagement_store, "Hive ASMBLD", TODAY + timedelta(days=12))
    sep17 = await _client_with_dinner(service, engagement_store, "Hot Shot", TODAY + timedelta(days=7))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [sep17.client_id, sep22.client_id]


async def test_next_dinner_sort_sep22_before_sep23(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    sep23 = await _client_with_dinner(service, engagement_store, "Applied Curiosity", TODAY + timedelta(days=13))
    sep22 = await _client_with_dinner(service, engagement_store, "Hive ASMBLD", TODAY + timedelta(days=12))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [sep22.client_id, sep23.client_id]


async def test_next_dinner_sort_upcoming_dinners_come_before_null(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    no_dinner = await service.create_client({"name": "No Dinner Co"})
    has_dinner = await _client_with_dinner(service, engagement_store, "Has Dinner Co", TODAY + timedelta(days=30))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [has_dinner.client_id, no_dinner.client_id]


async def test_next_dinner_sort_null_group_sorts_by_client_name(master_crm_service):
    service, _, _engagement_store, _ = master_crm_service
    await service.create_client({"name": "Zeta Co"})
    await service.create_client({"name": "Alpha Co"})

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.name for c in page.items] == ["Alpha Co", "Zeta Co"]


async def test_next_dinner_sort_same_date_tie_sorts_by_client_name(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    zeta = await _client_with_dinner(service, engagement_store, "Zeta Co", TODAY + timedelta(days=10))
    alpha = await _client_with_dinner(service, engagement_store, "Alpha Co", TODAY + timedelta(days=10))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [alpha.client_id, zeta.client_id]


async def test_next_dinner_sort_deterministic_final_fallback_is_client_id(master_crm_service):
    """Same date AND same name -- client_id is the last, fully
    deterministic tie-break (Stage 2C.1's own locked semantics)."""
    service, _, engagement_store, _ = master_crm_service
    client_a = await service.create_client({"name": "Twin Co"})
    client_b = await service.create_client({"name": "Twin Co"})
    same_date = TODAY + timedelta(days=10)
    await engagement_store.create(_svc_engagement(f"e-{client_a.client_id}", client_a.client_id, engagement_date=same_date))
    await engagement_store.create(_svc_engagement(f"e-{client_b.client_id}", client_b.client_id, engagement_date=same_date))

    expected_first = min(client_a.client_id, client_b.client_id)
    page = await service.list_clients(sort_by="next_dinner")
    assert page.items[0].client_id == expected_first


async def test_next_dinner_sort_past_dinner_behaves_as_null(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    past = await _client_with_dinner(service, engagement_store, "Past Co", TODAY - timedelta(days=1))
    future = await _client_with_dinner(service, engagement_store, "Future Co", TODAY + timedelta(days=1))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [future.client_id, past.client_id]
    assert page.items[1].next_dinner is None


async def test_next_dinner_sort_cancelled_dinner_behaves_as_null(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    cancelled = await _client_with_dinner(
        service, engagement_store, "Cancelled Co", TODAY + timedelta(days=5), status=EngagementStatus.CANCELLED
    )
    future = await _client_with_dinner(service, engagement_store, "Future Co", TODAY + timedelta(days=1))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [future.client_id, cancelled.client_id]
    assert page.items[1].next_dinner is None


async def test_next_dinner_sort_archived_dinner_behaves_as_null(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    archived = await _client_with_dinner(service, engagement_store, "Archived Co", TODAY + timedelta(days=5), archived=True)
    future = await _client_with_dinner(service, engagement_store, "Future Co", TODAY + timedelta(days=1))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [future.client_id, archived.client_id]
    assert page.items[1].next_dinner is None


async def test_next_dinner_sort_non_dinner_engagement_behaves_as_null(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    sponsorship = await _client_with_dinner(
        service, engagement_store, "Sponsorship Co", TODAY + timedelta(days=5), engagement_type=EngagementType.SPONSORSHIP
    )
    future = await _client_with_dinner(service, engagement_store, "Future Co", TODAY + timedelta(days=1))

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [future.client_id, sponsorship.client_id]
    assert page.items[1].next_dinner is None


async def test_next_dinner_sort_america_chicago_today_semantics_remain_intact(master_crm_service):
    """Integration-level confirmation that sort_by="next_dinner" uses the
    same _business_today() (America/Chicago) as plain next_dinner
    derivation -- a dinner dated exactly today still qualifies (and thus
    sorts ahead of a null-group Client)."""
    service, _, engagement_store, _ = master_crm_service
    real_business_today = _business_today()
    today_dinner = await _client_with_dinner(service, engagement_store, "Today Co", real_business_today)
    no_dinner = await service.create_client({"name": "No Dinner Co"})

    page = await service.list_clients(sort_by="next_dinner")
    assert [c.client_id for c in page.items] == [today_dinner.client_id, no_dinner.client_id]
    assert page.items[0].next_dinner == real_business_today


async def test_next_dinner_sort_happens_before_pagination_page1_has_globally_nearest_dinners(master_crm_service):
    """The exact regression this stage exists to prevent: deriving Next
    Dinner only for an arbitrary pre-sliced page (rather than the full
    filtered set) would put whatever happened to land on page 1 first,
    not the globally nearest dinners."""
    service, _, engagement_store, _ = master_crm_service
    # Create 5 Clients whose NAMES sort in the opposite order of their
    # dinner dates, so a name-based pre-slice would get page 1 wrong.
    names_and_offsets = [("E Co", 5), ("D Co", 4), ("C Co", 3), ("B Co", 2), ("A Co", 1)]
    created = {}
    for name, offset in names_and_offsets:
        created[name] = await _client_with_dinner(service, engagement_store, name, TODAY + timedelta(days=offset))

    page1 = await service.list_clients(sort_by="next_dinner", page=1, page_size=2)
    assert [c.name for c in page1.items] == ["A Co", "B Co"]
    assert page1.total == 5

    page2 = await service.list_clients(sort_by="next_dinner", page=2, page_size=2)
    assert [c.name for c in page2.items] == ["C Co", "D Co"]

    page3 = await service.list_clients(sort_by="next_dinner", page=3, page_size=2)
    assert [c.name for c in page3.items] == ["E Co"]


async def test_next_dinner_sort_filters_are_applied_before_sorting(master_crm_service):
    service, _, engagement_store, _ = master_crm_service
    active = await _client_with_dinner(service, engagement_store, "Active Co", TODAY + timedelta(days=10))
    inactive = await _client_with_dinner(service, engagement_store, "Inactive Co", TODAY + timedelta(days=1))
    await service.update_client(inactive.client_id, {"status": ClientStatus.INACTIVE})

    page = await service.list_clients(sort_by="next_dinner", status=ClientStatus.ACTIVE)
    assert [c.client_id for c in page.items] == [active.client_id]


async def test_existing_explicit_sort_by_name_unaffected_by_next_dinner_dates(master_crm_service):
    """Regression: explicit sort_by="name" must ignore dinner dates
    entirely, exactly as before Stage 2C.1."""
    service, _, engagement_store, _ = master_crm_service
    await _client_with_dinner(service, engagement_store, "Zeta Co", TODAY + timedelta(days=1))
    await _client_with_dinner(service, engagement_store, "Alpha Co", TODAY + timedelta(days=30))

    page = await service.list_clients(sort_by="name")
    assert [c.name for c in page.items] == ["Alpha Co", "Zeta Co"]


async def test_existing_explicit_sort_by_next_action_due_still_behaves_as_before(master_crm_service):
    service, _, _engagement_store, _ = master_crm_service
    await service.create_client({"name": "No due date"})
    await service.create_client({"name": "Later", "next_action_due": date(2027, 6, 1)})
    await service.create_client({"name": "Sooner", "next_action_due": date(2027, 1, 1)})

    page = await service.list_clients(sort_by="next_action_due", sort_dir="asc")
    assert [c.name for c in page.items] == ["Sooner", "Later", "No due date"]


async def test_next_dinner_sort_does_not_introduce_per_client_engagement_queries(master_crm_service):
    """Structural regression guard for the sort_by="next_dinner" path
    specifically: bulk list_for_clients() must be called exactly once
    (across the FULL filtered set), never list_for_client() per Client."""
    service, _, engagement_store, client_touchpoint_store = master_crm_service

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("list_for_client() must never be called by list_clients().")

    engagement_store.list_for_client = _forbidden
    client_touchpoint_store.list_for_client = _forbidden

    call_counts = {"engagements": 0}
    original_engagements = engagement_store.list_for_clients

    async def _counted_engagements(client_ids):
        call_counts["engagements"] += 1
        return await original_engagements(client_ids)

    engagement_store.list_for_clients = _counted_engagements

    for i in range(6):
        await _client_with_dinner(service, engagement_store, f"Client {i}", TODAY + timedelta(days=i))

    page = await service.list_clients(sort_by="next_dinner", page=1, page_size=3)
    assert len(page.items) == 3
    assert call_counts["engagements"] == 1


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


# --- Client CRM Stage 5A (2026-09-11) -- decline_origin, manual create/update ---


async def test_create_declined_participant_with_guest_origin(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "guest"}
    )
    assert participant.decline_origin == "guest"
    assert participant.decline_origin_is_manual is True


async def test_create_declined_participant_with_host_origin(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "host"}
    )
    assert participant.decline_origin == "host"
    assert participant.decline_origin_is_manual is True


async def test_create_declined_participant_with_unknown_origin(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "unknown"}
    )
    assert participant.decline_origin == "unknown"
    assert participant.decline_origin_is_manual is True


async def test_create_declined_participant_omitting_decline_origin_defaults_to_unknown(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined"}
    )
    assert participant.decline_origin == "unknown"
    assert participant.decline_origin_is_manual is True


async def test_create_non_declined_participant_normalizes_decline_origin_to_null(participant_service):
    service, _activity_log, _store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "confirmed", "decline_origin": "guest"}
    )
    assert participant.decline_origin is None
    assert participant.decline_origin_is_manual is False


async def test_update_decline_origin_becomes_human_authoritative(participant_service):
    """Explicitly PATCHing decline_origin while already declined is a
    direct human assertion, even if the participant was previously
    Luma-derived."""
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "unknown"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"decline_origin": "host"}
    )
    assert updated.decline_origin == "host"
    assert updated.decline_origin_is_manual is True


async def test_update_declined_omitting_decline_origin_preserves_existing_value(participant_service):
    """Standard PATCH 'omitted field is left untouched' semantics -- a
    patch that doesn't mention decline_origin, while the participant was
    ALREADY declined, must not reset it."""
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "guest"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"role": "host"}
    )
    assert updated.decline_origin == "guest"
    assert updated.decline_origin_is_manual is True


async def test_update_transition_into_declined_without_origin_defaults_unknown_and_manual(participant_service):
    """No prior declined-state value exists to preserve when this PATCH
    itself is what causes the transition into declined -- normalizes to
    Unknown, and counts as human-authoritative since an operator caused it."""
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "confirmed"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"rsvp_status": "declined"}
    )
    assert updated.decline_origin == "unknown"
    assert updated.decline_origin_is_manual is True


async def test_update_clears_decline_origin_when_rsvp_moves_away_from_declined(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "declined", "decline_origin": "host"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"rsvp_status": "confirmed"}
    )
    assert updated.decline_origin is None
    assert updated.decline_origin_is_manual is False


async def test_update_submitting_decline_origin_while_non_declined_is_ignored(participant_service):
    """Core invariant enforced even if the operator submits a
    decline_origin alongside a non-declined rsvp_status -- always null."""
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"first_name": "Jane", "rsvp_status": "confirmed"}
    )
    updated = await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id,
        {"rsvp_status": "invited", "decline_origin": "guest"},
    )
    assert updated.decline_origin is None
    assert updated.decline_origin_is_manual is False


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


# =====================================================================
# Manual participant -> Engagement Stage signal trigger -- Contacts CRM
# Stage 3B (2026-09-11)
# =====================================================================


async def test_manual_create_confirmed_guest_triggers_the_signal(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "guest", "rsvp_status": "confirmed"}
    )

    contact = await crm_contact_store.get("ethan-1")
    assert contact.custom_fields["engagement_stage"] == "Interested"


async def test_manual_update_invited_to_confirmed_triggers_the_signal(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "guest", "rsvp_status": "invited"}
    )
    assert (await crm_contact_store.get("ethan-1")).custom_fields.get("engagement_stage") is None

    await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"rsvp_status": "confirmed"}
    )

    assert (await crm_contact_store.get("ethan-1")).custom_fields["engagement_stage"] == "Interested"


async def test_manual_attendance_update_null_to_attended_triggers_the_signal(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "guest"}
    )
    assert (await crm_contact_store.get("ethan-1")).custom_fields.get("engagement_stage") is None

    await service.update_engagement_participant(
        client.client_id, engagement.engagement_id, participant.participant_id, {"attendance_status": "attended"}
    )

    assert (await crm_contact_store.get("ethan-1")).custom_fields["engagement_stage"] == "Interested"


async def test_manual_create_ineligible_role_does_not_trigger(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "client", "rsvp_status": "confirmed"}
    )

    assert (await crm_contact_store.get("ethan-1")).custom_fields.get("engagement_stage") is None


async def test_manual_signal_does_not_produce_a_second_generic_contact_updated_activity_event(participant_service):
    """The signal writes directly via crm_contact_store.save(), never
    through CrmService.update_contact() -- confirms no redundant generic
    "contact.updated" event accompanies the specific advancement event."""
    service, activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "guest", "rsvp_status": "confirmed"}
    )

    page = await activity_log.list_events(category=ActivityCategory.CONTACTS)
    event_types = [e.event_type for e in page.items]
    assert event_types.count("contact.engagement_stage.advanced") == 1
    assert "contact.updated" not in event_types


async def test_manual_signal_failure_never_blocks_the_participant_write(participant_service, monkeypatch):
    """A signal-service exception must never undo or block the already-
    successful participant create -- the participant is still returned
    and persisted."""
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)

    async def _boom(_participant):
        raise RuntimeError("simulated signal failure")

    monkeypatch.setattr(service.contact_engagement_signal_service, "reconcile_from_participant", _boom)

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "role": "guest", "rsvp_status": "confirmed"}
    )
    assert await store.get(participant.participant_id) is not None


# =====================================================================
# Engagement Participant canonical Contact display -- Client CRM Stage 4A
# (2026-09-11). list_engagement_participants() now returns
# EngagementParticipantView; resolved_name/resolved_title/resolved_company/
# resolved_profile_photo_url prefer the CURRENT linked Contact, falling
# back to the participant's own (untouched) snapshot fields. See
# EngagementParticipantView's own model docstring for the full precedence.
# =====================================================================


def _seed_stage4a_participant(client_id, engagement_id, crm_contact_id=None, participant_id="p1", **overrides) -> EngagementParticipant:
    """Same shape as _seed_participant() below, but with NO default
    first_name -- Stage 4A's own tests need a genuinely blank snapshot as
    their normal starting point, unlike Stage 3A's tests which default to
    a named "Jane"."""
    now = datetime.now(timezone.utc)
    overrides.setdefault("first_name", None)
    overrides.setdefault("last_name", None)
    overrides.setdefault("created_at", now)
    overrides.setdefault("updated_at", now)
    return EngagementParticipant(
        participant_id=participant_id, engagement_id=engagement_id, client_id=client_id,
        crm_contact_id=crm_contact_id, **overrides,
    )


async def test_resolved_name_prefers_current_contact_name(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name="John", last_name="Minter")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert isinstance(view, EngagementParticipantView)
    assert view.resolved_name == "John Minter"
    # The raw snapshot itself is untouched -- still blank, per Stage 4A's
    # own "never rewrite participant storage" rule.
    assert view.first_name is None
    assert view.last_name is None


async def test_resolved_title_prefers_current_contact_title(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, title="Founder and Managing Director")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", title="Old Title"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_title == "Founder and Managing Director"
    assert view.title == "Old Title"  # snapshot untouched


async def test_resolved_company_prefers_current_contact_company(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, company="Austin Growth Capital")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", company="OldCo"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_company == "Austin Growth Capital"
    assert view.company == "OldCo"  # snapshot untouched


async def test_resolved_profile_photo_url_from_current_contact(participant_service, monkeypatch):
    monkeypatch.setattr(settings, "profile_photo_cdn_base_url", "https://photos.astronomicconnect.com")
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, profile_photo_key="avatars/john.jpg")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_profile_photo_url is not None
    assert view.resolved_profile_photo_url.endswith("avatars/john.jpg")


async def test_resolved_name_falls_back_to_snapshot_when_contact_name_blank(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name=None, last_name=None)
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", first_name="Jane", last_name="Doe")
    )

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Jane Doe"


async def test_resolved_title_falls_back_to_snapshot_when_contact_title_blank(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, title=None)
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", title="Snapshot Title"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_title == "Snapshot Title"


async def test_resolved_company_falls_back_to_snapshot_when_contact_company_blank(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, company=None)
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", company="Snapshot Co"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_company == "Snapshot Co"


async def test_resolved_name_is_unnamed_participant_when_contact_and_snapshot_both_blank(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name=None, last_name=None)
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Unnamed participant"


async def test_resolved_name_contact_first_name_only(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name="John", last_name=None)
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "John"


async def test_resolved_name_contact_last_name_only(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name=None, last_name="Minter")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Minter"


async def test_whitespace_only_contact_name_does_not_suppress_snapshot_name(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name="   ", last_name="  ")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", first_name="Jane", last_name="Doe")
    )

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Jane Doe"


async def test_whitespace_only_contact_title_does_not_suppress_snapshot_title(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, title="   ")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", title="Snapshot Title"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_title == "Snapshot Title"


async def test_unresolved_participant_uses_snapshot_directly(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_stage4a_participant(
            client.client_id, engagement.engagement_id, crm_contact_id=None,
            first_name="Jane", last_name="Doe", title="Walk-in Title", company="Walk-in Co",
        )
    )

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Jane Doe"
    assert view.resolved_title == "Walk-in Title"
    assert view.resolved_company == "Walk-in Co"
    assert view.resolved_profile_photo_url is None


async def test_missing_linked_contact_falls_back_to_snapshot(participant_service):
    """crm_contact_id points to a Contact that no longer resolves (e.g.
    deleted from the store outright) -- must fail safe to the snapshot,
    never raise."""
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_stage4a_participant(
            client.client_id, engagement.engagement_id, crm_contact_id="does-not-exist",
            first_name="Jane", last_name="Doe",
        )
    )

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "Jane Doe"


async def test_archived_linked_contact_current_identity_still_wins(participant_service):
    """LOCKED Stage 4A decision: archived does not mean forgotten -- an
    archived Contact's CURRENT name/title/company still wins over the
    participant snapshot, exactly like an active Contact would."""
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(
        crm_contact_store, first_name="John", last_name="Minter", title="Founder", company="Austin Growth Capital", archived=True
    )
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.resolved_name == "John Minter"
    assert view.resolved_title == "Founder"
    assert view.resolved_company == "Austin Growth Capital"


async def test_multiple_linked_participants_use_exactly_one_bulk_contact_call(participant_service, monkeypatch):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-1", first_name="Ethan")
    await _seed_crm_contact(crm_contact_store, crm_contact_id="ethan-2", first_name="Priya")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", participant_id="p1"))
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-2", participant_id="p2"))
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id=None, participant_id="p3"))

    call_count = 0
    original_list_by_ids = crm_contact_store.list_by_ids

    async def _counting_list_by_ids(ids):
        nonlocal call_count
        call_count += 1
        return await original_list_by_ids(ids)

    monkeypatch.setattr(crm_contact_store, "list_by_ids", _counting_list_by_ids)

    views = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert len(views) == 3
    assert call_count == 1


async def test_duplicate_crm_contact_ids_are_deduped_in_the_bulk_lookup(participant_service, monkeypatch):
    """Two participant rows referencing the SAME crm_contact_id within one
    Engagement -- store.create()'s own duplicate guard would normally
    prevent this even across archived/active (see
    EngagementParticipantDuplicateError), so this seeds the second row
    directly into the Memory store's internal dict to construct the edge
    case regardless; the bulk lookup must still never pass that id twice."""
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name="Ethan")
    client, engagement = await _make_client_and_engagement(service)
    p1 = _seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", participant_id="p1", archived=True)
    p2 = _seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1", participant_id="p2")
    store._rows[p1.participant_id] = p1
    store._rows[p2.participant_id] = p2

    seen_ids = []
    original_list_by_ids = crm_contact_store.list_by_ids

    async def _spying_list_by_ids(ids):
        seen_ids.append(list(ids))
        return await original_list_by_ids(ids)

    monkeypatch.setattr(crm_contact_store, "list_by_ids", _spying_list_by_ids)

    views = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert len(views) == 2
    assert len(seen_ids) == 1
    assert len(seen_ids[0]) == len(set(seen_ids[0]))


async def test_participant_ordering_is_unchanged_by_the_projection(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    base = datetime.now(timezone.utc)
    await store.create(
        _seed_stage4a_participant(
            client.client_id, engagement.engagement_id, participant_id="p-second", first_name="Second", created_at=base + timedelta(seconds=1)
        )
    )
    await store.create(
        _seed_stage4a_participant(
            client.client_id, engagement.engagement_id, participant_id="p-first", first_name="First", created_at=base
        )
    )

    raw = await store.list_for_engagement(engagement.engagement_id)
    views = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert [p.participant_id for p in views] == [p.participant_id for p in raw]


async def test_list_engagement_participants_makes_zero_writes(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    contact = await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    participant = _seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1")
    await store.create(participant)

    await service.list_engagement_participants(client.client_id, engagement.engagement_id)

    unchanged_contact = await crm_contact_store.get("ethan-1")
    unchanged_participant = await store.get(participant.participant_id)
    assert unchanged_contact == contact
    assert unchanged_participant == participant


async def test_raw_snapshot_fields_remain_unchanged_in_the_view_response(participant_service):
    service, _activity_log, store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store, first_name="John", last_name="Minter", title="Founder", company="Austin Growth Capital")
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_stage4a_participant(client.client_id, engagement.engagement_id, crm_contact_id="ethan-1"))

    [view] = await service.list_engagement_participants(client.client_id, engagement.engagement_id)
    assert view.first_name is None
    assert view.last_name is None
    assert view.title is None
    assert view.company is None


# =====================================================================
# Contact Event History -- Contacts CRM Stage 3A (2026-09-11)
# =====================================================================


def _seed_participant(participant_id="p1", engagement_id="e1", client_id="c1", crm_contact_id=None, **overrides) -> EngagementParticipant:
    now = datetime.now(timezone.utc)
    overrides.setdefault("first_name", "Jane")
    overrides.setdefault("created_at", now)
    overrides.setdefault("updated_at", now)
    return EngagementParticipant(
        participant_id=participant_id, engagement_id=engagement_id, client_id=client_id,
        crm_contact_id=crm_contact_id, **overrides,
    )


async def test_manual_participant_appears_with_no_luma_registration(participant_service):
    service, _activity_log, _store, crm_contact_store = participant_service
    await _seed_crm_contact(crm_contact_store)
    client, engagement = await _make_client_and_engagement(service)
    await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": "ethan-1", "rsvp_status": "confirmed"}
    )

    history = await service.list_contact_event_history("ethan-1")
    assert len(history) == 1
    assert history[0].source == "manual"
    assert history[0].rsvp_status == "confirmed"
    assert history[0].engagement_id == engagement.engagement_id


async def test_luma_participant_appears(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_participant("p1", engagement.engagement_id, client.client_id, crm_contact_id="contact-1", source="luma", rsvp_status="confirmed")
    )

    history = await service.list_contact_event_history("contact-1")
    assert len(history) == 1
    assert history[0].source == "luma"


async def test_manual_and_luma_sources_produce_identical_row_structure(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement_a = await _make_client_and_engagement(service, title="Dinner A")
    _client_b, engagement_b = await _make_client_and_engagement(service, title="Dinner B")
    await store.create(_seed_participant("p1", engagement_a.engagement_id, client.client_id, crm_contact_id="contact-1", source="manual", role="guest", rsvp_status="confirmed"))
    await store.create(_seed_participant("p2", engagement_b.engagement_id, client.client_id, crm_contact_id="contact-1", source="luma", role="guest", rsvp_status="confirmed"))

    history = await service.list_contact_event_history("contact-1")
    assert len(history) == 2
    manual_entry = next(e for e in history if e.source == "manual")
    luma_entry = next(e for e in history if e.source == "luma")
    assert type(manual_entry) is type(luma_entry)
    assert manual_entry.role == luma_entry.role == "guest"
    assert manual_entry.rsvp_status == luma_entry.rsvp_status == "confirmed"
    # Identical field set on both -- differing only in the fields that are
    # genuinely different facts (engagement_id/participant_id/event_name/source).
    assert set(manual_entry.model_dump().keys()) == set(luma_entry.model_dump().keys())


async def test_exactly_one_row_per_contact_engagement_pair(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_participant("p1", engagement.engagement_id, client.client_id, crm_contact_id="contact-1"))

    history = await service.list_contact_event_history("contact-1")
    assert len(history) == 1


def test_list_contact_event_history_never_references_luma_registration_source():
    """Structural regression guard: the projection must derive SOLELY from
    EngagementParticipant -- never read LumaRegistration, even if a stale
    or duplicate Luma registration exists for the same Contact+Engagement.
    Checks the method's own docstring is stripped first -- it legitimately
    NAMES LumaRegistration in prose to explain why it's NOT read; this
    guards the actual code body only."""
    import inspect

    source = inspect.getsource(ClientCrmService.list_contact_event_history)
    body = source.split('"""', 2)[-1]  # everything after the closing docstring quotes
    assert "LumaRegistration" not in body
    assert "registration_store" not in body


async def test_archived_participant_excluded(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement_a = await _make_client_and_engagement(service, title="Active Dinner")
    _client_b, engagement_b = await _make_client_and_engagement(service, title="Archived Dinner")
    await store.create(_seed_participant("p1", engagement_a.engagement_id, client.client_id, crm_contact_id="contact-1"))
    await store.create(_seed_participant("p2", engagement_b.engagement_id, client.client_id, crm_contact_id="contact-1", archived=True))

    history = await service.list_contact_event_history("contact-1")
    assert [e.event_name for e in history] == ["Active Dinner"]


@pytest.mark.parametrize(
    "rsvp_status,attendance_status",
    [
        ("invited", None),
        ("declined", None),
        ("confirmed", None),
        (None, "attended"),
        (None, "no_show"),
        (None, "cancelled"),
        (None, None),
    ],
)
async def test_every_rsvp_and_attendance_combination_displays_correctly(participant_service, rsvp_status, attendance_status):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(
        _seed_participant(
            "p1", engagement.engagement_id, client.client_id, crm_contact_id="contact-1",
            rsvp_status=rsvp_status, attendance_status=attendance_status,
        )
    )

    history = await service.list_contact_event_history("contact-1")
    assert history[0].rsvp_status == rsvp_status
    assert history[0].attendance_status == attendance_status


@pytest.mark.parametrize("role", ["guest", "client", "host", "speaker_panelist", "astronomic_team", "other"])
async def test_every_role_may_appear_unfiltered(participant_service, role):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(service)
    await store.create(_seed_participant("p1", engagement.engagement_id, client.client_id, crm_contact_id="contact-1", role=role))

    history = await service.list_contact_event_history("contact-1")
    assert history[0].role == role


async def test_engagement_and_client_fields_resolve_correctly(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement = await _make_client_and_engagement(
        service, title="Austin Donor Dinner", engagement_type="dinner", dinner_type="donor_dinner", engagement_date=date(2026, 10, 8)
    )
    await store.create(_seed_participant("p1", engagement.engagement_id, client.client_id, crm_contact_id="contact-1"))

    entry = (await service.list_contact_event_history("contact-1"))[0]
    assert entry.event_name == "Austin Donor Dinner"
    assert entry.client_name == "Hive ASMBLD"
    assert entry.engagement_date == date(2026, 10, 8)
    assert entry.engagement_type == "dinner"
    assert entry.dinner_type == "donor_dinner"


async def test_ordering_newest_first_nulls_last_deterministic_tie_break(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    c1, e_sept = await _make_client_and_engagement(service, title="September Dinner", engagement_date=date(2026, 9, 1))
    c2, e_oct = await _make_client_and_engagement(service, title="October Dinner", engagement_date=date(2026, 10, 1))
    c3, e_null = await _make_client_and_engagement(service, title="No Date Dinner", engagement_date=None)
    c4, e_tie_b = await _make_client_and_engagement(service, title="Tie B", engagement_date=date(2026, 9, 1))

    # Two Engagements share the same date (Sept 1) -- tie-break must be
    # deterministic by participant_id ascending.
    await store.create(_seed_participant("p-zzz", e_sept.engagement_id, c1.client_id, crm_contact_id="contact-1"))
    await store.create(_seed_participant("p-aaa", e_tie_b.engagement_id, c4.client_id, crm_contact_id="contact-1"))
    await store.create(_seed_participant("p-oct", e_oct.engagement_id, c2.client_id, crm_contact_id="contact-1"))
    await store.create(_seed_participant("p-null", e_null.engagement_id, c3.client_id, crm_contact_id="contact-1"))

    history = await service.list_contact_event_history("contact-1")
    assert [e.event_name for e in history] == ["October Dinner", "Tie B", "September Dinner", "No Date Dinner"]


async def test_cross_contact_isolation(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    client, engagement_a = await _make_client_and_engagement(service, title="Contact A's Dinner")
    _client_b, engagement_b = await _make_client_and_engagement(service, title="Contact B's Dinner")
    await store.create(_seed_participant("p1", engagement_a.engagement_id, client.client_id, crm_contact_id="contact-a"))
    await store.create(_seed_participant("p2", engagement_b.engagement_id, client.client_id, crm_contact_id="contact-b"))

    history_a = await service.list_contact_event_history("contact-a")
    history_b = await service.list_contact_event_history("contact-b")
    assert [e.event_name for e in history_a] == ["Contact A's Dinner"]
    assert [e.event_name for e in history_b] == ["Contact B's Dinner"]


async def test_participant_with_no_matching_engagement_is_skipped_not_crashed(participant_service):
    """Fail-safe malformed-reference behavior: a participant row whose
    engagement_id doesn't resolve to a real Engagement is silently
    omitted, never raised -- one bad historical row must not break an
    entire Contact's Event History."""
    service, _activity_log, store, _crm_contact_store = participant_service
    await store.create(_seed_participant("p1", "engagement-does-not-exist", "client-does-not-exist", crm_contact_id="contact-1"))

    history = await service.list_contact_event_history("contact-1")
    assert history == []


async def test_participant_whose_engagement_client_is_missing_is_skipped_not_crashed(participant_service):
    service, _activity_log, store, _crm_contact_store = participant_service
    engagement_store = service.engagement_store
    now = datetime.now(timezone.utc)
    orphaned_engagement = Engagement(
        engagement_id="e-orphan", client_id="client-does-not-exist", title="Orphaned Dinner",
        engagement_type=EngagementType.DINNER, created_at=now, updated_at=now,
    )
    await engagement_store.create(orphaned_engagement)
    await store.create(_seed_participant("p1", "e-orphan", "client-does-not-exist", crm_contact_id="contact-1"))

    history = await service.list_contact_event_history("contact-1")
    assert history == []


async def test_no_per_participant_engagement_query_pattern(participant_service):
    """Structural regression guard: exactly one list_by_ids() call and one
    client_store.list() call regardless of how many participations a
    Contact has -- never one query per participant/Engagement."""
    service, _activity_log, store, _crm_contact_store = participant_service
    engagement_store = service.engagement_store
    client_store = service.client_store

    engagement_ids = []
    for i in range(5):
        client, engagement = await _make_client_and_engagement(service, title=f"Dinner {i}")
        await store.create(_seed_participant(f"p{i}", engagement.engagement_id, client.client_id, crm_contact_id="contact-1"))
        engagement_ids.append(engagement.engagement_id)

    call_counts = {"list_by_ids": 0, "client_list": 0, "engagement_get": 0}
    original_list_by_ids = engagement_store.list_by_ids
    original_client_list = client_store.list
    original_engagement_get = engagement_store.get

    async def _counted_list_by_ids(ids):
        call_counts["list_by_ids"] += 1
        return await original_list_by_ids(ids)

    async def _counted_client_list():
        call_counts["client_list"] += 1
        return await original_client_list()

    async def _counted_engagement_get(engagement_id):
        call_counts["engagement_get"] += 1
        return await original_engagement_get(engagement_id)

    engagement_store.list_by_ids = _counted_list_by_ids
    client_store.list = _counted_client_list
    engagement_store.get = _counted_engagement_get

    history = await service.list_contact_event_history("contact-1")
    assert len(history) == 5
    assert call_counts == {"list_by_ids": 1, "client_list": 1, "engagement_get": 0}


async def test_kevin_przybocki_synthetic_regression(participant_service):
    """The primary Stage 3A acceptance case: a Contact manually added to
    a Miracle-Foundation-like Engagement as Guest/Confirmed via Manual
    source, with NO LumaRegistration anywhere, must appear in their own
    Event History with exactly these fields."""
    service, activity_log, _store, crm_contact_store = participant_service
    kevin = await _seed_crm_contact(
        crm_contact_store, crm_contact_id="kevin-przybocki", first_name="Kevin", last_name="Przybocki", email="kevin@example.com"
    )
    client, engagement = await _make_client_and_engagement(
        service, title="Austin Donor Dinner", engagement_type="dinner", dinner_type="donor_dinner", engagement_date=date(2026, 10, 8)
    )
    client = await service.update_client(client.client_id, {"name": "Miracle Foundation"})

    participant = await service.create_engagement_participant(
        client.client_id, engagement.engagement_id, {"crm_contact_id": kevin.crm_contact_id, "role": "guest", "rsvp_status": "confirmed"}
    )
    assert participant.source == "manual"  # server-owned, confirms no Luma involvement was even possible here

    history = await service.list_contact_event_history(kevin.crm_contact_id)
    assert len(history) == 1
    entry = history[0]
    assert entry.event_name == "Austin Donor Dinner"
    assert entry.client_name == "Miracle Foundation"
    assert entry.role == "guest"
    assert entry.rsvp_status == "confirmed"
    assert entry.source == "manual"


# =====================================================================
# ClientTouchpoint -- Client CRM Stage 2A (2026-09-11)
# =====================================================================


async def _make_client_with_linked_contact(service, crm_contact_store, **contact_overrides):
    contact = await _seed_crm_contact(crm_contact_store, **contact_overrides)
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_contact(client.client_id, {"crm_contact_id": contact.crm_contact_id})
    return client, contact


# --- Create -------------------------------------------------------------


async def test_create_touchpoint_without_contact_succeeds(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"contact_type": "email", "contacted_by": "Ria"}
    )
    assert touchpoint.crm_contact_id is None
    assert touchpoint.contact_name is None
    assert touchpoint.client_id == client.client_id
    assert touchpoint.archived is False


async def test_create_touchpoint_with_valid_linked_client_contact_succeeds(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(service, crm_contact_store)
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "call", "contacted_by": "Ria"}
    )
    assert touchpoint.crm_contact_id == contact.crm_contact_id


async def test_create_touchpoint_populates_contact_name_snapshot(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(
        service, crm_contact_store, first_name="Sid", last_name="Atkinson"
    )
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "email", "contacted_by": "Ria"}
    )
    assert touchpoint.contact_name == "Sid Atkinson"


async def test_create_touchpoint_nonexistent_contact_rejected(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(
            client.client_id, {"crm_contact_id": "does-not-exist", "contact_type": "email", "contacted_by": "Ria"}
        )


async def test_create_touchpoint_contact_belonging_to_another_client_rejected(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    other_client, contact = await _make_client_with_linked_contact(service, crm_contact_store)
    this_client = await service.create_client({"name": "Applied Curiosity"})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(
            this_client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "email", "contacted_by": "Ria"}
        )


async def test_create_touchpoint_global_contact_not_linked_through_client_contact_rejected(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    contact = await _seed_crm_contact(crm_contact_store)  # exists in the CRM, but never linked to any Client
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(
            client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "email", "contacted_by": "Ria"}
        )


async def test_create_touchpoint_archived_client_contact_rejected(touchpoint_service):
    service, _activity_log, client_contact_store, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(service, crm_contact_store)
    client_contacts = await client_contact_store.list_for_client(client.client_id)
    await service.update_client_contact(client.client_id, client_contacts[0].client_contact_id, {"archived": True})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(
            client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "email", "contacted_by": "Ria"}
        )


async def test_create_touchpoint_invalid_contact_type_rejected(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(client.client_id, {"contact_type": "carrier_pigeon", "contacted_by": "Ria"})


async def test_create_touchpoint_blank_contacted_by_rejected(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(client.client_id, {"contact_type": "email", "contacted_by": "   "})
    with pytest.raises(ValueError):
        await service.create_client_touchpoint(client.client_id, {"contact_type": "email"})


async def test_create_touchpoint_note_is_optional(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    assert touchpoint.note is None


async def test_create_touchpoint_defaults_occurred_at_to_now(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    before = datetime.now(timezone.utc)
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    after = datetime.now(timezone.utc)
    assert before <= touchpoint.occurred_at <= after


async def test_create_touchpoint_requires_a_real_client(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    with pytest.raises(ClientNotFound):
        await service.create_client_touchpoint("does-not-exist", {"contact_type": "email", "contacted_by": "Ria"})


# --- Update ---------------------------------------------------------------


async def test_update_touchpoint_normal_fields(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    updated = await service.update_client_touchpoint(
        client.client_id, touchpoint.touchpoint_id, {"note": "Left a voicemail.", "contact_type": "email"}
    )
    assert updated.note == "Left a voicemail."
    assert updated.contact_type == "email"
    assert updated.contacted_by == "Ria"  # untouched


async def test_update_touchpoint_changing_crm_contact_id_refreshes_snapshot(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, _contact_a = await _make_client_with_linked_contact(
        service, crm_contact_store, crm_contact_id="a-1", first_name="Alice", last_name="Anders"
    )
    contact_b = await _seed_crm_contact(crm_contact_store, crm_contact_id="b-1", first_name="Bob", last_name=None)
    await service.create_client_contact(client.client_id, {"crm_contact_id": "b-1"})

    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"crm_contact_id": "a-1", "contact_type": "call", "contacted_by": "Ria"}
    )
    assert touchpoint.contact_name == "Alice Anders"

    updated = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"crm_contact_id": "b-1"})
    assert updated.crm_contact_id == "b-1"
    assert updated.contact_name == "Bob"


async def test_update_touchpoint_clearing_crm_contact_id_clears_snapshot(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(service, crm_contact_store, first_name="Sid")
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "call", "contacted_by": "Ria"}
    )
    updated = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"crm_contact_id": None})
    assert updated.crm_contact_id is None
    assert updated.contact_name is None


async def test_update_touchpoint_archive(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    archived = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": True})
    assert archived.archived is True


async def test_update_touchpoint_restore(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": True})
    restored = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": False})
    assert restored.archived is False


async def test_update_touchpoint_empty_patch_leaves_updated_at_unchanged(touchpoint_service):
    """Stage 2A's own approved correction: a TRUE no-op (empty patch)
    must NOT bump updated_at at all -- stricter than update_client()'s
    own "always writes on PATCH" base case, because updated_at on a
    Touchpoint is meant to represent a real change to the historical
    interaction record, not merely that a PATCH request arrived."""
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    updated = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {})
    assert updated.updated_at == touchpoint.updated_at
    assert updated == touchpoint


async def test_update_touchpoint_same_value_patch_leaves_updated_at_unchanged(touchpoint_service):
    """A patch whose every value already matches the stored row is
    equally a TRUE no-op, even though it isn't literally empty."""
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"contact_type": "call", "contacted_by": "Ria", "note": "Intro call."}
    )
    updated = await service.update_client_touchpoint(
        client.client_id, touchpoint.touchpoint_id, {"contact_type": "call", "contacted_by": "Ria", "note": "Intro call."}
    )
    assert updated.updated_at == touchpoint.updated_at
    assert updated == touchpoint


async def test_update_touchpoint_material_change_does_change_updated_at(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    updated = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"note": "Left a voicemail."})
    assert updated.updated_at > touchpoint.updated_at


async def test_update_touchpoint_archive_and_restore_still_change_updated_at(touchpoint_service):
    """Archiving/restoring is never treated as a no-op, even though a
    caller might think of it as "just a flag flip" -- it's a real,
    material change to the row."""
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})

    archived = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": True})
    assert archived.updated_at > touchpoint.updated_at

    restored = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": False})
    assert restored.updated_at > archived.updated_at


async def test_update_touchpoint_invalid_contact_type_rejected(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    with pytest.raises(ValueError):
        await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"contact_type": "fax"})


async def test_update_touchpoint_blank_contacted_by_rejected(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    with pytest.raises(ValueError):
        await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"contacted_by": "  "})


async def test_update_touchpoint_missing_is_not_found(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    with pytest.raises(ClientTouchpointNotFound):
        await service.update_client_touchpoint(client.client_id, "does-not-exist", {"note": "x"})


async def test_touchpoint_requested_through_wrong_client_is_not_found(touchpoint_service):
    """A Touchpoint belonging to Client A must never be exposed or
    mutable through Client B's URL -- same isolation rule as every other
    Client CRM entity."""
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    client_b = await service.create_client({"name": "Other Co"})
    touchpoint = await service.create_client_touchpoint(client_a.client_id, {"contact_type": "call", "contacted_by": "Ria"})

    with pytest.raises(ClientTouchpointNotFound):
        await service.update_client_touchpoint(client_b.client_id, touchpoint.touchpoint_id, {"note": "hijacked"})


async def test_canonical_contact_changing_later_never_rewrites_an_existing_snapshot(touchpoint_service):
    service, _activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(service, crm_contact_store, first_name="Sid", last_name="Atkinson")
    touchpoint = await service.create_client_touchpoint(
        client.client_id, {"crm_contact_id": contact.crm_contact_id, "contact_type": "call", "contacted_by": "Ria"}
    )
    assert touchpoint.contact_name == "Sid Atkinson"

    # The canonical Contact is renamed AFTER the Touchpoint was created --
    # nothing about that write should ever touch the existing Touchpoint.
    renamed = contact.model_copy(update={"first_name": "Sidney", "updated_at": datetime.now(timezone.utc)})
    await crm_contact_store.save(renamed)

    unrelated_update = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"note": "unrelated"})
    assert unrelated_update.contact_name == "Sid Atkinson"  # still the ORIGINAL snapshot


async def test_list_client_touchpoints_scoped_to_client_newest_first(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client_a = await service.create_client({"name": "Hive ASMBLD"})
    client_b = await service.create_client({"name": "Other Co"})
    t1 = await service.create_client_touchpoint(
        client_a.client_id, {"contact_type": "call", "contacted_by": "Ria", "occurred_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    )
    t2 = await service.create_client_touchpoint(
        client_a.client_id, {"contact_type": "email", "contacted_by": "Ria", "occurred_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    )
    await service.create_client_touchpoint(client_b.client_id, {"contact_type": "call", "contacted_by": "Chris"})

    touchpoints = await service.list_client_touchpoints(client_a.client_id)
    assert [t.touchpoint_id for t in touchpoints] == [t2.touchpoint_id, t1.touchpoint_id]


async def test_list_client_touchpoints_excludes_archived_by_default(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    active = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    to_archive = await service.create_client_touchpoint(client.client_id, {"contact_type": "email", "contacted_by": "Ria"})
    await service.update_client_touchpoint(client.client_id, to_archive.touchpoint_id, {"archived": True})

    touchpoints = await service.list_client_touchpoints(client.client_id)
    assert [t.touchpoint_id for t in touchpoints] == [active.touchpoint_id]


async def test_list_client_touchpoints_include_archived_true_returns_both(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    active = await service.create_client_touchpoint(
        client.client_id, {"contact_type": "call", "contacted_by": "Ria", "occurred_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}
    )
    to_archive = await service.create_client_touchpoint(
        client.client_id, {"contact_type": "email", "contacted_by": "Ria", "occurred_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    )
    await service.update_client_touchpoint(client.client_id, to_archive.touchpoint_id, {"archived": True})

    touchpoints = await service.list_client_touchpoints(client.client_id, include_archived=True)
    # Both present, and ordering is still deterministic newest-first --
    # filtering never reorders the already-sorted list.
    assert [t.touchpoint_id for t in touchpoints] == [active.touchpoint_id, to_archive.touchpoint_id]
    assert touchpoints[1].archived is True


async def test_list_client_touchpoints_requires_a_real_client(touchpoint_service):
    service, _activity_log, _cc, _crm, _tp = touchpoint_service
    with pytest.raises(ClientNotFound):
        await service.list_client_touchpoints("does-not-exist")


# --- Activity Log -----------------------------------------------------------


async def test_create_touchpoint_logs_exactly_one_created_activity(touchpoint_service):
    service, activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria", "note": "secret note"})

    events = (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items
    touchpoint_events = [e for e in events if e.event_type == "client_touchpoint.created"]
    assert len(touchpoint_events) == 1


async def test_material_touchpoint_update_logs_updated(touchpoint_service):
    service, activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"note": "Left a voicemail."})

    events = (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items
    assert any(e.event_type == "client_touchpoint.updated" for e in events)


async def test_touchpoint_archive_logs_archived(touchpoint_service):
    service, activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": True})

    events = (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items
    assert any(e.event_type == "client_touchpoint.archived" for e in events)
    assert not any(e.event_type == "client_touchpoint.updated" for e in events)


async def test_touchpoint_restore_logs_restored(touchpoint_service):
    service, activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": True})
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"archived": False})

    events = (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items
    assert any(e.event_type == "client_touchpoint.restored" for e in events)


async def test_touchpoint_no_op_update_produces_no_activity_entry(touchpoint_service):
    service, activity_log, _cc, _crm, _tp = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})
    before_count = len((await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items)

    # Re-sending the SAME value is a genuine no-op -- nothing material changes.
    await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {"contacted_by": "Ria"})

    after_count = len((await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items)
    assert after_count == before_count


async def test_touchpoint_true_no_op_never_reaches_the_store(touchpoint_service):
    """Stronger than the updated_at assertion alone -- monkeypatches the
    store's own save() to fail the test if it's ever called, proving a
    true no-op PATCH exits before any write attempt."""
    service, _activity_log, _cc, _crm, client_touchpoint_store = touchpoint_service
    client = await service.create_client({"name": "Hive ASMBLD"})
    touchpoint = await service.create_client_touchpoint(client.client_id, {"contact_type": "call", "contacted_by": "Ria"})

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("save() must never be called for a true no-op PATCH")

    client_touchpoint_store.save = _fail_if_called

    updated = await service.update_client_touchpoint(client.client_id, touchpoint.touchpoint_id, {})
    assert updated == touchpoint


async def test_touchpoint_activity_metadata_has_no_pii(touchpoint_service):
    service, activity_log, _cc, crm_contact_store, _tp = touchpoint_service
    client, contact = await _make_client_with_linked_contact(service, crm_contact_store, first_name="Sid", last_name="Atkinson")
    await service.create_client_touchpoint(
        client.client_id,
        {
            "crm_contact_id": contact.crm_contact_id,
            "contact_type": "email",
            "contacted_by": "Ria",
            "note": "Discussed Q4 budget and the CEO's private phone number 555-1234.",
        },
    )

    events = (await activity_log.list_events(category=ActivityCategory.CLIENT_CRM)).items
    touchpoint_event = next(e for e in events if e.event_type == "client_touchpoint.created")
    assert touchpoint_event.entity_name is None
    metadata_str = str(touchpoint_event.metadata)
    for forbidden in ("Ria", "Sid Atkinson", "555-1234", "Discussed Q4 budget", contact.crm_contact_id):
        assert forbidden not in metadata_str
    assert set(touchpoint_event.metadata.keys()) <= {"client_id", "contact_type", "fields_updated"}
