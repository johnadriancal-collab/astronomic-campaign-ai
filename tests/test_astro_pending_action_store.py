"""
AstroPendingActionStore tests -- Astro AI Phase 3's hard confirmation gate.
Mirrors test_astro_export_store.py's own testing shape (a small, focused
in-memory-store test file) for the sibling ephemeral-state store.
"""

from datetime import timedelta

import pytest

from app.services.astro_pending_action_store import (
    PENDING_ACTION_TTL,
    AstroPendingActionStore,
    PendingActionStatus,
)

pytestmark = pytest.mark.asyncio


async def _noop_execute() -> dict:
    return {"status": "done"}


async def test_put_then_get_returns_the_pending_action():
    store = AstroPendingActionStore()
    pending_action_id = store.put(
        tool_name="remove_crm_contact_from_list",
        description="Remove Jane Doe from Austin Investors.",
        before="member",
        after="not a member",
        execute=_noop_execute,
    )
    pending = store.get(pending_action_id)
    assert pending is not None
    assert pending.tool_name == "remove_crm_contact_from_list"
    assert pending.status == PendingActionStatus.PENDING
    assert pending.before == "member"
    assert pending.after == "not a member"


async def test_unknown_id_returns_none():
    store = AstroPendingActionStore()
    assert store.get("does-not-exist") is None


async def test_get_after_ttl_expires_returns_none():
    store = AstroPendingActionStore()
    pending_action_id = store.put(
        tool_name="remove_crm_contact_from_list", description="x", before=None, after=None, execute=_noop_execute
    )
    # Simulate elapsed time by rewinding created_at rather than sleeping in
    # a test -- same technique this codebase already uses for other TTL
    # stores' own tests.
    store._pending[pending_action_id].created_at -= PENDING_ACTION_TTL + timedelta(seconds=1)
    assert store.get(pending_action_id) is None


async def test_mark_executed_then_get_still_returns_it_as_executed():
    """An EXECUTED action is deliberately NOT pruned/deleted -- a repeated
    confirm_astro_action call must still find it and report
    'already_executed', never a confusing 'not found'."""
    store = AstroPendingActionStore()
    pending_action_id = store.put(
        tool_name="remove_crm_contact_from_list", description="x", before=None, after=None, execute=_noop_execute
    )
    store.mark_executed(pending_action_id)
    pending = store.get(pending_action_id)
    assert pending is not None
    assert pending.status == PendingActionStatus.EXECUTED
    assert pending.executed_at is not None


async def test_executed_action_survives_ttl_expiry_of_its_created_at():
    """Even if an EXECUTED action's created_at is old, it must still be
    retrievable -- only PENDING (never-confirmed) actions are pruned by
    TTL."""
    store = AstroPendingActionStore()
    pending_action_id = store.put(
        tool_name="remove_crm_contact_from_list", description="x", before=None, after=None, execute=_noop_execute
    )
    store.mark_executed(pending_action_id)
    store._pending[pending_action_id].created_at -= PENDING_ACTION_TTL + timedelta(seconds=1)
    pending = store.get(pending_action_id)
    assert pending is not None
    assert pending.status == PendingActionStatus.EXECUTED


async def test_execute_closure_is_called_and_returns_its_result():
    calls = []

    async def _execute() -> dict:
        calls.append(1)
        return {"status": "removed"}

    store = AstroPendingActionStore()
    pending_action_id = store.put(
        tool_name="remove_crm_contact_from_list", description="x", before=None, after=None, execute=_execute
    )
    pending = store.get(pending_action_id)
    result = await pending.execute()
    assert result == {"status": "removed"}
    assert calls == [1]
