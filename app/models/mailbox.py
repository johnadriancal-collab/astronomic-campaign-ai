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
