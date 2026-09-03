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
    # `str`, which pydantic-settings would fail app startup on) fixes. Astro
    # AI chat (app/services/astro_ai_service.py) reuses this SAME key --
    # there is deliberately only one Anthropic credential in this app.
    anthropic_api_key: str | None = None
    apollo_api_key: str

    # Campaign Builder's plan-generation/ranking model -- unrelated to Astro
    # AI chat below; kept as its own setting so changing one never touches
    # the other's behavior.
    claude_model: str = "claude-sonnet-4-5"

    # Astro AI chat's model (see app/services/astro_ai_service.py) --
    # deliberately a SEPARATE setting from claude_model above, even though
    # both currently point at Claude, because they serve different jobs
    # (structured campaign-plan JSON vs. conversational business assistant)
    # that may reasonably need different models later. claude-sonnet-5 is
    # the current mid-tier model: strong enough for research/writing/
    # analysis without paying for Opus-tier capability this use case
    # doesn't need.
    astro_chat_model: str = "claude-sonnet-5"

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

    # Admin/service read-only tooling (Phase 1 -- READ ONLY, 2026-09-03) -- see
    # app/session_auth_middleware.py's own docstring for the full flow. Lets a
    # trusted development/operations tool (not a browser, no Hub session
    # cookie possible or wanted) make authenticated GET/HEAD/OPTIONS calls
    # against /crm/* for production investigation, via `Authorization: Bearer
    # <token>`. Deliberately separate from every webhook token above -- this
    # is pulled by an operator tool, never pushed by an external service.
    # Same "None until deliberately configured, clear 503 rather than
    # crashing" precedent as itf_webhook_token. There is NO corresponding
    # write token yet -- see that middleware module for why, and for the
    # explicit contact/list-only allowlist a future write token would need.
    admin_service_read_token: str | None = None

    # Admin/service OPERATOR token (Phase 2, 2026-09-03) -- see
    # app/session_auth_middleware.py's own docstring for the full flow and
    # the exact explicit route allowlist. A SEPARATE credential from
    # admin_service_read_token above -- distinct secret, distinct scope,
    # distinct request.state.identity ("service_operator" vs
    # "service_read"), never derived from or compared against the read
    # token. Lets a trusted automation identity (not a browser, no Hub
    # session cookie, never the operator's own personal password) perform
    # a narrow, explicitly allow-listed set of Astronomic Mail
    # campaign-preparation writes (create/edit a DRAFT campaign, manage its
    # steps/schedule/channel selection, manage CRM list membership for its
    # audience, Mark Ready, Unlock) plus the handful of reads needed to
    # verify that state -- never campaign activation, pause/resume,
    # archive, mail sending/execution administration, suppression
    # mutations, mailbox OAuth connect/disconnect, CRM contact writes,
    # custom-field/Luma-mapping writes, or the backup/import raw-data
    # surfaces. Same "None until deliberately configured, clear 503 rather
    # than crashing" precedent as every other token above.
    admin_service_operator_token: str | None = None

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

    # Astronomic Mail Phase B3 (Unsubscribe Architecture) -- see
    # app/services/unsubscribe_token.py and
    # app/services/mail_unsubscribe_composition.py. Both None until
    # deliberately configured; URL/token-generation call sites raise a
    # clear NotConfigured error rather than silently emitting a broken or
    # localhost-pointing link -- same fail-closed convention as every
    # other integration secret above.
    #
    # public_backend_origin: this backend's own publicly reachable origin
    # (scheme + host, no trailing slash -- e.g.
    # "https://api.astronomicconnect.com"), used ONLY to build
    # fully-qualified outbound links that must work from OUTSIDE this
    # app entirely (an email client, a mail-security scanner) -- NOT the
    # same thing as frontend_origin (where a browser gets sent back to
    # after an OAuth callback) or the frontend's own BACKEND_ORIGIN
    # rewrite-proxy target (next.config.ts -- that's for the browser's
    # own same-origin calls while looking at a Hub page, a different
    # concern entirely). Deliberately left unset until
    # api.astronomicconnect.com's TLS certificate is actually ready --
    # see the B1.5/B3 investigation history for why the current Railway
    # domain is not the intended long-term value here.
    #
    # unsubscribe_token_encryption_keys: one or more Fernet keys,
    # comma-separated, NEWEST KEY FIRST (e.g. "new_key,old_key") -- a
    # completely separate secret from mailbox_token_encryption_key above
    # (different trust domain, different rotation cadence; rotating one
    # must never require touching the other). The first key is used to
    # encrypt every newly generated unsubscribe token; ALL configured
    # keys are tried when decrypting (via cryptography.fernet.MultiFernet)
    # -- so an old token stays valid across a key rotation for as long as
    # its issuing key remains present in this list, and is only ever
    # truly retired once that key is finally dropped.
    public_backend_origin: str | None = None
    unsubscribe_token_encryption_keys: str | None = None

    # Internal Hub login (single shared account -- see app/services/auth_service.py's
    # module docstring for why this phase deliberately has no per-user
    # accounts/signup/roles). Both None until configured; POST /auth/login
    # returns a clear 503 rather than ever accepting a login when unset, so
    # the app fails CLOSED (nobody can log in) rather than open.
    #
    # auth_email: the one allowed login email, compared case-insensitively
    # (see app.models.crm.normalize_email).
    #
    # auth_password_hash: NEVER the plaintext password -- see
    # app/services/password_hashing.py's module docstring for the exact,
    # standard-library-only command that generates this value. Set the
    # ENTIRE printed string (starting with "pbkdf2_sha256$") as this value.
    #
    # cookie_secure: whether the session cookie is marked `Secure` (HTTPS
    # only). Defaults True (production-safe); only reason to ever set this
    # False is testing the login flow in a real browser over plain
    # http://localhost, where a Secure cookie would silently never be sent.
    auth_email: str | None = None
    auth_password_hash: str | None = None
    cookie_secure: bool = True

    # Luma (lu.ma) -> Hub CRM integration -- see app/luma/client.py and
    # app/services/luma_sync_service.py. Both None until deliberately
    # configured; the webhook route and any outbound Luma call return a
    # clear 503 rather than crashing app startup, matching every other
    # integration credential's precedent above.
    #
    # luma_api_key: a Calendar API key from Luma (Calendar Settings ->
    # Developer -> API Keys), scoped to Astronomic's one Luma calendar --
    # requires Luma Plus. Used ONLY for outbound calls this backend makes
    # to Luma (event/guest listing for backfill); never sent to the
    # frontend, never logged, never returned by any route.
    #
    # luma_webhook_secret: the per-webhook `whsec_...` signing secret Luma
    # returns when the webhook is created (Calendar Settings -> Developer
    # -> Webhooks, or POST /v2/webhooks/create) -- used to verify the
    # `Webhook-Signature` header on every inbound delivery (see
    # app/luma/webhook_signature.py). This is Luma's own documented
    # signature scheme, not a bearer token we issue.
    luma_api_key: str | None = None
    luma_webhook_secret: str | None = None

    # Astronomic Mail Phase A (durable execution model) -- see
    # app/services/mail_sending_service.py's module docstring. Defaults to
    # False (fails CLOSED, matching cookie_secure's precedent above, not
    # auth_email/itf_webhook_token's "None until configured" shape, since
    # this one genuinely needs an explicit BOOLEAN choice rather than a
    # credential). Gates ONLY MailCampaignService.activate_campaign() --
    # the state machine, every other lifecycle method, and the full
    # execution/safety service remain fully testable regardless of this
    # setting; only the one transition that could put a REAL production
    # campaign into ACTIVE is affected. This exists because /activate is a
    # real, callable route the instant this backend deploys, but no
    # worker/sender exists yet to safely act on an ACTIVE campaign (Phase
    # C/D) -- "the frontend doesn't show the button" is not a safety
    # boundary on its own; this is. Flip to True only when Phase C's
    # worker is intentionally being turned on.
    mail_sending_engine_enabled: bool = False

    # Astronomic Mail Phase C (Campaign Execution Worker) -- controlled-test
    # safety gates, SEPARATE from mail_sending_engine_enabled above and
    # deliberately fail-CLOSED in the opposite direction from every other
    # setting in this file: most settings here are "None means the feature
    # is off." These two are "None (or either one being None) means NO
    # PROVIDER INVOCATION IS EVER PERMITTED, even with the engine enabled" --
    # see MailSendingService's controlled-test gate check for the exact
    # enforcement. Both must be explicitly configured, and BOTH must match
    # (mailbox AND recipient), before Phase C's worker may ever cross the
    # SENDING boundary for a real provider call. This exists specifically so
    # that accidentally flipping mail_sending_engine_enabled=True can never,
    # by itself, let a real campaign reach real contacts during the first
    # disposable-mailbox controlled test.
    #
    # mail_sending_mailbox_allowlist: comma-separated mailbox_id values.
    # Exact match only -- no wildcards, no domain-level allowance. An
    # enrollment's assigned mailbox must be in this list or nothing sends.
    #
    # mail_sending_recipient_allowlist: comma-separated, NORMALIZED
    # (lowercased/trimmed -- see app.models.crm.normalize_email) recipient
    # email addresses. Exact match only, same reasoning -- deliberately NOT
    # domain-level (e.g. no bare "@astronomic.com") for this first
    # implementation: a small, explicit, reviewable list of the exact
    # addresses the first controlled test targets is safer than anything
    # that could match an address nobody actually intended to include.
    mail_sending_mailbox_allowlist: str | None = None
    mail_sending_recipient_allowlist: str | None = None


settings = Settings()
