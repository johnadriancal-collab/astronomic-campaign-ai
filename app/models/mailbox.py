"""
Astronomic Mail -- Phase 2 (Google Workspace Mailbox Connection) models.

Mailbox CONNECTION ONLY -- there is no sending capability anywhere in this
module or anywhere else in this phase. Connecting a Google Workspace inbox
does not enable Gmail send access: the OAuth scopes requested (see
app/google/oauth_client.py) are `openid email profile` only --
no `gmail.*` scope of any kind is ever requested. A future sending phase
would need `https://www.googleapis.com/auth/gmail.send` (a Google
"Sensitive" scope, requiring Google's OAuth verification/security
assessment before general availability) and would need every already-
connected mailbox to re-consent; nothing here does that.

Mailbox is the PUBLIC-safe model -- returned by every route in
app/api/mailboxes.py. MailboxCredential is a SEPARATE, INTERNAL-ONLY model
holding the encrypted refresh token; it is never imported by
app/api/mailboxes.py's response models and never serialized into any API
response. This split is deliberate defense-in-depth: even a future route
that accidentally returned the wrong object would fail Pydantic's
response_model validation rather than leak a token, since the two models
share no credential field at all.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MailboxProvider(str, Enum):
    GOOGLE = "google"


class MailboxStatus(str, Enum):
    """
    CONNECTED: token exchange + userinfo fetch succeeded and a refresh
      token is stored -- see MailboxService.handle_google_callback().
    NEEDS_REAUTH: reserved for when a future refresh-token-use (e.g. once
      sending exists, or a periodic health check is added) discovers Google
      has revoked/expired the grant. Nothing in this phase ever transitions
      a mailbox into this state, since nothing yet uses the stored refresh
      token again after the initial connection.
    DISCONNECTED: the user explicitly disconnected this mailbox (see
      MailboxService.disconnect_mailbox()) -- terminal in this phase,
      matching MailCampaignStatus.ARCHIVED's "no un-archive" precedent.
    """

    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    DISCONNECTED = "disconnected"


class Mailbox(BaseModel):
    mailbox_id: str
    provider: MailboxProvider
    email: str
    display_name: str | None
    status: MailboxStatus
    google_user_id: str | None  # OIDC `sub` -- the stable dedup key; see MailboxService
    # Exactly what Google's token response reported as granted (its `scope`
    # field, space-delimited) -- NOT what build_authorize_url() requested.
    # Not sensitive (these are scope identifiers like "openid", never a
    # credential), so this lives on the public model, unlike the refresh
    # token. Refreshed to the latest grant on every reconnect.
    granted_scopes: list[str] = Field(default_factory=list)
    connected_at: datetime
    updated_at: datetime
    disconnected_at: datetime | None = None


class MailboxCredential(BaseModel):
    """
    INTERNAL ONLY -- never imported by app/api/mailboxes.py, never a
    response_model, never logged. `encrypted_refresh_token` is Fernet
    ciphertext (see app/services/token_encryption.py); the plaintext
    refresh token exists only transiently in memory during
    MailboxService.handle_google_callback()/disconnect_mailbox().
    """

    mailbox_id: str
    encrypted_refresh_token: str
    created_at: datetime
    updated_at: datetime


class MailboxSendPolicy(BaseModel):
    """
    Phase A -- per-mailbox sending-SAFETY configuration, deliberately
    separate from MailCampaign.daily_lead_start_limit (see that field's own
    docstring for exactly why the two must never be collapsed: this one
    protects a single Gmail account's sending reputation across every
    campaign that uses it; that one paces one campaign's new starts).

    1:1 with Mailbox by `mailbox_id`, but a row's ABSENCE is a fully valid,
    expected state -- NOT an error and NOT something requiring a
    backfill/migration write for any already-connected mailbox. Both
    `daily_send_limit` and `min_seconds_between_sends` are nullable, and
    null means "defer to the current system-wide default constant" (see
    MailSendingService.DEFAULT_MAILBOX_DAILY_SEND_LIMIT/
    DEFAULT_MAILBOX_MIN_SECONDS_BETWEEN_SENDS) -- the exact same fallback
    as when no row exists at all. This is deliberate: it separates "the
    capability to configure a limit" from "what the limit's number
    currently is," so a system-wide default can be tuned in one place
    without ever needing to touch every mailbox's row, and so existing
    mailbox connections (e.g. an already-connected mailbox from before this
    phase) need zero mutation to be correctly, safely governed by whatever
    the current default is.

    `daily_send_limit`: the maximum total messages (Step 1 AND every
    follow-up, across every campaign using this mailbox) this mailbox may
    send in one UTC calendar day -- see MailSendingService's runtime safety
    checks for the exact enforcement query and why the day boundary is
    UTC-fixed rather than tied to any one campaign's timezone (a mailbox
    can serve multiple campaigns with different timezones at once, so no
    single campaign's local midnight is the correct boundary for a
    mailbox-level safety limit).

    `min_seconds_between_sends`: the minimum pacing floor between two
    consecutive sends FROM THIS MAILBOX, across every campaign -- exists so
    a burst of simultaneously-due messages (e.g. 100 messages all due at a
    schedule window's opening moment) cannot become 100 near-simultaneous
    provider calls; see MailSendingService's runtime safety checks for the
    exact enforcement (comparing against this mailbox's own most recent
    SENT timestamp, across every enrollment/campaign, not just the one
    currently being processed).

    New mailbox connections MAY create a row (system-default-valued) at
    connect time if that turns out to keep the wiring simpler, but
    execution correctness must never depend on a row existing -- see
    MailSendingService.resolve_mailbox_send_policy()'s "missing row ==
    null-override row" equivalence, and its own test coverage.
    """

    mailbox_id: str
    daily_send_limit: int | None = None
    min_seconds_between_sends: int | None = None
    created_at: datetime
    updated_at: datetime
