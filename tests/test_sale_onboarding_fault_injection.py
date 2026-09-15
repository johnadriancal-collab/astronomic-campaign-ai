"""
Pre-production hardening (2026-09-15): fault-injection test for
SaleOnboardingService.onboard()'s partial-failure behavior.

SaleOnboardingService does NOT wrap its steps in a database transaction
-- Client, Contact, ClientContact, and Engagement each live in their own
SQLite table/store, written via separate calls (ClientCrmService.
create_client(), CrmService.create_contact(),
ClientCrmService.create_client_contact(), ClientCrmService.
create_client_engagement()), and there is no cross-store transaction
mechanism in this codebase to wrap them in one atomic unit.

Convergence is achieved through IDEMPOTENT OPERATIONS instead: every step
before Engagement creation is itself a lookup-before-create (Client by
normalized name, Contact by normalized email, ClientContact by
(client_id, crm_contact_id) pair -- see SaleOnboardingService._resolve_*
methods), so a retry after a failure anywhere in the sequence re-finds
whatever was already created on the failed attempt rather than
duplicating it, and only the step that actually failed (or never ran)
does real work on the retry.

This test injects a failure specifically at the LAST step (Engagement
creation, after Client/Contact/ClientContact have already been created
for real) -- the scenario the user asked about -- and proves a retry with
the identical payload converges to exactly one of each record, never a
duplicate Client/Contact/ClientContact, and never leaves the sale
unrecoverable.
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
    engagements = []
    for c in clients:
        client_contacts.extend(await client_crm_service.client_contact_store.list_for_client(c.client_id))
        engagements.extend(await client_crm_service.engagement_store.list_for_client(c.client_id))
    return len(clients), len(contacts), len(client_contacts), len(engagements)


async def test_engagement_creation_failure_after_client_contact_clientcontact_succeed_leaves_a_safely_resumable_state():
    service, client_crm_service = await _services()
    payload = _payload()

    real_create_engagement = client_crm_service.create_client_engagement
    call_count = {"n": 0}

    async def flaky_create_engagement(client_id, fields):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated Engagement creation failure (e.g. a transient DB write error)")
        return await real_create_engagement(client_id, fields)

    client_crm_service.create_client_engagement = flaky_create_engagement

    # First attempt: Client/Contact/ClientContact all succeed for real;
    # Engagement creation is the injected failure point.
    with pytest.raises(RuntimeError, match="simulated Engagement creation failure"):
        await service.onboard(payload)

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 1, 1, 0)

    client = (await client_crm_service.client_store.list())[0]
    contact = (await client_crm_service.crm_contact_store.list())[0]
    client_contacts_for_client = await client_crm_service.client_contact_store.list_for_client(client.client_id)
    assert len(client_contacts_for_client) == 1
    assert client.name == "Hive ASMBLD"
    assert contact.email == "jane@hiveasmbld.com"

    # Retry with the IDENTICAL payload (simulating the sale bot's next
    # 5-minute poll) -- this time Engagement creation succeeds.
    result = await service.onboard(payload)

    assert result.already_processed is False
    assert result.client_id == client.client_id
    assert result.crm_contact_id == contact.crm_contact_id

    # Converged to exactly one of each -- the failed attempt's partial
    # state was reused, never duplicated.
    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 1, 1, 1)


async def test_a_second_retry_after_convergence_is_a_pure_no_op():
    service, client_crm_service = await _services()
    payload = _payload()

    real_create_engagement = client_crm_service.create_client_engagement
    call_count = {"n": 0}

    async def fail_once(client_id, fields):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return await real_create_engagement(client_id, fields)

    client_crm_service.create_client_engagement = fail_once

    with pytest.raises(RuntimeError):
        await service.onboard(payload)
    first_success = await service.onboard(payload)
    second_success = await service.onboard(payload)

    assert first_success.already_processed is False
    assert second_success.already_processed is True
    assert second_success.engagement_id == first_success.engagement_id

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 1, 1, 1)


async def test_failure_injected_at_the_contact_creation_step_also_converges_cleanly():
    """A different fault point -- Contact creation fails after the Client
    already exists -- to confirm convergence isn't specific to the
    Engagement-step example."""
    service, client_crm_service = await _services()
    payload = _payload()

    real_create_contact = service.crm_service.create_contact
    call_count = {"n": 0}

    async def flaky_create_contact(fields):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated Contact creation failure")
        return await real_create_contact(fields)

    service.crm_service.create_contact = flaky_create_contact

    with pytest.raises(RuntimeError, match="simulated Contact creation failure"):
        await service.onboard(payload)

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 0, 0, 0)

    result = await service.onboard(payload)
    assert result.already_processed is False

    n_clients, n_contacts, n_client_contacts, n_engagements = await _counts(client_crm_service)
    assert (n_clients, n_contacts, n_client_contacts, n_engagements) == (1, 1, 1, 1)
