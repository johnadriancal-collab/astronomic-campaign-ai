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

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _service():
    client_store = MemoryClientStore()
    crm_contact_store = MemoryCrmContactStore()
    activity_log = ActivityLogService(store=MemoryActivityEventStore())
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=MemoryClientContactStore(),
        crm_contact_store=crm_contact_store,
        engagement_store=MemoryEngagementStore(),
        engagement_closeout_store=MemoryEngagementCloseoutStore(),
        engagement_participant_store=MemoryEngagementParticipantStore(),
        luma_event_store=MemoryLumaEventStore(),
        client_touchpoint_store=MemoryClientTouchpointStore(),
        contact_engagement_signal_service=signal_service,
    )
    return service


async def test_exact_match_found():
    service = await _service()
    created = await service.create_client({"name": "Hive ASMBLD"})
    found = await service.find_client_by_normalized_name("Hive ASMBLD")
    assert found.client_id == created.client_id


@pytest.mark.parametrize("variant", ["  Hive ASMBLD  ", "hive asmbld", "HIVE ASMBLD", "Hive   ASMBLD", "Hive\tASMBLD"])
async def test_whitespace_and_case_variants_still_match(variant):
    service = await _service()
    created = await service.create_client({"name": "Hive ASMBLD"})
    found = await service.find_client_by_normalized_name(variant)
    assert found.client_id == created.client_id


async def test_genuinely_different_names_never_match():
    service = await _service()
    await service.create_client({"name": "Hive ASMBLD"})
    assert await service.find_client_by_normalized_name("Hive ASMBLD Inc") is None
    assert await service.find_client_by_normalized_name("Hive") is None
    assert await service.find_client_by_normalized_name("ASMBLD") is None


async def test_no_match_returns_none_not_an_error():
    service = await _service()
    assert await service.find_client_by_normalized_name("Nonexistent Co") is None


async def test_archived_clients_are_never_matched():
    service = await _service()
    created = await service.create_client({"name": "Hive ASMBLD"})
    archived = created.model_copy(update={"archived": True})
    await service.client_store.save(archived)

    assert await service.find_client_by_normalized_name("Hive ASMBLD") is None


async def test_blank_or_whitespace_only_query_returns_none():
    service = await _service()
    await service.create_client({"name": "Hive ASMBLD"})
    assert await service.find_client_by_normalized_name("") is None
    assert await service.find_client_by_normalized_name("   ") is None
