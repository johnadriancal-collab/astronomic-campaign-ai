"""
FastAPI entry point for Astronomic Campaign AI.

Endpoints:
    GET  /health                     - health check
    POST /campaign/preview           - generates CampaignPlan ONCE, creates + stores a Campaign
    POST /campaign/search            - loads a Campaign by id, searches/ranks using its stored plan
    POST /campaign/build             - loads a Campaign by id, builds in Apollo from its stored plan/selection
    GET  /campaign/{campaign_id}     - fetch full Campaign state
    GET  /campaign/{campaign_id}/leads - the campaign's real, persisted Leads
    POST /campaign/{campaign_id}/ready    - human approval gate, no Apollo call
    POST /campaign/{campaign_id}/activate - activates in Apollo, persists only on success
    POST /campaign/{campaign_id}/pause    - deactivates in Apollo, persists only on success
    GET  /campaign/{campaign_id}/sequence      - the campaign's synced EmailSequence + steps
    POST /campaign/{campaign_id}/sequence/sync - explicit manual sync against Apollo
    GET  /campaign/{campaign_id}/messages                    - synced + fixture EmailMessages for this campaign
    POST /campaign/{campaign_id}/messages/sync                - explicit manual message sync against Apollo
    POST /campaign/{campaign_id}/messages/fixtures             - generate local test-fixture messages (no Apollo call)
    GET  /campaign/{campaign_id}/messages/{message_id}/events        - a message's synced open/click events
    POST /campaign/{campaign_id}/messages/{message_id}/sync-events   - explicit manual event sync for one message
    POST /sync/campaigns             - discovers/updates/archives campaigns from Apollo's current sequence list
    POST /sync/itf-contact           - webhook target for the Apps Script ITF bridge, one Form submission per call (dry_run supported)
    GET  /crm/activity                - the CRM Activity Log, newest first (category/search/date filter + pagination)
    POST /crm/activity/exports        - records a client-side CSV export as an Activity Log event (best-effort, write-only)
    POST /sync/email-intake                 - webhook target for a (Phase 2, not yet activated) Apps Script Gmail bridge, idempotent on gmail_message_id
    GET  /crm/email-intake                  - the Email Intake review queue, newest first (status/search filter + pagination)
    GET  /crm/email-intake/{intake_id}      - one intake item's full detail (source email, match, proposal)
    POST /crm/email-intake/{intake_id}/match   - reviewer-selected CRM contact for a NEEDS_MATCH item
    POST /crm/email-intake/{intake_id}/approve - applies ONLY the checked proposed fields via CrmService.update_contact()
    POST /crm/email-intake/{intake_id}/reject  - marks the item Rejected; never touches the CRM

Campaign routes themselves live in app/api/campaign.py (see
docs/ARCHITECTURE.md) -- this module only owns app-level concerns: the
homepage/health routes, and the CampaignService singleton's lifecycle
(opens the persistent SQLite store once at startup, closes it at
shutdown).

No endpoint after /campaign/preview ever regenerates a CampaignPlan --
every later stage loads the plan already stored on the Campaign.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.activity import router as activity_router
from app.api.astro import router as astro_router
from app.api.campaign import router as campaign_router
from app.api.campaign_manager import router as campaign_manager_router
from app.api.crm import router as crm_router
from app.api.email_intake import crm_router as email_intake_crm_router
from app.api.email_intake import sync_router as email_intake_sync_router
from app.api.leads import router as leads_router
from app.api.mail import router as mail_router
from app.api.mailboxes import router as mailboxes_router
from app.api.sync import router as sync_router
from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_campaign_lead_store import SQLiteCampaignLeadStore
from app.repositories.sqlite_campaign_store import SQLiteCampaignStore
from app.repositories.sqlite_crm_contact_list_member_store import SQLiteCrmContactListMemberStore
from app.repositories.sqlite_crm_contact_list_store import SQLiteCrmContactListStore
from app.repositories.sqlite_crm_contact_store import SQLiteCrmContactStore
from app.repositories.sqlite_crm_custom_field_store import SQLiteCrmCustomFieldStore
from app.repositories.sqlite_crm_import_batch_store import SQLiteCrmImportBatchStore
from app.repositories.sqlite_email_intake_store import SQLiteEmailIntakeStore
from app.repositories.sqlite_email_message_event_store import SQLiteEmailMessageEventStore
from app.repositories.sqlite_email_message_store import SQLiteEmailMessageStore
from app.repositories.sqlite_email_sequence_step_store import SQLiteEmailSequenceStepStore
from app.repositories.sqlite_email_sequence_store import SQLiteEmailSequenceStore
from app.repositories.sqlite_itf_ingestion_log_store import SQLiteItfIngestionLogStore
from app.repositories.sqlite_lead_store import SQLiteLeadStore
from app.repositories.sqlite_mail_campaign_store import SQLiteMailCampaignStore
from app.repositories.sqlite_mail_enrollment_store import SQLiteMailEnrollmentStore
from app.repositories.sqlite_mail_sequence_step_store import SQLiteMailSequenceStepStore
from app.repositories.sqlite_mail_suppression_store import SQLiteMailSuppressionStore
from app.repositories.sqlite_mailbox_credential_store import SQLiteMailboxCredentialStore
from app.repositories.sqlite_mailbox_store import SQLiteMailboxStore
from app.google.oauth_client import GoogleOAuthClient
from app.services.activity_log_service import ActivityLogService
from app.services.campaign_service import CampaignService
from app.services.campaign_sync_service import CampaignSyncService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.email_intake_service import EmailIntakeService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.itf_ingestion_service import ItfIngestionService
from app.services.lead_service import LeadService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_suppression_service import MailSuppressionService
from app.services.mailbox_service import MailboxService


@asynccontextmanager
async def lifespan(app: FastAPI):
    # All stores share the same SQLite file (different tables) -- each
    # manages its own connection independently, matching
    # SQLiteCampaignStore's existing self-contained connect()/close().
    campaign_store = SQLiteCampaignStore(settings.database_path)
    lead_store = SQLiteLeadStore(settings.database_path)
    campaign_lead_store = SQLiteCampaignLeadStore(settings.database_path)
    email_sequence_store = SQLiteEmailSequenceStore(settings.database_path)
    email_sequence_step_store = SQLiteEmailSequenceStepStore(settings.database_path)
    email_message_store = SQLiteEmailMessageStore(settings.database_path)
    email_message_event_store = SQLiteEmailMessageEventStore(settings.database_path)
    crm_contact_store = SQLiteCrmContactStore(settings.database_path)
    crm_custom_field_store = SQLiteCrmCustomFieldStore(settings.database_path)
    crm_import_batch_store = SQLiteCrmImportBatchStore(settings.database_path)
    crm_contact_list_store = SQLiteCrmContactListStore(settings.database_path)
    crm_contact_list_member_store = SQLiteCrmContactListMemberStore(settings.database_path)
    itf_ingestion_log_store = SQLiteItfIngestionLogStore(settings.database_path)
    activity_event_store = SQLiteActivityEventStore(settings.database_path)
    email_intake_store = SQLiteEmailIntakeStore(settings.database_path)
    mail_campaign_store = SQLiteMailCampaignStore(settings.database_path)
    mail_sequence_step_store = SQLiteMailSequenceStepStore(settings.database_path)
    mail_enrollment_store = SQLiteMailEnrollmentStore(settings.database_path)
    mail_suppression_store = SQLiteMailSuppressionStore(settings.database_path)
    mailbox_store = SQLiteMailboxStore(settings.database_path)
    mailbox_credential_store = SQLiteMailboxCredentialStore(settings.database_path)
    await campaign_store.connect()
    await lead_store.connect()
    await campaign_lead_store.connect()
    await email_sequence_store.connect()
    await email_sequence_step_store.connect()
    await email_message_store.connect()
    await email_message_event_store.connect()
    await crm_contact_store.connect()
    await crm_custom_field_store.connect()
    await crm_import_batch_store.connect()
    await crm_contact_list_store.connect()
    await crm_contact_list_member_store.connect()
    await itf_ingestion_log_store.connect()
    await activity_event_store.connect()
    await email_intake_store.connect()
    await mail_campaign_store.connect()
    await mail_sequence_step_store.connect()
    await mail_enrollment_store.connect()
    await mail_suppression_store.connect()
    await mailbox_store.connect()
    await mailbox_credential_store.connect()

    activity_log_service = ActivityLogService(store=activity_event_store)
    app.state.activity_log_service = activity_log_service

    lead_service = LeadService(store=lead_store, campaign_lead_store=campaign_lead_store, campaign_store=campaign_store)
    app.state.lead_service = lead_service
    app.state.campaign_service = CampaignService(
        store=campaign_store,
        lead_service=lead_service,
        campaign_lead_store=campaign_lead_store,
        activity_log=activity_log_service,
    )
    app.state.email_sequence_sync_service = EmailSequenceSyncService(
        campaign_store=campaign_store, store=email_sequence_store, step_store=email_sequence_step_store
    )
    app.state.email_message_sync_service = EmailMessageSyncService(
        sequence_store=email_sequence_store,
        step_store=email_sequence_step_store,
        message_store=email_message_store,
        event_store=email_message_event_store,
        lead_store=lead_store,
        campaign_lead_store=campaign_lead_store,
    )
    app.state.campaign_sync_service = CampaignSyncService(
        campaign_store=campaign_store,
        sequence_store=email_sequence_store,
        step_store=email_sequence_step_store,
        activity_log=activity_log_service,
    )

    crm_service = CrmService(
        contact_store=crm_contact_store,
        custom_field_store=crm_custom_field_store,
        list_store=crm_contact_list_store,
        list_member_store=crm_contact_list_member_store,
        activity_log=activity_log_service,
    )
    app.state.crm_service = crm_service
    crm_import_service = CrmImportService(crm_service=crm_service, batch_store=crm_import_batch_store)
    app.state.crm_import_service = crm_import_service

    # ITF (Investor Thesis Form) intake -- no Google credentials involved, so this
    # is always constructed; POST /sync/itf-contact's own auth dependency (not this
    # wiring) is what returns a clear 503 when ITF_WEBHOOK_TOKEN isn't configured.
    app.state.itf_ingestion_service = ItfIngestionService(
        import_service=crm_import_service,
        log_store=itf_ingestion_log_store,
        activity_log=activity_log_service,
    )

    # Email -> CRM Intake (Phase 1) -- no Gmail credentials involved (the
    # Phase 2 Apps Script bridge is NOT activated), so this is always
    # constructed; POST /sync/email-intake's own auth dependency (not this
    # wiring) is what returns a clear 503 when EMAIL_INTAKE_WEBHOOK_TOKEN
    # isn't configured.
    app.state.email_intake_service = EmailIntakeService(
        store=email_intake_store,
        crm_service=crm_service,
        activity_log=activity_log_service,
    )

    # Astronomic Mail (Phase 1 -- Foundation). No Gmail/OAuth credentials
    # involved and no sending capability exists anywhere in this wiring or
    # in the services/routes it constructs -- see app/api/mail.py's module
    # docstring. MailCampaignService depends on crm_service (read-only:
    # get_contact_list/get_list_contacts) to resolve a campaign's audience;
    # it never mutates a CrmContact or CrmContactList.
    app.state.mail_suppression_service = MailSuppressionService(
        store=mail_suppression_store,
        activity_log=activity_log_service,
    )
    app.state.mail_campaign_service = MailCampaignService(
        campaign_store=mail_campaign_store,
        step_store=mail_sequence_step_store,
        enrollment_store=mail_enrollment_store,
        crm_service=crm_service,
        activity_log=activity_log_service,
    )

    # Astronomic Mail Phase 2 (Google Workspace Mailbox Connection). CSRF
    # `state` lives only in MailboxService's in-memory dict, not a store --
    # see that class's docstring. GoogleOAuthClient makes real network calls
    # to Google only when GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI are
    # configured; every route that needs them returns a clear 503 otherwise
    # (see app/api/mailboxes.py), never crashing app startup.
    app.state.mailbox_service = MailboxService(
        mailbox_store=mailbox_store,
        credential_store=mailbox_credential_store,
        oauth_client=GoogleOAuthClient(),
    )

    yield
    await campaign_store.close()
    await lead_store.close()
    await campaign_lead_store.close()
    await email_sequence_store.close()
    await email_sequence_step_store.close()
    await email_message_store.close()
    await email_message_event_store.close()
    await crm_contact_store.close()
    await crm_custom_field_store.close()
    await crm_import_batch_store.close()
    await crm_contact_list_store.close()
    await crm_contact_list_member_store.close()
    await itf_ingestion_log_store.close()
    await activity_event_store.close()
    await email_intake_store.close()
    await mail_campaign_store.close()
    await mail_sequence_step_store.close()
    await mail_enrollment_store.close()
    await mail_suppression_store.close()
    await mailbox_store.close()
    await mailbox_credential_store.close()


app = FastAPI(title="Astronomic Campaign AI", lifespan=lifespan)
app.include_router(campaign_router)
app.include_router(campaign_manager_router)
app.include_router(leads_router)
app.include_router(sync_router)
app.include_router(crm_router)
app.include_router(astro_router)
app.include_router(activity_router)
app.include_router(email_intake_sync_router)
app.include_router(email_intake_crm_router)
app.include_router(mail_router)
app.include_router(mailboxes_router)

HOMEPAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Astronomic Campaign AI</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 4rem auto; padding: 0 1.5rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .status { display: inline-flex; align-items: center; gap: 0.5rem; font-size: 0.95rem; margin: 1rem 0 2rem; }
  .dot { width: 0.6rem; height: 0.6rem; border-radius: 50%; background: #999; }
  .dot.ok { background: #2ea043; }
  .dot.err { background: #d1242f; }
  ul { line-height: 1.9; padding-left: 1.2rem; }
  code { background: #f0f0f0; padding: 0.1rem 0.35rem; border-radius: 4px; font-size: 0.9em; }
</style>
</head>
<body>
<h1>Astronomic Campaign AI</h1>
<div class="status"><span class="dot" id="dot"></span><span id="status-text">Checking server status…</span></div>
<ul>
  <li><a href="/docs">Swagger UI</a> — interactive API docs, try endpoints from the browser</li>
  <li><a href="/redoc">ReDoc</a> — read-only API reference</li>
  <li><a href="/health">/health</a> — raw health-check JSON</li>
</ul>
<p>No dedicated frontend yet — <code>POST /campaign/preview</code>, <code>/campaign/search</code>, and <code>/campaign/build</code> are exercised via the Swagger UI or <code>curl</code> for now.</p>
<script>
fetch("/health").then(r => r.json()).then(d => {
  document.getElementById("dot").classList.add(d.status === "ok" ? "ok" : "err");
  document.getElementById("status-text").textContent = d.status === "ok" ? "Server is running" : "Server responded with an error";
}).catch(() => {
  document.getElementById("dot").classList.add("err");
  document.getElementById("status-text").textContent = "Could not reach /health";
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return HOMEPAGE_HTML


@app.get("/health")
async def health():
    return {"status": "ok"}
