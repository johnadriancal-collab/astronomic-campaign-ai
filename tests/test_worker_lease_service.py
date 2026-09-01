"""WorkerLeaseService -- thin orchestration over WorkerLeaseStore."""

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.worker_lease_store import MemoryWorkerLeaseStore
from app.services.worker_lease_service import WorkerLeaseService, generate_holder_id

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_generate_holder_id_is_unique_across_calls():
    ids = {generate_holder_id() for _ in range(50)}
    assert len(ids) == 50


async def test_first_tick_acquires_leadership():
    service = WorkerLeaseService(MemoryWorkerLeaseStore(), holder_id="A")
    assert await service.try_acquire_or_renew(NOW, 90) is True


async def test_second_service_cannot_acquire_while_first_is_valid():
    store = MemoryWorkerLeaseStore()
    a = WorkerLeaseService(store, holder_id="A")
    b = WorkerLeaseService(store, holder_id="B")
    assert await a.try_acquire_or_renew(NOW, 90) is True
    assert await b.try_acquire_or_renew(NOW + timedelta(seconds=5), 90) is False


async def test_subsequent_ticks_renew_rather_than_reacquire():
    store = MemoryWorkerLeaseStore()
    service = WorkerLeaseService(store, holder_id="A")
    assert await service.try_acquire_or_renew(NOW, 90) is True
    assert await service.try_acquire_or_renew(NOW + timedelta(seconds=30), 90) is True
    lease = await store.get("mail_execution_worker")
    assert lease.expires_at == NOW + timedelta(seconds=30) + timedelta(seconds=90)


async def test_takeover_after_expiry_via_try_acquire_or_renew():
    store = MemoryWorkerLeaseStore()
    a = WorkerLeaseService(store, holder_id="A")
    b = WorkerLeaseService(store, holder_id="B")
    await a.try_acquire_or_renew(NOW, 90)
    later = NOW + timedelta(seconds=200)
    assert await b.try_acquire_or_renew(later, 90) is True
    lease = await store.get("mail_execution_worker")
    assert lease.holder_id == "B"


async def test_is_leader_true_for_current_valid_holder():
    store = MemoryWorkerLeaseStore()
    service = WorkerLeaseService(store, holder_id="A")
    await service.try_acquire_or_renew(NOW, 90)
    assert await service.is_leader(NOW + timedelta(seconds=10)) is True


async def test_is_leader_false_after_takeover_by_another_holder():
    """The exact scenario prepare_and_send_step()'s confirm_leadership
    callback exists to catch: A believes it might still be leader, but a
    fresh check proves it lost the lease."""
    store = MemoryWorkerLeaseStore()
    a = WorkerLeaseService(store, holder_id="A")
    b = WorkerLeaseService(store, holder_id="B")
    await a.try_acquire_or_renew(NOW, 90)
    later = NOW + timedelta(seconds=200)
    await b.try_acquire_or_renew(later, 90)
    assert await a.is_leader(later + timedelta(seconds=1)) is False


async def test_is_leader_false_when_never_acquired():
    service = WorkerLeaseService(MemoryWorkerLeaseStore(), holder_id="A")
    assert await service.is_leader(NOW) is False


async def test_is_leader_is_a_fresh_check_not_the_cached_flag():
    """Even if the service's own in-memory _currently_leader flag is
    stale (set True by a prior tick), is_leader() must re-derive the
    truth from the store, not trust the cache."""
    store = MemoryWorkerLeaseStore()
    a = WorkerLeaseService(store, holder_id="A")
    b = WorkerLeaseService(store, holder_id="B")
    await a.try_acquire_or_renew(NOW, 90)
    assert a._currently_leader is True  # cached from the acquire above
    later = NOW + timedelta(seconds=200)
    await b.try_acquire_or_renew(later, 90)  # takeover, A's cache is now stale
    assert await a.is_leader(later + timedelta(seconds=1)) is False
    assert a._currently_leader is False  # is_leader() corrected the cache too


async def test_release_by_current_leader_succeeds():
    store = MemoryWorkerLeaseStore()
    service = WorkerLeaseService(store, holder_id="A")
    await service.try_acquire_or_renew(NOW, 90)
    assert await service.release() is True
    assert await store.get("mail_execution_worker") is None


async def test_release_after_already_losing_leadership_is_a_safe_noop():
    store = MemoryWorkerLeaseStore()
    a = WorkerLeaseService(store, holder_id="A")
    b = WorkerLeaseService(store, holder_id="B")
    await a.try_acquire_or_renew(NOW, 90)
    later = NOW + timedelta(seconds=200)
    await b.try_acquire_or_renew(later, 90)
    assert await a.release() is False
    lease = await store.get("mail_execution_worker")
    assert lease.holder_id == "B"  # B's still-valid lease untouched by A's release
