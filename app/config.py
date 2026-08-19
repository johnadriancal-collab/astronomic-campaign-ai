"""
Application configuration, loaded from environment variables / .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional (Phase 0, 2026-08-12): the Campaign Builder ("Astro")'s Claude
    # plan-generation/ranking calls are the ONLY thing that needs this -- see
    # app/claude/client.py's ClaudeNotConfiguredError. Leaving it unset must
    # never prevent the rest of the app (CRM, ITF intake, /health, etc.) from
    # booting; that's exactly what making this Optional (rather than a bare
    # `str`, which pydantic-settings would fail app startup on) fixes.
    anthropic_api_key: str | None = None
    apollo_api_key: str

    claude_model: str = "claude-sonnet-4-5"
    apollo_base_url: str = "https://api.apollo.io/api/v1"

    # Optional: default mailbox to send from if not specified per-request.
    # Verify this ID exists under your Apollo account's connected mailboxes.
    default_sender_mailbox_id: str | None = None

    # SQLite file path for persistent Campaign storage (see
    # app/repositories/sqlite_campaign_store.py). Relative paths resolve
    # against the process's working directory. In production, this should
    # point at a mounted persistent volume -- the container filesystem
    # otherwise resets on every redeploy.
    database_path: str = "data/campaigns.db"

    # ITF (Investor Thesis Form) contact intake -- see
    # app/services/itf_ingestion_service.py. A Google Apps Script bound to the
    # ITF response Sheet POSTs each new submission to POST /sync/itf-contact;
    # no Google credentials of any kind live in this backend. This shared
    # secret is the only thing authenticating that call -- generate a long
    # random value (e.g. `openssl rand -hex 32`), set it here AND in the
    # script's PropertiesService, and never commit it. None until Railway is
    # configured with it; the webhook returns a clear 503 rather than crashing
    # at startup when it's unset.
    itf_webhook_token: str | None = None

    # Email -> CRM Intake (Phase 1, 2026-08-12) -- see
    # app/services/email_intake_service.py. Same shared-secret pattern as
    # itf_webhook_token above: a future Apps Script bridge (Phase 2, NOT
    # activated yet) would POST each new data@astronomic.com message to
    # POST /sync/email-intake, authenticated by this token. No Gmail
    # credentials of any kind live in this backend. None until that Phase 2
    # activation is deliberately configured -- the webhook returns a clear
    # 503 rather than crashing at startup when it's unset, same as ITF's.
    email_intake_webhook_token: str | None = None

    # Astronomic Mail Phase 2 (Google Workspace Mailbox Connection) -- see
    # app/integrations/google_oauth_client.py and app/services/mailbox_service.py.
    # ALL five are None until deliberately configured; every route that needs
    # them returns a clear 503 rather than crashing app startup, matching
    # itf_webhook_token's precedent above. No Gmail send/data scope is ever
    # requested by this client -- connection only.
    #
    # google_oauth_client_id / google_oauth_client_secret: from a Google Cloud
    # Console OAuth 2.0 Client ID (Web application type).
    #
    # google_oauth_redirect_uri: must exactly match one of that Client ID's
    # registered "Authorized redirect URIs" -- e.g.
    # "https://<this-backend's-public-domain>/mailboxes/google/callback".
    # Google redirects the user's browser here DIRECTLY (never through the
    # frontend's /backend/* rewrite proxy), since an OAuth redirect is a
    # top-level browser navigation Google itself issues, not a same-origin
    # fetch/XHR the proxy could intercept.
    #
    # frontend_origin: this backend's only way to know where to send the
    # user's browser back to after processing the callback (e.g.
    # "https://<frontend's-public-domain>") -- there is no other frontend-
    # origin config anywhere in this app today because the browser otherwise
    # never talks to this backend directly.
    #
    # mailbox_token_encryption_key: a Fernet key (32 url-safe base64 bytes)
    # used ONLY to encrypt/decrypt stored Google refresh tokens at rest (see
    # app/services/token_encryption.py) -- losing it means every connected
    # mailbox must be reconnected; there is no recovery path, by design.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    frontend_origin: str | None = None
    mailbox_token_encryption_key: str | None = None


settings = Settings()
