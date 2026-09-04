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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.api.activity import router as activity_router
from app.api.astro import router as astro_router
from app.api.astro_ai import router as astro_ai_router
from app.api.auth import router as auth_router
from app.api.campaign import router as campaign_router
from app.api.campaign_manager import router as campaign_manager_router
from app.api.crm import router as crm_router
from app.api.email_intake import crm_router as email_intake_crm_router
from app.api.email_intake import sync_router as email_intake_sync_router
from app.api.leads import router as leads_router
from app.api.luma import mapping_router as luma_mapping_router
from app.api.luma import router as luma_router
from app.api.mail import router as mail_router
from app.api.mail_unsubscribe import router as mail_unsubscribe_router
from app.api.mailboxes import router as mailboxes_router
from app.api.sync import router as sync_router
from app import access_log_filter
from app.config import settings
from app.repositories.sqlite_activity_event_store import SQLiteActivityEventStore
from app.repositories.sqlite_auth_session_store import SQLiteAuthSessionStore
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
from app.repositories.sqlite_luma_backfill_checkpoint_store import SQLiteLumaBackfillCheckpointStore
from app.repositories.sqlite_luma_event_store import SQLiteLumaEventStore
from app.repositories.sqlite_luma_question_mapping_store import SQLiteLumaQuestionMappingStore
from app.repositories.sqlite_luma_registration_store import SQLiteLumaRegistrationStore
from app.repositories.sqlite_mail_campaign_mailbox_store import SQLiteMailCampaignMailboxStore
from app.repositories.sqlite_mail_campaign_store import SQLiteMailCampaignStore
from app.repositories.sqlite_mail_campaign_csv_prospect_link_store import SQLiteMailCampaignCsvProspectLinkStore
from app.repositories.sqlite_mail_enrollment_batch_member_store import SQLiteMailEnrollmentBatchMemberStore
from app.repositories.sqlite_mail_enrollment_batch_store import SQLiteMailEnrollmentBatchStore
from app.repositories.sqlite_mail_enrollment_step_store import SQLiteMailEnrollmentStepStore
from app.repositories.sqlite_mail_enrollment_store import SQLiteMailEnrollmentStore
from app.repositories.sqlite_mail_lead_start_trigger_store import SQLiteMailLeadStartTriggerStore
from app.repositories.sqlite_mail_send_window_store import SQLiteMailSendWindowStore
from app.repositories.sqlite_mail_sequence_step_store import SQLiteMailSequenceStepStore
from app.repositories.sqlite_mail_trigger_occurrence_store import SQLiteMailTriggerOccurrenceStore
from app.repositories.sqlite_mail_suppression_store import SQLiteMailSuppressionStore
from app.repositories.sqlite_mailbox_credential_store import SQLiteMailboxCredentialStore
from app.repositories.sqlite_mailbox_send_policy_store import SQLiteMailboxSendPolicyStore
from app.repositories.sqlite_mailbox_store import SQLiteMailboxStore
from app.repositories.sqlite_worker_lease_store import SQLiteWorkerLeaseStore
from app.session_auth_middleware import enforce_session_auth
from app.google.gmail_api_client import GmailApiClient
from app.google.gmail_sender import GmailSender
from app.google.oauth_client import GoogleOAuthClient
from app.luma.client import LumaClient
from app.services.activity_log_service import ActivityLogService
from app.services.astro_activity_tools import AstroActivityTools
from app.services.astro_ai_service import AstroAiService, build_default_claude_client
from app.services.astro_campaign_tools import AstroCampaignTools
from app.services.astro_crm_tools import AstroCrmTools
from app.services.astro_export_store import AstroExportStore
from app.services.astro_hub_tools import AstroHubTools
from app.services.astro_mailbox_tools import AstroMailboxTools
from app.services.auth_service import SESSION_COOKIE_NAME, AuthService
from app.services.campaign_service import CampaignService
from app.services.campaign_sync_service import CampaignSyncService
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.email_intake_service import EmailIntakeService
from app.services.email_message_sync_service import EmailMessageSyncService
from app.services.email_sequence_sync_service import EmailSequenceSyncService
from app.services.itf_ingestion_service import ItfIngestionService
from app.services.lead_service import LeadService
from app.services.luma_sync_service import LumaSyncService
from app.services.mail_campaign_csv_prospect_service import MailCampaignCsvProspectService
from app.services.mail_campaign_service import MailCampaignService
from app.services.mail_batch_reconciliation_worker import MailBatchReconciliationWorker
from app.services.mail_execution_worker import MailExecutionWorker
from app.services.mail_sending_service import MailSendingService
from app.services.mail_suppression_service import MailSuppressionService
from app.services.mailbox_service import MailboxService
from app.services.worker_lease_service import WorkerLeaseService


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
    mail_campaign_mailbox_store = SQLiteMailCampaignMailboxStore(settings.database_path)
    mail_send_window_store = SQLiteMailSendWindowStore(settings.database_path)
    mail_enrollment_step_store = SQLiteMailEnrollmentStepStore(settings.database_path)
    mail_enrollment_batch_store = SQLiteMailEnrollmentBatchStore(settings.database_path)
    mail_enrollment_batch_member_store = SQLiteMailEnrollmentBatchMemberStore(settings.database_path)
    mail_campaign_csv_prospect_link_store = SQLiteMailCampaignCsvProspectLinkStore(settings.database_path)
    # Trigger feature foundation (Stage 5A, 2026-09-04) -- schema/persistence
    # only, connected here so the tables exist, but not yet injected into any
    # service/route: nothing in this codebase creates, reads, or executes a
    # trigger/occurrence yet. See app/models/mail.py's Trigger docstrings.
    mail_lead_start_trigger_store = SQLiteMailLeadStartTriggerStore(settings.database_path)
    mail_trigger_occurrence_store = SQLiteMailTriggerOccurrenceStore(settings.database_path)
    mailbox_send_policy_store = SQLiteMailboxSendPolicyStore(settings.database_path)
    mailbox_store = SQLiteMailboxStore(settings.database_path)
    mailbox_credential_store = SQLiteMailboxCredentialStore(settings.database_path)
    auth_session_store = SQLiteAuthSessionStore(settings.database_path)
    luma_event_store = SQLiteLumaEventStore(settings.database_path)
    luma_registration_store = SQLiteLumaRegistrationStore(settings.database_path)
    luma_question_mapping_store = SQLiteLumaQuestionMappingStore(settings.database_path)
    luma_backfill_checkpoint_store = SQLiteLumaBackfillCheckpointStore(settings.database_path)
    worker_lease_store = SQLiteWorkerLeaseStore(settings.database_path)
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
    await mail_campaign_mailbox_store.connect()
    await mail_send_window_store.connect()
    await mail_enrollment_step_store.connect()
    await mail_enrollment_batch_store.connect()
    await mail_enrollment_batch_member_store.connect()
    await mail_campaign_csv_prospect_link_store.connect()
    await mail_lead_start_trigger_store.connect()
    await mail_trigger_occurrence_store.connect()
    await mailbox_send_policy_store.connect()
    await mailbox_store.connect()
    await mailbox_credential_store.connect()
    await auth_session_store.connect()
    await luma_event_store.connect()
    await luma_registration_store.connect()
    await luma_question_mapping_store.connect()
    await luma_backfill_checkpoint_store.connect()
    await worker_lease_store.connect()

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
    # Phase A (durable execution model). At the time Phase A shipped, this
    # MailSendingService had no concrete MailSenderPort and nothing called
    # process_one_due_step() on a schedule. Phase C (further down in this
    # same function) later wires a real GmailSender into this exact
    # instance and starts a worker that does call it on a schedule -- see
    # app/services/mail_execution_worker.py's own docstring for the
    # multi-layered gating (mail_sending_engine_enabled, the controlled-
    # test allowlists, the worker lease) that governs whether that
    # schedule can ever actually reach Gmail in a given environment.
    mail_sending_service = MailSendingService(
        campaign_store=mail_campaign_store,
        enrollment_store=mail_enrollment_store,
        step_store=mail_enrollment_step_store,
        mailbox_store=mailbox_store,
        channel_store=mail_campaign_mailbox_store,
        policy_store=mailbox_send_policy_store,
        suppression_store=mail_suppression_store,
        activity_log=activity_log_service,
    )
    app.state.mail_sending_service = mail_sending_service
    app.state.mail_campaign_service = MailCampaignService(
        campaign_store=mail_campaign_store,
        step_store=mail_sequence_step_store,
        enrollment_store=mail_enrollment_store,
        crm_service=crm_service,
        activity_log=activity_log_service,
        mailbox_store=mailbox_store,
        channel_store=mail_campaign_mailbox_store,
        window_store=mail_send_window_store,
        enrollment_step_store=mail_enrollment_step_store,
        sending_service=mail_sending_service,
        batch_store=mail_enrollment_batch_store,
        batch_member_store=mail_enrollment_batch_member_store,
        suppression_store=mail_suppression_store,
        # Stage 4B (2026-09-03): the SAME crm_import_service instance
        # constructed above, but MailCampaignService only ever sees it as
        # the narrow, read-only CrmImportResolutionReader Protocol -- see
        # that Protocol's own docstring. MailCampaignCsvProspectService,
        # constructed right below, is the one place that gets the FULL,
        # writable crm_import_service.
        crm_import_reader=crm_import_service,
    )
    app.state.mail_campaign_csv_prospect_service = MailCampaignCsvProspectService(
        crm_import_service=crm_import_service,
        mail_campaign_service=app.state.mail_campaign_service,
        link_store=mail_campaign_csv_prospect_link_store,
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
        activity_log=activity_log_service,
    )

    # Astronomic Mail Phase C (Campaign Execution Worker). Connects Phase
    # A's execution model, B1's OAuth foundation, B2's Gmail sender, and
    # B3's unsubscribe composition -- see app/services/
    # mail_execution_worker.py's own module docstring for the full,
    # multi-layered "still cannot send in THIS environment" chain
    # (mail_sending_engine_enabled unset -> worker.start() never even
    # polls; both controlled-test allowlists unset -> the one provider-
    # bound check that could pass even if the engine were on still fails
    # closed; the database lease -> only one process-instance can ever
    # claim work at all). Constructing GmailSender here is itself
    # harmless -- see MailSenderPort/GmailSender's own docstrings: a
    # constructed-but-never-successfully-invoked sender sends nothing.
    worker_lease_service = WorkerLeaseService(store=worker_lease_store)
    gmail_sender = GmailSender(mailbox_service=app.state.mailbox_service, gmail_api_client=GmailApiClient())
    mail_execution_worker = MailExecutionWorker(
        mail_sending_service=mail_sending_service,
        mail_campaign_service=app.state.mail_campaign_service,
        lease_service=worker_lease_service,
        sender=gmail_sender,
        activity_log=activity_log_service,
    )
    app.state.mail_execution_worker = mail_execution_worker
    mail_execution_worker.start()

    # Phase 2 Stage 3 -- recovers any MailEnrollmentBatch left PREPARING by
    # a crashed/interrupted add_prospects() call, and cleans up orphaned
    # MailEnrollmentBatchMember rows. Deliberately started unconditionally
    # (no mail_sending_engine_enabled gate) and makes zero Gmail/provider
    # calls -- see MailBatchReconciliationWorker's own module docstring.
    mail_batch_reconciliation_worker = MailBatchReconciliationWorker(
        mail_campaign_service=app.state.mail_campaign_service,
    )
    app.state.mail_batch_reconciliation_worker = mail_batch_reconciliation_worker
    mail_batch_reconciliation_worker.start()

    # Internal Hub login -- a single shared account, no signup/roles/teams
    # (see app/services/auth_service.py's module docstring). AUTH_EMAIL/
    # AUTH_PASSWORD_HASH unset means POST /auth/login always 503s -- the
    # app fails CLOSED, never open, when unconfigured.
    app.state.auth_service = AuthService(session_store=auth_session_store)

    # Astro AI chat (Phase 1 general assistant + Phase 2 read-only CRM
    # tool-use + Phase 3 read-only Campaign Manager/Lists/mailbox/Activity
    # Log tool-use). Stateless: no store to connect/close, matching Astro
    # Search's own precedent. Reuses ANTHROPIC_API_KEY (see
    # app/config.py) -- there is only ever one Anthropic credential in
    # this app -- with its own model setting (astro_chat_model).
    # AstroHubTools composes four per-domain tool surfaces, each wrapping
    # the SAME already-constructed service/store this app's own routes
    # use -- read-only only (see each astro_*_tools.py module docstring),
    # never a second/parallel system. AstroMailboxTools is given
    # `mailbox_store` directly (never `mailbox_service`, which also holds
    # a `MailboxCredentialStore` reference) -- see astro_mailbox_tools.py
    # for why that's a structural, not just conventional, guarantee.
    # In-memory, 15-minute-TTL holding area for Astro AI's export_crm_contacts
    # CSV bytes (app/services/astro_export_store.py). Process-local -- see
    # that module's docstring for the documented single-instance constraint.
    astro_export_store = AstroExportStore()
    app.state.astro_export_store = astro_export_store

    app.state.astro_ai_service = AstroAiService(
        claude_client=build_default_claude_client(),
        hub_tools=AstroHubTools(
            crm_tools=AstroCrmTools(
                crm_service, export_store=astro_export_store, activity_log_service=activity_log_service
            ),
            mailbox_tools=AstroMailboxTools(mailbox_store),
            activity_tools=AstroActivityTools(activity_log_service),
            campaign_tools=AstroCampaignTools(
                campaign_service=app.state.campaign_service,
                mail_campaign_service=app.state.mail_campaign_service,
                mail_suppression_service=app.state.mail_suppression_service,
                email_sequence_store=app.state.email_sequence_sync_service.store,
            ),
        ),
    )

    # Luma (lu.ma) -> Hub CRM sync (see app/services/luma_sync_service.py).
    # Single-calendar Calendar API key scope only (approved architecture --
    # no organization-key/multi-calendar abstraction). LumaClient is
    # constructed even when LUMA_API_KEY is unset (matches
    # build_default_claude_client's precedent above); it only raises
    # LumaNotConfiguredError lazily, on an actual outbound call (i.e. only
    # the backfill route needs it -- the webhook path never calls Luma).
    app.state.luma_sync_service = LumaSyncService(
        crm_service=crm_service,
        event_store=luma_event_store,
        registration_store=luma_registration_store,
        mapping_store=luma_question_mapping_store,
        activity_log=activity_log_service,
        checkpoint_store=luma_backfill_checkpoint_store,
        luma_client=LumaClient(),
    )

    yield
    # Worker stop MUST happen before any store closes -- see
    # MailExecutionWorker.stop()'s own docstring on why (no coroutine may
    # still be mid-write when a connection closes underneath it). Same
    # reasoning applies to the reconciliation worker.
    await mail_execution_worker.stop()
    await mail_batch_reconciliation_worker.stop()
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
    await mail_campaign_mailbox_store.close()
    await mail_send_window_store.close()
    await mail_enrollment_step_store.close()
    await mail_enrollment_batch_store.close()
    await mail_enrollment_batch_member_store.close()
    await mail_campaign_csv_prospect_link_store.close()
    await mail_lead_start_trigger_store.close()
    await mail_trigger_occurrence_store.close()
    await mailbox_send_policy_store.close()
    await mailbox_store.close()
    await mailbox_credential_store.close()
    await auth_session_store.close()
    await luma_event_store.close()
    await luma_registration_store.close()
    await luma_question_mapping_store.close()
    await luma_backfill_checkpoint_store.close()
    await worker_lease_store.close()


app = FastAPI(title="Astronomic Campaign AI", lifespan=lifespan)

# Strips query strings from Uvicorn's own access-log lines (never touches
# request handling -- see app/access_log_filter.py's own docstring for
# the root cause this fixes: an OAuth callback's `code`/`state` query
# params otherwise land verbatim in Railway's captured logs). Installed
# here, at import time, which Uvicorn's CLI entry point guarantees runs
# AFTER Uvicorn has already configured its own "uvicorn.access" logger
# (Config.__init__ calls configure_logging() before the app string is
# ever imported) -- so this always layers onto, never races, that setup.
access_log_filter.install()

app.middleware("http")(enforce_session_auth)
app.include_router(auth_router)
app.include_router(astro_ai_router)
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
app.include_router(mail_unsubscribe_router)
app.include_router(mailboxes_router)
app.include_router(luma_router)
app.include_router(luma_mapping_router)

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
async def health(request: Request):
    """`status` stays "ok" (HTTP 200) regardless of the worker's own
    state -- a disabled or non-leader Phase C worker is a NORMAL,
    expected condition in most deployments (e.g. every non-leader replica
    if Railway is ever misconfigured with more than one), never a reason
    for Railway's own healthcheck to restart an otherwise-healthy web
    service. `mail_worker` is purely informational -- see
    MailExecutionWorker.liveness_snapshot()'s own docstring."""
    worker = getattr(request.app.state, "mail_execution_worker", None)
    body = {"status": "ok"}
    if worker is not None:
        body["mail_worker"] = worker.liveness_snapshot()
    return body
