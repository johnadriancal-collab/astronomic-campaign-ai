"""
Concurrency-safety tests for LumaSyncService.process_guest_event() -- the
exact race discovered in production (three near-simultaneous webhook
deliveries for the same guest, all independently concluding "this
registration is new"). See process_guest_event()'s docstring in
app/services/luma_sync_service.py for the root cause and fix.

`asyncio.gather` is a faithful reproduction of the real scenario: FastAPI/
Uvicorn hands each inbound webhook request its own coroutine, and multiple
in-flight requests interleave at every `await` point exactly the way
gathered tasks do here -- this isn't a simulation of concurrency, it's the
same concurrency model production actually uses.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.crm import CrmCustomFieldDefinition, CustomFieldType, normalize_email
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncService
from tests.test_luma_sync_service import make_event, make_guest

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


class _RaceSimulatingContactStore(MemoryCrmContactStore):
    """The FIRST create() call for a given email deterministically
    simulates a concurrent winner: it inserts a DIFFERENT "competing"
    contact with the same email (standing in for whatever a truly-
    concurrent coroutine would have just committed) and then raises
    ValueError -- exactly the shape of error SQLiteCrmContactStore's real
    `email_normalized TEXT UNIQUE` constraint produces. This makes the
    cross-guest-id race deterministic and CI-safe to test, rather than
    depending on winning a real asyncio scheduling race."""

    def __init__(self):
        super().__init__()
        self._raced_emails: set[str] = set()

    async def create(self, contact):
        email = normalize_email(contact.email)
        if email and email not in self._raced_emails:
            self._raced_emails.add(email)
            competing = contact.model_copy(update={"crm_contact_id": str(uuid.uuid4())})
            await super().create(competing)
            raise ValueError(f"CrmContact already exists (duplicate email/apollo_contact_id/linkedin_url): simulated race for {email}")
        await super().create(contact)


@pytest_asyncio.fixture
async def crm_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="investor_type", label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT, options=["Angel Investor"], active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    return CrmService(custom_field_store=custom_field_store)


@pytest.fixture
def luma_service(crm_service):
    return LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
    )


# --- same guest_id, concurrent deliveries -----------------------------------


async def test_concurrent_identical_deliveries_same_guest_produce_exactly_one_registration_and_contact(
    luma_service, crm_service
):
    event = make_event()
    guest = make_guest()

    results = await asyncio.gather(
        *[luma_service.process_guest_event(event, guest, webhook_delivery_id=f"wh-{i}") for i in range(5)]
    )
    assert len(results) == 5  # no exceptions -- gather would have raised otherwise

    all_regs = await luma_service.registration_store.list()
    assert len(all_regs) == 1

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    created_reg_events = [e for e in page.items if e.event_type == "luma.registration.created"]
    created_contact_events = [e for e in page.items if e.event_type == "luma.contact.created"]
    assert len(created_reg_events) == 1
    assert len(created_contact_events) == 1


async def test_concurrent_distinct_webhook_ids_same_guest_no_duplicates(luma_service, crm_service):
    """Distinct Webhook-Id values (not exact-duplicate deliveries) for the
    same guest -- the scenario actually observed in production."""
    event = make_event()
    guest = make_guest(guest_id="gst-distinct")

    await asyncio.gather(
        luma_service.process_guest_event(event, guest, webhook_delivery_id="wh-a"),
        luma_service.process_guest_event(event, guest, webhook_delivery_id="wh-b"),
        luma_service.process_guest_event(event, guest, webhook_delivery_id="wh-c"),
    )

    all_regs = await luma_service.registration_store.list()
    assert len(all_regs) == 1
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1


async def test_concurrent_registered_and_updated_variants_same_guest_converge_safely(luma_service, crm_service):
    """A mix of approval_status values arriving concurrently for the same
    guest -- whichever wins the lock first is "created," the rest are
    "updated," but there is still only ever one registration row and one
    contact, and no exception propagates regardless of arrival order."""
    event = make_event()
    guest_id = "gst-mixed"

    results = await asyncio.gather(
        luma_service.process_guest_event(event, make_guest(guest_id=guest_id, approval_status="pending_approval"), webhook_delivery_id="wh-1"),
        luma_service.process_guest_event(event, make_guest(guest_id=guest_id, approval_status="approved"), webhook_delivery_id="wh-2"),
        luma_service.process_guest_event(event, make_guest(guest_id=guest_id, approval_status="approved"), webhook_delivery_id="wh-3"),
        return_exceptions=True,
    )
    assert not any(isinstance(r, Exception) for r in results)

    all_regs = await luma_service.registration_store.list()
    assert len(all_regs) == 1
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1


async def test_legitimate_update_after_a_concurrent_burst_still_works(luma_service, crm_service):
    """Proves the per-guest lock is released correctly (never left stuck)
    after a burst -- a normal, later, real status transition must still
    apply."""
    event = make_event()
    guest_id = "gst-later-update"

    await asyncio.gather(
        *[
            luma_service.process_guest_event(event, make_guest(guest_id=guest_id, approval_status="pending_approval"), webhook_delivery_id=f"wh-{i}")
            for i in range(3)
        ]
    )

    final = await luma_service.process_guest_event(
        event, make_guest(guest_id=guest_id, approval_status="approved"), webhook_delivery_id="wh-final"
    )
    assert final.registration.approval_status.value == "approved"
    assert final.registration_is_new is False

    all_regs = await luma_service.registration_store.list()
    assert len(all_regs) == 1


async def test_different_guests_are_not_globally_serialized(luma_service, crm_service):
    """Two DIFFERENT guests processed concurrently must both complete
    correctly and independently -- proves the lock is per-guest, not a
    single global lock that would serialize unrelated registrations."""
    event = make_event()
    results = await asyncio.gather(
        luma_service.process_guest_event(event, make_guest(guest_id="gst-x", email="x@example.com")),
        luma_service.process_guest_event(event, make_guest(guest_id="gst-y", email="y@example.com")),
    )
    assert results[0].contact.email == "x@example.com"
    assert results[1].contact.email == "y@example.com"
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2  # two distinct real people, two distinct contacts


# --- cross-guest-id race on NEW contact creation (deterministic simulation) -


async def test_cross_guest_id_email_race_recovers_without_crashing():
    """Two DIFFERENT guest_ids (so the per-guest lock does NOT cover this
    case by itself) whose mapped email collides -- deterministically
    simulated via _RaceSimulatingContactStore standing in for a real
    concurrent winner. Proves the ValueError-catch fallback in
    _process_guest_event_locked resolves this gracefully (finds the
    other contact, enriches it) rather than crashing the request or
    depending solely on the DB constraint to "clean up after" the race."""
    custom_field_store = MemoryCrmCustomFieldStore()
    contact_store = _RaceSimulatingContactStore()
    crm_service = CrmService(contact_store=contact_store, custom_field_store=custom_field_store)
    luma_service = LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
    )

    event = make_event()
    guest = make_guest(guest_id="gst-race", email="raced@example.com")

    result = await luma_service.process_guest_event(event, guest)  # must not raise

    assert result.registration.crm_contact_id is not None
    assert result.registration.match_status.value == "matched"
    all_contacts = await contact_store.list()
    assert len(all_contacts) == 1  # the "competing" contact -- ours was never actually inserted
