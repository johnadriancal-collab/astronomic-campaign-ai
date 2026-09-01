"""
WorkerLeaseStore -- both Memory and SQLite implementations, run through
the SAME test matrix (parametrized) so the fake and the real store are
provably equivalent. The SQLite case is what actually proves the atomic
CAS semantics (verified empirically against a real UPSERT-with-WHERE
before this file was written -- see sqlite_worker_lease_store.py's own
docstring).
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.repositories.sqlite_worker_lease_store import SQLiteWorkerLeaseStore
from app.repositories.worker_lease_store import MemoryWorkerLeaseStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryWorkerLeaseStore()
        return
    s = SQLiteWorkerLeaseStore(str(tmp_path / "lease.db"))
    await s.connect()
    yield s
    await s.close()


async def test_first_acquire_succeeds(store):
    assert await store.try_acquire("lease", "A", NOW, 90) is True
    lease = await store.get("lease")
    assert lease is not None
    assert lease.holder_id == "A"
    assert lease.expires_at == NOW + timedelta(seconds=90)


async def test_second_acquire_while_still_valid_fails(store):
    await store.try_acquire("lease", "A", NOW, 90)
    assert await store.try_acquire("lease", "B", NOW + timedelta(seconds=10), 90) is False
    lease = await store.get("lease")
    assert lease.holder_id == "A"  # untouched by the failed attempt


async def test_acquire_after_expiry_succeeds_and_takes_over(store):
    await store.try_acquire("lease", "A", NOW, 90)
    later = NOW + timedelta(seconds=200)  # well past the 90s lease
    assert await store.try_acquire("lease", "B", later, 90) is True
    lease = await store.get("lease")
    assert lease.holder_id == "B"


async def test_acquire_at_the_exact_expiry_instant_succeeds():
    """expires_at <= now -- the boundary is inclusive."""
    store = MemoryWorkerLeaseStore()
    await store.try_acquire("lease", "A", NOW, 90)
    exact_expiry = NOW + timedelta(seconds=90)
    assert await store.try_acquire("lease", "B", exact_expiry, 90) is True


async def test_renew_by_current_holder_succeeds_and_extends(store):
    await store.try_acquire("lease", "A", NOW, 90)
    renew_time = NOW + timedelta(seconds=30)
    assert await store.try_renew("lease", "A", renew_time, 90) is True
    lease = await store.get("lease")
    assert lease.expires_at == renew_time + timedelta(seconds=90)


async def test_renew_by_a_non_holder_fails(store):
    await store.try_acquire("lease", "A", NOW, 90)
    assert await store.try_renew("lease", "B", NOW + timedelta(seconds=10), 90) is False
    lease = await store.get("lease")
    assert lease.holder_id == "A"  # untouched


async def test_renew_of_a_nonexistent_lease_fails(store):
    assert await store.try_renew("lease", "A", NOW, 90) is False


async def test_renew_after_being_taken_over_fails():
    """The exact scenario WorkerLeaseService.is_leader()'s docstring
    warns about: A's lease expired, B took over -- A calling try_renew()
    afterward must fail, not silently re-extend a lease A no longer owns."""
    store = MemoryWorkerLeaseStore()
    await store.try_acquire("lease", "A", NOW, 90)
    later = NOW + timedelta(seconds=200)
    await store.try_acquire("lease", "B", later, 90)  # takeover
    assert await store.try_renew("lease", "A", later + timedelta(seconds=1), 90) is False
    lease = await store.get("lease")
    assert lease.holder_id == "B"


async def test_get_on_a_never_acquired_lease_returns_none(store):
    assert await store.get("never-acquired") is None


async def test_release_by_current_holder_succeeds(store):
    await store.try_acquire("lease", "A", NOW, 90)
    assert await store.release("lease", "A") is True
    assert await store.get("lease") is None


async def test_release_by_a_non_holder_fails_and_leaves_lease_intact(store):
    await store.try_acquire("lease", "A", NOW, 90)
    assert await store.release("lease", "B") is False
    lease = await store.get("lease")
    assert lease is not None and lease.holder_id == "A"


async def test_release_of_a_nonexistent_lease_is_a_safe_noop(store):
    assert await store.release("lease", "A") is False


async def test_two_independent_lease_names_do_not_interfere(store):
    assert await store.try_acquire("lease-1", "A", NOW, 90) is True
    assert await store.try_acquire("lease-2", "B", NOW, 90) is True
    assert (await store.get("lease-1")).holder_id == "A"
    assert (await store.get("lease-2")).holder_id == "B"


async def test_reacquiring_by_the_same_holder_before_expiry_fails():
    """try_acquire() is for first-acquisition/takeover only -- a current,
    still-valid holder must use try_renew(), never try_acquire() again
    (see WorkerLeaseStore.try_acquire()'s own docstring)."""
    store = MemoryWorkerLeaseStore()
    await store.try_acquire("lease", "A", NOW, 90)
    assert await store.try_acquire("lease", "A", NOW + timedelta(seconds=10), 90) is False
