"""
compute_authorization_health() -- proactive OAuth expiration warnings
(2026-09-17). Pure function, no stores/fixtures needed -- just Mailbox
rows built directly and a fixed `now`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.mailbox import Mailbox, MailboxAuthorizationHealthState, MailboxProvider, MailboxStatus
from app.services import mailbox_authorization_health as health_module
from app.services.mailbox_authorization_health import compute_authorization_health

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_mailbox(**overrides) -> Mailbox:
    fields = dict(
        mailbox_id="mb-1",
        provider=MailboxProvider.GOOGLE,
        email="victoria@useastronomic.com",
        display_name="Victoria Bennett",
        status=MailboxStatus.CONNECTED,
        google_user_id="google-sub-1",
        granted_scopes=["openid", "email", "profile"],
        connected_at=NOW - timedelta(days=30),
        updated_at=NOW,
        gmail_authorized_at=None,
    )
    fields.update(overrides)
    return Mailbox(**fields)


@pytest.fixture(autouse=True)
def testing_mode_enabled(monkeypatch):
    monkeypatch.setattr(health_module, "OAUTH_TESTING_MODE_EXPIRY_ENABLED", True)


def test_newly_authorized_mailbox_is_connected():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(minutes=5))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.CONNECTED
    assert health.authorized_at_is_estimated is False


def test_five_day_old_authorization_is_still_connected():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=5))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.CONNECTED


def test_six_day_old_authorization_is_reconnect_soon():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=6))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.RECONNECT_SOON


def test_six_days_twenty_three_hours_is_still_reconnect_soon_not_needs_reauth():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=6, hours=23))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.RECONNECT_SOON


def test_seven_days_or_more_is_needs_reauth():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=7))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.NEEDS_REAUTH


def test_eight_days_is_needs_reauth():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=8))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.NEEDS_REAUTH


def test_invalid_grant_status_overrides_a_fresh_looking_age():
    """A mailbox reconnected 5 minutes ago (age-wise, clearly CONNECTED)
    but whose MailboxStatus is already NEEDS_REAUTH (e.g. the user
    revoked access from their own Google Account settings moments after
    reconnecting) must report NEEDS_REAUTH -- the real provider signal
    always wins over the calculated age."""
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(minutes=5), status=MailboxStatus.NEEDS_REAUTH)
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.NEEDS_REAUTH


def test_disconnected_mailbox_has_no_computed_state():
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=1), status=MailboxStatus.DISCONNECTED)
    health = compute_authorization_health(mailbox, NOW)
    assert health.state is None


def test_missing_gmail_authorized_at_falls_back_to_updated_at_and_is_flagged_estimated():
    mailbox = make_mailbox(gmail_authorized_at=None, updated_at=NOW - timedelta(days=6, hours=1))
    health = compute_authorization_health(mailbox, NOW)
    assert health.authorized_at == mailbox.updated_at
    assert health.authorized_at_is_estimated is True
    assert health.state == MailboxAuthorizationHealthState.RECONNECT_SOON


def test_estimated_expires_at_is_exactly_seven_days_after_effective_authorized_at():
    authorized = NOW - timedelta(days=2)
    mailbox = make_mailbox(gmail_authorized_at=authorized)
    health = compute_authorization_health(mailbox, NOW)
    assert health.estimated_expires_at == authorized + timedelta(days=7)


def test_testing_mode_disabled_never_produces_an_age_based_warning(monkeypatch):
    """Once OAUTH_TESTING_MODE_EXPIRY_ENABLED is flipped off (the intended
    one-line change for moving to Production), a very old authorization
    must no longer be reported as RECONNECT_SOON/NEEDS_REAUTH by age
    alone."""
    monkeypatch.setattr(health_module, "OAUTH_TESTING_MODE_EXPIRY_ENABLED", False)
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=60))
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.CONNECTED
    assert health.estimated_expires_at is None


def test_testing_mode_disabled_still_honors_a_real_invalid_grant(monkeypatch):
    monkeypatch.setattr(health_module, "OAUTH_TESTING_MODE_EXPIRY_ENABLED", False)
    mailbox = make_mailbox(gmail_authorized_at=NOW - timedelta(days=1), status=MailboxStatus.NEEDS_REAUTH)
    health = compute_authorization_health(mailbox, NOW)
    assert health.state == MailboxAuthorizationHealthState.NEEDS_REAUTH
