"""
Dedicated ClientContact idempotency tests for the Sale Bot -> AstroHub
onboarding integration, requested as a pre-production hardening item
(2026-09-15). Two scenarios:

  1. The EXACT same sale request repeated -> same Client, same
     CrmContact, same ClientContact, same Engagement, zero duplicate
     relationships.
  2. Two DIFFERENT sales, same Client, same primary contact -> 1 Client,
     1 CrmContact, 1 ClientContact relationship, 2 Engagements. The
     second dinner must not create a second ClientContact row for the
     same Client + Contact pair.

Also documents (and demonstrates, not just asserts in prose) the actual
uniqueness posture of ClientContact(client_id, crm_contact_id): there is
NO database-level uniqueness constraint on that pair. This is a
pre-existing, DOCUMENTED Stage 1D decision (see
ClientContactStore.create()'s own abstract docstring: "this store does
not itself enforce that a given crm_contact_id can only be linked to one
ClientContact per Client (or at all) -- Stage 1D's own dedup/linking flow
owns that decision, not this store"), not something introduced by this
feature, and not something this stage silently changed -- the
client_contacts table is shared by the entire Client CRM (its own manual
UI included), so widening its constraints is a deliberate decision beyond
this feature's own scope, reported here rather than made unilaterally.

The Sale Bot -> AstroHub onboarding integration's own idempotency for
this relationship is therefore SERVICE-level (SaleOnboardingService.
_resolve_client_contact()'s own lookup-before-create, verified by the
tests below across both the same-sale-retried and different-sale
scenarios), not database-level.
"""

from datetime import datetime, timezone

import pytest

from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.sqlite_client_contact_store import SQLiteClientContactStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.crm_service import CrmService
from app.services.sale_onboarding_service import SaleOnboardingPayload, SaleOnboardingService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _services():
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    luma_event_store = MemoryLumaEventStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    client_crm_service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        engagement_store=engagement_store,
        engagement_closeout_store=engagement_closeout_store,
        engagement_participant_store=engagement_participant_store,
        luma_event_store=luma_event_store,
        client_touchpoint_store=client_touchpoint_store,
        contact_engagement_signal_service=signal_service,
    )
    crm_service = CrmService(contact_store=crm_contact_store, activity_log=activity_log)
    return SaleOnboardingService(client_crm_service=client_crm_service, crm_service=crm_service), client_crm_service


def _payload(**overrides) -> SaleOnboardingPayload:
    defaults = dict(
        sale_id="sale-001",
        docusign_envelope_id="env-001",
        mercury_invoice_id="merc-001",
        qb_invoice_id="qb-inv-001",
        qb_customer_id="qb-cust-001",
        qb_amount=5000.0,
        client_company="Hive ASMBLD",
        primary_contact="Jane Doe",
        contact_email="jane@hiveasmbld.com",
        signer_name="John Roe",
        signer_email="john@hiveasmbld.com",
        service_sold="Investor Dinner",
        city="Austin",
        event_date="10/15/2026",
        internal_owner="Chris",
    )
    defaults.update(overrides)
    return SaleOnboardingPayload(**defaults)


async def _counts(client_crm_service):
    clients = await client_crm_service.client_store.list()
    contacts = await client_crm_service.crm_contact_store.list()
    client_contacts = []
    for c in clients:
        client_contacts.extend(await client_crm_service.client_contact_store.list_for_client(c.client_id))
    engagements = []
    for c in clients:
        engagements.extend(await client_crm_service.engagement_store.list_for_client(c.client_id))
    return len(clients), len(contacts), len(client_contacts), len(engagements)


# --- Scenario 1: same sale request repeated -----------------------------


async def test_same_sale_request_repeated_produces_zero_duplicate_records():
    service, client_crm_service = await _services()
    payload = _payload()

    first = await service.onboard(payload)
    second = await service.onboard(payload)
    third = await service.onboard(payload)

    assert second.already_processed is True
    assert third.already_processed is True
    assert second.client_id == first.client_id
    assert second.crm_contact_id == first.crm_contact_id
    assert second.client_contact_id == first.client_contact_id
    assert second.engagement_id == first.engagement_id
    assert third.client_id == first.client_id
    assert third.crm_contact_id == first.crm_contact_id
    assert third.client_contact_id == first.client_contact_id
    assert third.engagement_id == first.engagement_id

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 1, 1, 1)


# --- Scenario 2: two different sales, same Client, same primary contact ---


async def test_two_different_sales_same_client_same_contact_produce_one_client_one_contact_one_relationship_two_engagements():
    service, client_crm_service = await _services()

    first = await service.onboard(
        _payload(sale_id="sale-001", docusign_envelope_id="env-001", event_date="10/15/2026")
    )
    second = await service.onboard(
        _payload(sale_id="sale-002", docusign_envelope_id="env-002", event_date="11/20/2026")
    )

    # Same Client.
    assert first.client_id == second.client_id
    assert second.created_new_client is False

    # Same CrmContact (same normalized email).
    assert first.crm_contact_id == second.crm_contact_id
    assert second.created_new_contact is False

    # Same ClientContact relationship row -- NOT a second one for the same pair.
    assert first.client_contact_id == second.client_contact_id

    # Two distinct Engagements -- one per dinner.
    assert first.engagement_id != second.engagement_id

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert n_clients == 1
    assert n_contacts == 1
    assert n_client_contacts == 1  # the second dinner did NOT create another ClientContact row
    assert n_engagements == 2


# --- Structural uniqueness posture (documented finding, demonstrated) -----


async def test_client_contact_store_has_no_database_level_uniqueness_constraint_memory():
    """Demonstrates the gap directly: bypassing SaleOnboardingService's own
    lookup-before-create and inserting two ClientContact rows for the
    exact same (client_id, crm_contact_id) pair succeeds at the STORE
    layer with no error -- there is no constraint here to catch it. This
    is why the onboarding integration's own idempotency for this
    relationship must come from (and does come from) the service layer's
    own lookup-before-create, not from the database."""
    store = MemoryClientContactStore()
    from app.models.client_crm import ClientContact

    now = _now()
    await store.create(
        ClientContact(client_contact_id="cc-1", client_id="client-1", crm_contact_id="contact-1", created_at=now, updated_at=now)
    )
    # A second row for the IDENTICAL (client_id, crm_contact_id) pair --
    # this does NOT raise, proving there is no uniqueness protection here.
    await store.create(
        ClientContact(client_contact_id="cc-2", client_id="client-1", crm_contact_id="contact-1", created_at=now, updated_at=now)
    )
    matches = [c for c in await store.list_for_client("client-1") if c.crm_contact_id == "contact-1"]
    assert len(matches) == 2  # the gap, demonstrated -- not hypothetical


async def test_client_contact_store_has_no_database_level_uniqueness_constraint_sqlite(tmp_path):
    """Same demonstration against the real SQLite-backed store -- confirms
    this is genuinely a missing DB constraint, not just a Memory-store
    testing artifact."""
    store = SQLiteClientContactStore(str(tmp_path / "client_contacts.db"))
    await store.connect()
    from app.models.client_crm import ClientContact

    now = _now()
    await store.create(
        ClientContact(client_contact_id="cc-1", client_id="client-1", crm_contact_id="contact-1", created_at=now, updated_at=now)
    )
    # No IntegrityError, no custom conflict exception -- the real SQLite
    # table has no unique index over (client_id, crm_contact_id).
    await store.create(
        ClientContact(client_contact_id="cc-2", client_id="client-1", crm_contact_id="contact-1", created_at=now, updated_at=now)
    )
    matches = [c for c in await store.list_for_client("client-1") if c.crm_contact_id == "contact-1"]
    assert len(matches) == 2
    await store.close()
