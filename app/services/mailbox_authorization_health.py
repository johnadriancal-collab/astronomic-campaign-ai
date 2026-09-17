"""
Proactive Gmail OAuth expiration warnings (2026-09-17).

Our Google OAuth app is currently External + Testing, where Google expires
a test user's authorization -- including the offline refresh token itself --
roughly 7 days after consent. Nothing in GoogleOAuthClient ever receives or
stores an actual expiry timestamp for a refresh token (Google doesn't
expose one), so this is necessarily a HEURISTIC built from our own last-
authorized timestamp, not a hard expiry read from Google. A real, confirmed
failure (MailboxStatus.NEEDS_REAUTH, set exclusively by MailboxService.
refresh_mailbox_access_token() on Google's own invalid_grant) always
overrides this heuristic -- see compute_authorization_health() below.

Computed fresh on every call, from Mailbox fields alone -- never persisted,
never cached, no periodic worker/cron needed to keep it current (V1
deliberately has none; see the approved plan).

TESTING-MODE ASSUMPTION: the entire 6/7-day age check is gated behind
OAUTH_TESTING_MODE_EXPIRY_ENABLED, a single named constant, specifically so
that once this app's Google Cloud Console OAuth consent screen moves from
External+Testing to Production (or Internal), the age-based warning can be
switched off with a one-line change here -- not a mailbox-model or
MailboxStatus redesign. Flip it to False (or delete the age branch
entirely) at that point; MailboxStatus.NEEDS_REAUTH's real, confirmed-
failure path is completely independent of this flag and needs no change.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.mailbox import Mailbox, MailboxAuthorizationHealthState, MailboxStatus

OAUTH_TESTING_MODE_EXPIRY_ENABLED = True

RECONNECT_SOON_AFTER = timedelta(days=6)
NEEDS_REAUTH_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class MailboxAuthorizationHealth:
    state: MailboxAuthorizationHealthState | None
    authorized_at: datetime | None
    authorized_at_is_estimated: bool
    age_seconds: float | None
    estimated_expires_at: datetime | None


def compute_authorization_health(mailbox: Mailbox, now: datetime) -> MailboxAuthorizationHealth:
    """A real provider failure ALWAYS overrides the calculated age-based
    state -- this never downgrades a mailbox Google has already confirmed
    broken (MailboxStatus.NEEDS_REAUTH) back down to CONNECTED or
    RECONNECT_SOON just because its age happens to look fine (e.g. it was
    reconnected recently and then immediately hit invalid_grant for an
    unrelated reason, such as the user revoking access from their Google
    Account settings).

    A DISCONNECTED mailbox has no meaningful authorization age to show --
    its own MailboxStatus badge already communicates its state -- so this
    returns state=None for it, not a fabricated CONNECTED/RECONNECT_SOON
    guess.
    """
    if mailbox.status == MailboxStatus.NEEDS_REAUTH:
        return MailboxAuthorizationHealth(
            state=MailboxAuthorizationHealthState.NEEDS_REAUTH,
            authorized_at=mailbox.gmail_authorized_at,
            authorized_at_is_estimated=mailbox.gmail_authorized_at is None,
            age_seconds=None,
            estimated_expires_at=None,
        )

    if mailbox.status == MailboxStatus.DISCONNECTED:
        return MailboxAuthorizationHealth(
            state=None,
            authorized_at=None,
            authorized_at_is_estimated=False,
            age_seconds=None,
            estimated_expires_at=None,
        )

    effective_authorized_at = mailbox.gmail_authorized_at or mailbox.updated_at
    is_estimated = mailbox.gmail_authorized_at is None
    age = now - effective_authorized_at

    if not OAUTH_TESTING_MODE_EXPIRY_ENABLED:
        return MailboxAuthorizationHealth(
            state=MailboxAuthorizationHealthState.CONNECTED,
            authorized_at=effective_authorized_at,
            authorized_at_is_estimated=is_estimated,
            age_seconds=age.total_seconds(),
            estimated_expires_at=None,
        )

    if age >= NEEDS_REAUTH_AFTER:
        state = MailboxAuthorizationHealthState.NEEDS_REAUTH
    elif age >= RECONNECT_SOON_AFTER:
        state = MailboxAuthorizationHealthState.RECONNECT_SOON
    else:
        state = MailboxAuthorizationHealthState.CONNECTED

    return MailboxAuthorizationHealth(
        state=state,
        authorized_at=effective_authorized_at,
        authorized_at_is_estimated=is_estimated,
        age_seconds=age.total_seconds(),
        estimated_expires_at=effective_authorized_at + NEEDS_REAUTH_AFTER,
    )
