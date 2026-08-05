/**
 * Client for the existing FastAPI backend. Requests go through the
 * `/backend/*` rewrite in next.config.ts, which proxies server-side to
 * BACKEND_ORIGIN -- this keeps the browser same-origin (no CORS) without
 * requiring any change to the FastAPI app itself.
 *
 * `Campaign` is the single source of truth returned by every endpoint --
 * preview/search/build all return the same shape, just with more fields
 * populated at each stage. There is no separate "plan" or "report" type
 * that could drift out of sync with it.
 */

export type CampaignStatus =
  | "draft"
  | "searched"
  | "building"
  | "built"
  | "failed"
  | "ready"
  | "active"
  | "paused";

export interface CampaignFilters {
  locations: string[];
  industries: string[];
  titles: string[];
  company_size: string[];
  funding_stage: string[];
}

export interface SequenceStep {
  day: number;
  subject: string;
  body: string;
}

export interface CampaignPlan {
  campaign_name: string;
  filters: CampaignFilters;
  sequence: SequenceStep[];
  launch: boolean;
}

export interface ApolloPerson {
  id: string;
  first_name?: string;
  last_name_obfuscated?: string;
  title?: string;
  has_email?: boolean;
  organization?: {
    name?: string;
  };
  claude_score?: number;
  claude_reason?: string;
}

export interface BuildReport {
  apollo_list_id: string | null;
  apollo_sequence_id: string | null;
  contacts_created: number;
  contacts_enrolled: number;
  activated: boolean;
  errors: string[];
}

/**
 * Internal sync bookkeeping only -- never rendered anywhere. A Campaign
 * is a Campaign regardless of this value; it exists purely so
 * CampaignSyncService knows which records it owns and may overwrite.
 */
export type CampaignSource = "native" | "synced";

export interface Campaign {
  campaign_id: string;
  original_prompt: string;
  created_at: string;
  status: CampaignStatus;
  source: CampaignSource;
  plan: CampaignPlan;

  desired_prospect_count: number;
  retrieval_pool_size: number;
  total_matches: number | null;
  selected_prospects: ApolloPerson[];
  selected_prospect_count: number;

  apollo_list_id: string | null;
  apollo_sequence_id: string | null;
  apollo_contact_ids: string[];
  contacts_created: number;
  contacts_enrolled: number;
  activated: boolean;

  logs: string[];
  errors: string[];
  build_report: BuildReport;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/backend${path}`, init);

  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new ApiError(res.status, detail || `Request to ${path} failed with ${res.status}`);
  }

  return res.json() as Promise<T>;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function previewCampaign(prompt: string, desiredProspectCount = 25): Promise<Campaign> {
  return post<Campaign>("/campaign/preview", {
    prompt,
    desired_prospect_count: desiredProspectCount,
  });
}

export function searchProspects(campaignId: string): Promise<Campaign> {
  return post<Campaign>("/campaign/search", { campaign_id: campaignId });
}

export function buildCampaign(campaignId: string, autoLaunch = false): Promise<Campaign> {
  const query = autoLaunch ? "?auto_launch=true" : "";
  return post<Campaign>(`/campaign/build${query}`, { campaign_id: campaignId });
}

/**
 * Lifecycle actions: each loads the Campaign, performs the Apollo action,
 * and only updates stored state after Apollo confirms success -- a failed
 * call leaves the Campaign completely unchanged server-side. Idempotent:
 * calling `activate` on an already-active campaign (or `pause` on an
 * already-paused one) is a no-op that makes no Apollo call at all.
 */
export function markCampaignReady(campaignId: string): Promise<Campaign> {
  return post<Campaign>(`/campaign/${campaignId}/ready`, {});
}

export function activateCampaign(campaignId: string): Promise<Campaign> {
  return post<Campaign>(`/campaign/${campaignId}/activate`, {});
}

export function pauseCampaign(campaignId: string): Promise<Campaign> {
  return post<Campaign>(`/campaign/${campaignId}/pause`, {});
}

/**
 * Campaign Manager reads -- both load an already-stored Campaign by id (or
 * every stored Campaign) and never trigger plan generation, ranking, or an
 * Apollo search. Same read-only guarantee as GET /campaign/{id} already had
 * for Builder's polling use.
 */
export function listCampaigns(includeArchived = false): Promise<Campaign[]> {
  const query = includeArchived ? "?include_archived=true" : "";
  return request<Campaign[]>(`/campaign${query}`);
}

export function getCampaign(campaignId: string): Promise<Campaign> {
  return request<Campaign>(`/campaign/${campaignId}`);
}

/**
 * The result of one campaign sync run -- for visibility/debugging, not
 * stored anywhere. See app/models/campaign_sync.py.
 */
export interface CampaignSyncReport {
  found: number;
  created: number;
  updated: number;
  archived: number;
  unchanged: number;
  duration_ms: number;
}

/**
 * Discovers new Apollo sequences (each becomes a Campaign), refreshes
 * already-synced ones, and archives any that disappeared from Apollo.
 * Never automatic/scheduled -- the campaigns list page triggers this on
 * mount, non-blocking, same pattern as every other "Sync now" in this app.
 */
export function syncCampaigns(): Promise<CampaignSyncReport> {
  return post<CampaignSyncReport>("/sync/campaigns", {});
}

/**
 * Leads are read-only from this client's perspective -- there is no
 * create/edit/status-change call yet. A Lead only ever comes into
 * existence server-side, inside CampaignService.build(), once an Apollo
 * contact has been confirmed created.
 *
 * claude_score/claude_reason are NOT on Lead -- they describe how well
 * Claude thought this person fit ONE specific campaign, so they live on
 * the campaign-membership shapes below instead (LeadCampaignMembership,
 * CampaignLeadView). The same Lead in two campaigns can carry two
 * different scores; Lead itself only ever holds global, person-level data.
 */
export type LeadStatus = "new";
export type CampaignLeadStatus = "added";

export interface Lead {
  lead_id: string;
  workspace_id: string | null;
  apollo_contact_id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  title: string | null;
  company: string | null;
  company_domain: string | null;
  status: LeadStatus;
  created_at: string;
  updated_at: string;
  apollo_snapshot: Record<string, unknown>;
}

export interface LeadListItem extends Lead {
  campaign_count: number;
}

export interface LeadCampaignMembership {
  campaign_id: string;
  campaign_name: string;
  campaign_status: CampaignStatus;
  status: CampaignLeadStatus;
  added_at: string;
  claude_score: number | null;
  claude_reason: string | null;
}

export interface LeadDetail extends Lead {
  campaigns: LeadCampaignMembership[];
}

/** One row of GET /campaign/{id}/leads -- a lead's global fields plus this campaign's own status/score/reason. */
export interface CampaignLeadView {
  lead_id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  title: string | null;
  company: string | null;
  lead_status: LeadStatus;
  campaign_status: CampaignLeadStatus;
  claude_score: number | null;
  claude_reason: string | null;
  added_at: string;
}

export function listLeads(): Promise<LeadListItem[]> {
  return request<LeadListItem[]>("/leads");
}

export function getLead(leadId: string): Promise<LeadDetail> {
  return request<LeadDetail>(`/leads/${leadId}`);
}

export function listCampaignLeads(campaignId: string): Promise<CampaignLeadView[]> {
  return request<CampaignLeadView[]>(`/campaign/${campaignId}/leads`);
}

/**
 * EmailSequence -- Phase 1 only (no EmailMessage yet). Two distinct kinds
 * of data, kept visually and structurally separate in the UI:
 *   - `steps` (subject/day/body) are OUR deployed-configuration snapshot,
 *     captured once from CampaignPlan at first-sync time -- not live Apollo
 *     state.
 *   - `status`/`status_reason`/`unique_*` fields ARE synced Apollo state,
 *     only ever refreshed by an explicit call to `syncCampaignSequence`,
 *     always shown alongside `last_synced_at` -- never real-time.
 */
export type EmailSequenceStatus = "active" | "paused" | "archived";

export interface EmailSequenceStep {
  email_sequence_step_id: string;
  email_sequence_id: string;
  apollo_step_id: string | null;
  position: number;
  day: number;
  subject: string;
  body: string;
}

export interface EmailSequence {
  email_sequence_id: string;
  workspace_id: string | null;
  campaign_id: string;
  apollo_sequence_id: string;
  name: string;
  status: EmailSequenceStatus;
  status_reason: string | null;
  created_at: string;
  updated_at: string;
  last_synced_at: string | null;
  unique_scheduled: number;
  unique_delivered: number;
  unique_opened: number;
  unique_clicked: number;
  unique_replied: number;
  unique_bounced: number;
  unique_unsubscribed: number;
}

export interface EmailSequenceWithSteps extends EmailSequence {
  steps: EmailSequenceStep[];
}

/** Read-only -- never calls Apollo. 404s if this campaign's sequence has never been synced. */
export function getCampaignSequence(campaignId: string): Promise<EmailSequenceWithSteps> {
  return request<EmailSequenceWithSteps>(`/campaign/${campaignId}/sequence`);
}

/** Explicit, manual sync against Apollo -- never triggered automatically. */
export function syncCampaignSequence(campaignId: string): Promise<EmailSequenceWithSteps> {
  return post<EmailSequenceWithSteps>(`/campaign/${campaignId}/sequence/sync`, {});
}

/**
 * EmailMessage/EmailMessageEvent -- the final link in
 * Campaign -> EmailSequence -> EmailMessage -> Lead.
 *
 * `source` is NOT an Apollo field -- it's ours, and it's the single most
 * important field on both types for this UI: every message/event is
 * either a real, synced Apollo record ("apollo_sync") or a locally
 * fabricated demo record ("test_fixture"), and the UI must always render
 * that distinction rather than ever presenting a fixture as live data.
 *
 * `status`/`event_type` are raw strings, not closed unions -- Apollo has
 * only ever been observed returning a handful of values, but this client
 * must not fail or silently coerce a value it hasn't seen before.
 */
export type EmailMessageSource = "apollo_sync" | "test_fixture";

export interface EmailMessage {
  email_message_id: string;
  apollo_message_id: string | null;
  email_sequence_id: string;
  email_sequence_step_id: string | null;
  apollo_touch_id: string | null;
  lead_id: string;

  status: string;
  failure_reason: string | null;
  bounce: boolean;
  spam_blocked: boolean;

  replied: boolean;
  reply_class: string | null;

  provider_message_id: string | null;
  provider_thread_id: string | null;

  created_at: string | null;
  due_at: string | null;
  completed_at: string | null;
  failed_at: string | null;

  source: EmailMessageSource;
  last_synced_at: string | null;
}

/** open_count/click_count are computed server-side from this message's EmailMessageEvent rows -- never stored fields. */
export interface EmailMessageWithEventCounts extends EmailMessage {
  open_count: number;
  click_count: number;
}

export interface EmailMessageEvent {
  email_message_event_id: string;
  email_message_id: string;
  apollo_event_id: string | null;
  event_type: string;
  occurred_at: string;
  apollo_contact_id: string | null;
  readable_user_agent: string | null;
  region: string | null;
  country: string | null;
  source: EmailMessageSource;
}

/** Read-only -- never calls Apollo. Empty array before the first sync/fixture generation. */
export function listCampaignMessages(campaignId: string): Promise<EmailMessageWithEventCounts[]> {
  return request<EmailMessageWithEventCounts[]>(`/campaign/${campaignId}/messages`);
}

/** Explicit, manual sync against Apollo's /emailer_messages/search -- pages through everything, upserts by apollo_message_id. */
export function syncCampaignMessages(campaignId: string): Promise<EmailMessageWithEventCounts[]> {
  return post<EmailMessageWithEventCounts[]>(`/campaign/${campaignId}/messages/sync`, {});
}

/** Makes ZERO Apollo calls -- generates clearly-labeled local test-fixture messages. Idempotent. */
export function generateCampaignMessageFixtures(campaignId: string): Promise<EmailMessageWithEventCounts[]> {
  return post<EmailMessageWithEventCounts[]>(`/campaign/${campaignId}/messages/fixtures`, {});
}

/** Read-only -- this message's currently stored open/click events. */
export function listMessageEvents(campaignId: string, messageId: string): Promise<EmailMessageEvent[]> {
  return request<EmailMessageEvent[]>(`/campaign/${campaignId}/messages/${messageId}/events`);
}

/** Explicit, manual sync of ONE message's /activities. 400s if the message is a test fixture. */
export function syncMessageEvents(campaignId: string, messageId: string): Promise<EmailMessageEvent[]> {
  return post<EmailMessageEvent[]>(`/campaign/${campaignId}/messages/${messageId}/sync-events`, {});
}

/* ------------------------------------------------------------------ */
/* CRM -- a standalone area, no relationship to Campaign/Lead/EmailSequence */
/* ------------------------------------------------------------------ */

export interface CrmContact {
  crm_contact_id: string;
  created_at: string;
  updated_at: string;
  archived: boolean;

  apollo_contact_id: string | null;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  email_status: string | null;
  phone: string | null;
  linkedin_url: string | null;
  title: string | null;
  company: string | null;
  company_website: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  industry: string | null;
  company_size: string | null;
  revenue: string | null;
  funding_stage: string | null;
  funding_amount: string | null;
  technologies: string[];
  seniority: string | null;
  department: string | null;
  job_function: string | null;
  source_snapshot: Record<string, unknown>;

  thesis_cities: string | null;
  thesis_investor_mode: string | null;
  thesis_investor_mode_manual_override: boolean;

  thesis_private_asset_types: string[];
  thesis_private_asset_types_other: string | null;
  thesis_private_business_models: string[];
  thesis_private_business_models_other: string | null;
  thesis_private_industries: string[];
  thesis_private_industries_other: string | null;
  thesis_private_check_sizes: string[];
  thesis_private_check_sizes_other: string | null;
  thesis_private_deal_stages: string[];
  thesis_private_deal_stages_other: string | null;
  thesis_private_meeting_preferences: string[];
  thesis_private_meeting_preferences_other: string | null;
  thesis_private_demographic_preferences: string[];
  thesis_private_demographic_preferences_other: string | null;
  thesis_private_other_criteria: string | null;

  thesis_also_invests_institutionally: boolean | null;

  thesis_institutional_asset_types: string[];
  thesis_institutional_asset_types_other: string | null;
  thesis_institutional_business_models: string[];
  thesis_institutional_business_models_other: string | null;
  thesis_institutional_industries: string[];
  thesis_institutional_industries_other: string | null;
  thesis_institutional_check_sizes: string[];
  thesis_institutional_check_sizes_other: string | null;
  thesis_institutional_deal_stages: string[];
  thesis_institutional_deal_stages_other: string | null;
  thesis_institutional_meeting_preferences: string[];
  thesis_institutional_meeting_preferences_other: string | null;
  thesis_institutional_demographic_preferences: string[];
  thesis_institutional_demographic_preferences_other: string | null;
  thesis_institutional_other_criteria: string | null;

  thesis_dietary_preferences: string | null;
  thesis_referral_emails: string | null;

  custom_fields: Record<string, unknown>;
}

export type CustomFieldType = "text" | "long_text" | "number" | "date" | "boolean" | "single_select" | "multi_select";

export interface CrmCustomFieldDefinition {
  crm_custom_field_id: string;
  field_key: string;
  label: string;
  description: string | null;
  field_type: CustomFieldType;
  options: string[];
  required: boolean;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export type CrmImportRowStatus = "new" | "existing" | "possible_duplicate" | "error";

export interface CrmImportRowPreview {
  row_index: number;
  mapped_fields: Record<string, unknown>;
  status: CrmImportRowStatus;
  matched_contact_id: string | null;
  matched_on: string | null;
  error: string | null;
}

export type CrmImportBatchStatus = "uploaded" | "mapped" | "committed";

export interface CrmImportBatch {
  import_batch_id: string;
  filename: string;
  uploaded_at: string;
  status: CrmImportBatchStatus;
  headers: string[];
  rows: Record<string, string>[];
  row_count: number;
  suggested_mapping: Record<string, string>;
  column_mapping: Record<string, string> | null;
  preview: CrmImportRowPreview[] | null;
  new_count: number | null;
  existing_count: number | null;
  possible_duplicate_count: number | null;
  error_count: number | null;
}

export interface CrmImportReport {
  created: number;
  updated: number;
  skipped: number;
  errors: number;
}

export interface CrmContactFilters {
  q?: string;
  city?: string;
  state?: string;
  country?: string;
  company?: string;
  industry?: string;
  deal_stage?: string;
  check_size?: string;
  investor_mode?: string;
  email_status?: string;
  include_archived?: boolean;
  page?: number;
  page_size?: number;
}

export interface CrmContactPage {
  items: CrmContact[];
  total: number;
  page: number;
  page_size: number;
}

export function listCrmContacts(filters: CrmContactFilters = {}): Promise<CrmContactPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return request<CrmContactPage>(`/crm/contacts${query ? `?${query}` : ""}`);
}

export function getCrmContact(crmContactId: string): Promise<CrmContact> {
  return request<CrmContact>(`/crm/contacts/${crmContactId}`);
}

export function createCrmContact(fields: Record<string, unknown>): Promise<CrmContact> {
  return post<CrmContact>("/crm/contacts", fields);
}

export function updateCrmContact(crmContactId: string, patch: Record<string, unknown>): Promise<CrmContact> {
  return request<CrmContact>(`/crm/contacts/${crmContactId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function archiveCrmContact(crmContactId: string): Promise<CrmContact> {
  return request<CrmContact>(`/crm/contacts/${crmContactId}`, { method: "DELETE" });
}

export function listCrmCustomFields(includeInactive = true): Promise<CrmCustomFieldDefinition[]> {
  return request<CrmCustomFieldDefinition[]>(`/crm/custom-fields?include_inactive=${includeInactive}`);
}

export function createCrmCustomField(payload: {
  field_key: string;
  label: string;
  field_type: CustomFieldType;
  description?: string;
  options?: string[];
  required?: boolean;
}): Promise<CrmCustomFieldDefinition> {
  return post<CrmCustomFieldDefinition>("/crm/custom-fields", payload);
}

export function updateCrmCustomField(
  crmCustomFieldId: string,
  patch: Record<string, unknown>
): Promise<CrmCustomFieldDefinition> {
  return request<CrmCustomFieldDefinition>(`/crm/custom-fields/${crmCustomFieldId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function uploadCrmImport(file: File): Promise<CrmImportBatch> {
  const formData = new FormData();
  formData.append("file", file);
  return request<CrmImportBatch>("/crm/import/upload", { method: "POST", body: formData });
}

export function getCrmImportBatch(importBatchId: string): Promise<CrmImportBatch> {
  return request<CrmImportBatch>(`/crm/import/${importBatchId}`);
}

export function previewCrmImport(importBatchId: string, columnMapping: Record<string, string>): Promise<CrmImportBatch> {
  return post<CrmImportBatch>(`/crm/import/${importBatchId}/preview`, { column_mapping: columnMapping });
}

export function commitCrmImport(importBatchId: string, decisions: Record<string, string>): Promise<CrmImportReport> {
  return post<CrmImportReport>(`/crm/import/${importBatchId}/commit`, { decisions });
}

export function exportCrmBackup(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/crm/backup/export");
}
