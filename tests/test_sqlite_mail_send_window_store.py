"""
Direct SQLite-layer tests for MailSendWindowStore -- same tmp_path-backed
SQLite file convention as test_sqlite_mail_campaign_mailbox_store.py.
Proves the send-window table round-trips real MailSendWindow objects
(multiple windows per day, multiple days), and that replace_for_campaign()
is atomic -- a failure partway through leaves the previous window set
completely intact.
"""

from datetime import datetime, time, timezone

import aiosqlite
import pytest
import pytest_asyncio

from app.models.mail import MailSendWindow
from app.repositories.sqlite_mail_send_window_store import SQLiteMailSendWindowStore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteMailSendWindowStore(str(tmp_path / "windows.db"))
    await s.connect()
    yield s
    await s.close()


def _window(campaign_id="c1", day=0, start="08:00", end="12:00", window_id=None) -> MailSendWindow:
    now = datetime.now(timezone.utc)
    return MailSendWindow(
        window_id=window_id or f"win-{campaign_id}-{day}-{start}",
        mail_campaign_id=campaign_id,
        day_of_week=day,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        created_at=now,
        updated_at=now,
    )


async def test_list_starts_empty(store):
    assert await store.list_for_campaign("c1") == []


async def test_replace_sets_the_windows(store):
    windows = [_window(day=0, start="08:00", end="12:00"), _window(day=1, start="09:00", end="17:00")]
    await store.replace_for_campaign("c1", windows)
    stored = await store.list_for_campaign("c1")
    assert len(stored) == 2
    assert {w.day_of_week for w in stored} == {0, 1}


async def test_multiple_windows_same_day_round_trip(store):
    windows = [
        _window(day=0, start="08:00", end="12:00", window_id="w1"),
        _window(day=0, start="14:00", end="18:00", window_id="w2"),
    ]
    await store.replace_for_campaign("c1", windows)
    stored = await store.list_for_campaign("c1")
    assert len(stored) == 2
    assert all(w.day_of_week == 0 for w in stored)
    assert [w.start_time.isoformat() for w in stored] == ["08:00:00", "14:00:00"]


async def test_replace_is_a_full_replace_not_an_incremental_add(store):
    await store.replace_for_campaign("c1", [_window(day=0)])
    await store.replace_for_campaign("c1", [_window(day=2, start="09:00", end="10:00")])
    stored = await store.list_for_campaign("c1")
    assert len(stored) == 1
    assert stored[0].day_of_week == 2


async def test_replace_never_touches_other_campaigns(store):
    await store.replace_for_campaign("c1", [_window(campaign_id="c1")])
    await store.replace_for_campaign("c2", [_window(campaign_id="c2")])
    await store.replace_for_campaign("c1", [])
    assert await store.list_for_campaign("c1") == []
    assert len(await store.list_for_campaign("c2")) == 1


async def test_replace_is_atomic_on_failure(store, monkeypatch):
    """A forced failure partway through replace_for_campaign() (after the
    DELETE but before the INSERT completes) must leave the PREVIOUS window
    set completely intact, not a half-replaced one."""
    original = [_window(day=0), _window(day=1, start="09:00", end="17:00")]
    await store.replace_for_campaign("c1", original)

    original_executemany = store._connection.executemany

    async def failing_executemany(*args, **kwargs):
        raise aiosqlite.OperationalError("simulated failure")

    monkeypatch.setattr(store._connection, "executemany", failing_executemany)

    with pytest.raises(aiosqlite.OperationalError):
        await store.replace_for_campaign("c1", [_window(day=3, start="10:00", end="11:00")])

    monkeypatch.setattr(store._connection, "executemany", original_executemany)
    stored = await store.list_for_campaign("c1")
    assert {w.day_of_week for w in stored} == {0, 1}


async def test_survives_reconnect(tmp_path):
    db_path = str(tmp_path / "windows.db")
    store1 = SQLiteMailSendWindowStore(db_path)
    await store1.connect()
    await store1.replace_for_campaign("c1", [_window(day=4, start="07:30", end="11:45")])
    await store1.close()

    store2 = SQLiteMailSendWindowStore(db_path)
    await store2.connect()
    stored = await store2.list_for_campaign("c1")
    assert len(stored) == 1
    assert stored[0].day_of_week == 4
    assert stored[0].start_time.isoformat() == "07:30:00"
    await store2.close()
