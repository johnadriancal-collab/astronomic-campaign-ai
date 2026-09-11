"""
Client CRM Stage 5A (2026-09-11) -- pure-function unit tests for
app/services/luma_decline_origin.py, the ONE place that derives
decline_origin from a Luma approval_status transition (or a first-seen
already-declined registration). No stores, no services -- these are exact
input->output tests for the locked derivation rules themselves.
"""

from app.models.client_crm import DeclineOrigin
from app.models.luma import LumaApprovalStatus
from app.services.luma_decline_origin import derive_from_first_seen_declined, derive_from_observed_transition


def test_pending_approval_to_declined_is_host():
    assert derive_from_observed_transition(LumaApprovalStatus.PENDING_APPROVAL) == DeclineOrigin.HOST


def test_invited_to_declined_is_guest():
    assert derive_from_observed_transition(LumaApprovalStatus.INVITED) == DeclineOrigin.GUEST


def test_approved_to_declined_is_unknown():
    assert derive_from_observed_transition(LumaApprovalStatus.APPROVED) == DeclineOrigin.UNKNOWN


def test_waitlist_to_declined_is_unknown():
    assert derive_from_observed_transition(LumaApprovalStatus.WAITLIST) == DeclineOrigin.UNKNOWN


def test_session_to_declined_is_unknown():
    assert derive_from_observed_transition(LumaApprovalStatus.SESSION) == DeclineOrigin.UNKNOWN


def test_first_seen_declined_invited_only_is_guest():
    from datetime import datetime, timezone

    invited_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    assert derive_from_first_seen_declined(invited_at=invited_at, registered_at=None) == DeclineOrigin.GUEST


def test_first_seen_declined_with_registered_at_present_is_unknown():
    """LOCKED: never infer HOST merely from registered_at being present --
    a person who actively registered before we ever observed them could
    have been declined by either party."""
    from datetime import datetime, timezone

    registered_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert derive_from_first_seen_declined(invited_at=None, registered_at=registered_at) == DeclineOrigin.UNKNOWN
    # Even with invited_at ALSO set -- registered_at present still forces UNKNOWN.
    invited_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    assert derive_from_first_seen_declined(invited_at=invited_at, registered_at=registered_at) == DeclineOrigin.UNKNOWN


def test_first_seen_declined_with_neither_timestamp_is_unknown():
    assert derive_from_first_seen_declined(invited_at=None, registered_at=None) == DeclineOrigin.UNKNOWN
