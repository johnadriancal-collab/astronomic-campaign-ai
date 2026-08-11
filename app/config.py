"""
Application configuration, loaded from environment variables / .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str
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


settings = Settings()
