"""
MailOpenEventStore -- open tracking (2026-09-18). Same tests run against
both the Memory and SQLite implementations via a fixture parametrized by
store class, matching this codebase's usual per-store-pair test
convention.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.repositories.mail_open_event_store import MemoryMailOpenEventStore
from app.repositories.sqlite_mail_open_event_store import SQLiteMailOpenEventStore

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryMailOpenEventStore()
    else:
        s = SQLiteMailOpenEventStore(str(tmp_path / "test.db"))
        await s.connect()
        yield s
        await s.close()


async def test_first_open_creates_the_row_and_returns_true(store):
    created = await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)

    assert created is True
    event = await store.get("s1")
    assert event.first_opened_at == NOW
    assert event.last_opened_at == NOW
    assert event.open_count == 1


async def test_repeat_open_for_the_same_step_bumps_count_but_returns_false(store):
    await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)
    later = NOW + timedelta(hours=1)

    created_again = await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=later)

    assert created_again is False
    event = await store.get("s1")
    assert event.open_count == 2
    assert event.last_opened_at == later


async def test_first_opened_at_never_changes_on_a_later_open(store):
    await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)
    await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW + timedelta(days=1))

    event = await store.get("s1")
    assert event.first_opened_at == NOW


async def test_get_returns_none_for_a_step_that_never_opened(store):
    assert await store.get("does-not-exist") is None


async def test_list_for_campaign_returns_only_rows_for_that_campaign(store):
    await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)
    await store.record_open(enrollment_step_id="s2", mail_campaign_id="c1", enrollment_id="e2", at=NOW)
    await store.record_open(enrollment_step_id="s3", mail_campaign_id="c2", enrollment_id="e3", at=NOW)

    rows = await store.list_for_campaign("c1")
    assert {r.enrollment_step_id for r in rows} == {"s1", "s2"}


async def test_multiple_opens_across_different_steps_never_collide(store):
    """Two different steps -- even for the same enrollment/campaign --
    are two independent rows, never merged."""
    await store.record_open(enrollment_step_id="s1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)
    await store.record_open(enrollment_step_id="s2", mail_campaign_id="c1", enrollment_id="e1", at=NOW)

    rows = await store.list_for_campaign("c1")
    assert len(rows) == 2
    assert {r.enrollment_step_id for r in rows} == {"s1", "s2"}
