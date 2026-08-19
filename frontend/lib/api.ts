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

  thesis_dietary_preferences: string[];
  thesis_dietary_preferences_other: string | null;
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

export type CrmContactExportFieldKind = "scalar" | "list" | "boolean";

export interface CrmContactExportField {
  key: string;
  kind: CrmContactExportFieldKind;
}

// Core/thesis field list for the CSV export, computed server-side straight from the
// CrmContact model -- never hand-maintained here, so a new backend field is picked up
// automatically. Paired with listCrmCustomFields() for the custom-field columns.
export function listCrmContactExportFields(): Promise<CrmContactExportField[]> {
  return request<CrmContactExportField[]>("/crm/contacts/export-fields");
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

// --- More Filters: dynamic field registry + query engine ---
//
// FilterFieldMeta is the ONLY source of truth for the filter builder's field
// list, operator choices, and select options -- there is no second, hardcoded
// field list anywhere in the frontend. A field added to the backend registry
// (core/thesis fields in crm_filter_service.py, or a new active custom field
// definition) shows up here automatically, with zero frontend changes.

export type FilterFieldType = "text" | "number" | "boolean" | "date" | "single_select" | "multi_select";

export interface FilterFieldMeta {
  key: string;
  label: string;
  category: string;
  type: FilterFieldType;
  storage_shape: "scalar" | "list";
  source: "core" | "thesis" | "custom";
  options: string[];
  ordered: boolean;
  ordered_options: string[];
  non_ordered_options: string[];
  operators: string[];
}

export interface FilterCondition {
  field: string;
  operator: string;
  value?: unknown;
}

export interface FilterSort {
  field: string;
  direction: "asc" | "desc";
}

export interface FilterQuery {
  filters: FilterCondition[];
  logic: "AND" | "OR";
  include_archived?: boolean;
  page?: number;
  page_size?: number;
  sort?: FilterSort | null;
}

export function listCrmFilterableFields(): Promise<FilterFieldMeta[]> {
  return request<FilterFieldMeta[]>("/crm/filterable-fields");
}

export function queryCrmContacts(query: FilterQuery): Promise<CrmContactPage> {
  return post<CrmContactPage>("/crm/contacts/query", query);
}

// --- Astro Core (Phase 1/1.1) -- deterministic, Claude-free CRM search/count ---
//
// `context` is the ENTIRE conversation state: just the last resolved
// FilterQuery + intent. There is no session id and nothing is persisted
// server-side -- the caller holds this and resends it each turn. See
// app/models/astro.py and app/services/astro_parser.py's Phase 1.1 module
// docstring for why this is deliberately the smallest reliable design.

export interface AstroCommandContext {
  query: FilterQuery | null;
  intent: "search_contacts" | "count_contacts" | null;
}

export interface AstroCommandRequest {
  text: string;
  context?: AstroCommandContext;
}

export interface AstroCommandResponse {
  intent: "search_contacts" | "count_contacts" | "unresolved";
  understood_as: string;

  query: FilterQuery | null;
  total: number | null;
  contacts: CrmContact[] | null;

  // Set only for a resolved Phase 1.1 refinement turn -- never set for a
  // standalone Phase 1 command (e.g. the very first turn of a conversation).
  operation: "add" | "replace" | "remove" | "reset" | "change_intent" | null;
  changed_field: string | null;

  // Deterministic -- never Claude-generated. Explains a refinement when
  // `operation` is set; carries the Phase 1 clarification when intent is
  // "unresolved"; null for a plain standalone command.
  message: string | null;

  understood: Record<string, string> | null;
  unresolved_phrase: string | null;
}

export function sendAstroCommand(text: string, context?: AstroCommandContext): Promise<AstroCommandResponse> {
  return post<AstroCommandResponse>("/astro/command", context ? { text, context } : { text });
}

// --- Lists: named, persistent groupings of existing CRM contacts ---
//
// A list never holds contact data of its own -- GET .../contacts always
// returns live CrmContact rows (the same CrmContactPage shape used
// everywhere else), never a stored copy. See app/services/crm_service.py's
// "--- Lists ---" section for the backend side of this contract.

export interface CrmContactList {
  list_id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface CrmContactListSummary extends CrmContactList {
  contact_count: number;
}

export interface CrmListBulkAddResult {
  added: number;
  already_member: number;
  not_found: number;
}

export interface CrmListBulkRemoveResult {
  removed: number;
}

export function listCrmLists(): Promise<CrmContactListSummary[]> {
  return request<CrmContactListSummary[]>("/crm/lists");
}

export function createCrmList(payload: { name: string; description?: string }): Promise<CrmContactListSummary> {
  return post<CrmContactListSummary>("/crm/lists", payload);
}

export function getCrmList(listId: string): Promise<CrmContactListSummary> {
  return request<CrmContactListSummary>(`/crm/lists/${listId}`);
}

export function updateCrmList(listId: string, patch: { name?: string; description?: string }): Promise<CrmContactListSummary> {
  return request<CrmContactListSummary>(`/crm/lists/${listId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

// Returns the summary as it was immediately before deletion -- the list
// itself is gone (permanently -- see the backend docstring for why that's
// safe here), but its own contacts are never touched.
export function deleteCrmList(listId: string): Promise<CrmContactListSummary> {
  return request<CrmContactListSummary>(`/crm/lists/${listId}`, { method: "DELETE" });
}

export function listCrmListContacts(
  listId: string,
  params: { page?: number; page_size?: number } = {}
): Promise<CrmContactPage> {
  const query = new URLSearchParams();
  if (params.page !== undefined) query.set("page", String(params.page));
  if (params.page_size !== undefined) query.set("page_size", String(params.page_size));
  const qs = query.toString();
  return request<CrmContactPage>(`/crm/lists/${listId}/contacts${qs ? `?${qs}` : ""}`);
}

// The ONE call made regardless of whether 1 contact or every currently
// selected contact (which may already be thousands, from Contacts/More
// Filters/Astro Search's existing "Select all N matching") is being added --
// never one request per contact. Duplicate membership is idempotent, never
// an error -- see CrmListBulkAddResult for how a repeat is reported.
export function bulkAddToCrmList(listId: string, contactIds: string[]): Promise<CrmListBulkAddResult> {
  return post<CrmListBulkAddResult>(`/crm/lists/${listId}/contacts/bulk-add`, { contact_ids: contactIds });
}

export function bulkRemoveFromCrmList(listId: string, contactIds: string[]): Promise<CrmListBulkRemoveResult> {
  return post<CrmListBulkRemoveResult>(`/crm/lists/${listId}/contacts/bulk-remove`, { contact_ids: contactIds });
}

// Returns the list's updated summary (contact_count reflects the removal).
export function removeCrmListContact(listId: string, crmContactId: string): Promise<CrmContactListSummary> {
  return request<CrmContactListSummary>(`/crm/lists/${listId}/contacts/${crmContactId}`, { method: "DELETE" });
}

// --- Activity Log ---

export type ActivityCategory =
  | "itf"
  | "contacts"
  | "imports"
  | "lists"
  | "exports"
  | "campaigns"
  | "email_intake"
  | "errors";

export type ActivitySource =
  | "manual_crm"
  | "itf_automation"
  | "csv_import"
  | "lists"
  | "contacts_page"
  | "more_filters"
  | "astro_search"
  | "campaign_system"
  | "email_intake"
  | "system";

export interface ActivityEvent {
  event_id: string;
  event_type: string;
  category: ActivityCategory;
  created_at: string;
  source: ActivitySource;
  actor: string | null;
  entity_type: string | null;
  entity_id: string | null;
  entity_name: string | null;
  summary: string;
  metadata: Record<string, unknown>;
}

export interface ActivityEventPage {
  items: ActivityEvent[];
  total: number;
  page: number;
  page_size: number;
}

export interface ActivityEventFilters {
  category?: ActivityCategory;
  q?: string;
  date_from?: string;
  date_to?: string;
  page?: number;
  page_size?: number;
}

export function listActivityEvents(filters: ActivityEventFilters = {}): Promise<ActivityEventPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return request<ActivityEventPage>(`/crm/activity${query ? `?${query}` : ""}`);
}

// The one deliberate exception to "events only come from a backend write path" --
// CSV export is 100% client-side (see lib/csv-export.ts), so the frontend calls
// this itself, AFTER the download has already been triggered. Best-effort: a
// failure here must never surface as an export error, since the file is
// already on the user's machine by the time this call is made -- callers
// should fire this and ignore/swallow any rejection (see logCrmExportSafely
// call sites in app/crm/*), never block or roll back on it.
export type CrmExportSource = "contacts" | "more_filters" | "astro_search" | "list";

export function logCrmExport(payload: {
  source: CrmExportSource;
  contact_count: number;
  format?: string;
  list_id?: string;
  list_name?: string;
}): Promise<{ status: string }> {
  return post<{ status: string }>("/crm/activity/exports", { format: "csv", ...payload });
}

// Fire-and-forget wrapper for logCrmExport -- swallows any failure so a
// logging hiccup can never be mistaken for (or interfere with) the export
// itself, which has already completed by the time this is called.
export function logCrmExportSafely(payload: {
  source: CrmExportSource;
  contact_count: number;
  format?: string;
  list_id?: string;
  list_name?: string;
}): void {
  logCrmExport(payload).catch(() => {
    // Intentionally ignored -- see this module's docstring above.
  });
}

// --- Email Intake (Phase 1 -- review/approval only, no live Gmail) ---

export type EmailIntakeStatus = "pending_review" | "needs_match" | "approved" | "rejected" | "error";

export type EmailFieldChangeOperation = "set" | "union_add" | "append";

export interface EmailAttachmentMeta {
  filename: string;
  content_type: string | null;
  size_bytes: number | null;
}

export interface EmailCrmFieldChange {
  field_key: string;
  field_label: string;
  operation: EmailFieldChangeOperation;
  current_value: unknown;
  proposed_value: unknown;
  source_text: string | null;
}

export interface EmailIntakeItem {
  intake_id: string;
  gmail_message_id: string;
  gmail_thread_id: string | null;
  received_at: string;
  sender: string;
  recipients: string[];
  subject: string;
  body_text: string;
  attachments: EmailAttachmentMeta[];
  status: EmailIntakeStatus;
  matched_contact_id: string | null;
  matched_contact_name: string | null;
  matched_on: string | null;
  proposal: EmailCrmFieldChange[];
  error_message: string | null;
  created_at: string;
  reviewed_at: string | null;
}

export interface EmailIntakeItemPage {
  items: EmailIntakeItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface EmailIntakeFilters {
  status?: EmailIntakeStatus;
  q?: string;
  page?: number;
  page_size?: number;
}

export function listEmailIntakeItems(filters: EmailIntakeFilters = {}): Promise<EmailIntakeItemPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  return request<EmailIntakeItemPage>(`/crm/email-intake${query ? `?${query}` : ""}`);
}

export function getEmailIntakeItem(intakeId: string): Promise<EmailIntakeItem> {
  return request<EmailIntakeItem>(`/crm/email-intake/${intakeId}`);
}

// Reviewer-selected CRM contact for a NEEDS_MATCH item -- generates the
// proposal against the chosen contact and moves it to PENDING_REVIEW.
// Never creates a new contact.
export function matchEmailIntakeItem(intakeId: string, crmContactId: string): Promise<EmailIntakeItem> {
  return post<EmailIntakeItem>(`/crm/email-intake/${intakeId}/match`, { crm_contact_id: crmContactId });
}

export interface StaleFieldConflict {
  field_key: string;
  field_label: string;
  reviewed_value: unknown;
  live_value: unknown;
  proposed_value: unknown;
}

export interface ApproveEmailIntakeResult {
  status: "approved" | "stale";
  item: EmailIntakeItem;
  conflicts: StaleFieldConflict[];
}

// Applies ONLY the checked field_keys. If any of them drifted from what
// this proposal originally reviewed, nothing is written -- result.status
// is "stale" and result.conflicts lists exactly what changed; result.item
// already reflects the refreshed current values, ready to re-render.
export function approveEmailIntakeItem(intakeId: string, fieldKeys: string[]): Promise<ApproveEmailIntakeResult> {
  return post<ApproveEmailIntakeResult>(`/crm/email-intake/${intakeId}/approve`, { field_keys: fieldKeys });
}

export function rejectEmailIntakeItem(intakeId: string): Promise<EmailIntakeItem> {
  return post<EmailIntakeItem>(`/crm/email-intake/${intakeId}/reject`, {});
}

// --- Campaign Manager Integration Phase: read-side aggregation only -------
//
// UnifiedCampaignSummary is a presentation DTO, not a third campaign model --
// see app/api/campaign_manager.py. It never replaces Campaign or MailCampaign
// above; the dashboard still fetches richer per-provider data at the detail
// route each card links to. `status_bucket` is a small shared vocabulary for
// this endpoint's own consumers -- the dashboard itself renders each item's
// EXISTING per-provider status badge from `raw_status`, not from this bucket.

export type SendingMethod = "apollo" | "astronomic_mail";

export type CampaignStatusBucket = "draft" | "in_progress" | "ready" | "active" | "paused" | "failed" | "archived";

export interface UnifiedCampaignSummary {
  id: string;
  sending_method: SendingMethod;
  name: string;
  status_bucket: CampaignStatusBucket;
  raw_status: string;
  summary: string;
  created_at: string;
  detail_path: string;
}

export function listUnifiedCampaigns(): Promise<UnifiedCampaignSummary[]> {
  return request<UnifiedCampaignSummary[]>("/campaign-manager/campaigns");
}

// --- Astronomic Mail (Phase 1 -- Foundation, NO sending capability) -------
//
// Deliberately independent from Campaign/CampaignStatus above (the Apollo-
// oriented system) -- MailCampaign/MailCampaignStatus are their own types.
// MailCampaignStatus intentionally has only 3 values in this phase; there
// is no "active"/"paused"/"completed" to represent because nothing here
// can send. See app/models/mail.py for the full rationale.

export type MailCampaignStatus = "draft" | "ready" | "archived";

// Campaign Manager Integration Phase -- who can see/edit this campaign.
// This app has no multi-user/workspace permission system anywhere (no
// owner/user field on MailCampaign or any other model), so this is a
// stored PREFERENCE only -- nothing enforces it. See app/models/mail.py's
// MailCampaignSharing docstring.
export type MailCampaignSharing = "everyone" | "only_me";

export interface MailCampaign {
  mail_campaign_id: string;
  name: string;
  status: MailCampaignStatus;
  source_list_id: string | null;
  sending_days: number[]; // 0=Monday .. 6=Sunday
  start_time: string | null; // "HH:MM:SS"
  end_time: string | null;
  timezone: string | null;
  all_hours: boolean;
  sharing: MailCampaignSharing;
  start_immediately: boolean;
  daily_lead_start_limit: number | null;
  created_at: string;
  updated_at: string;
  ready_at: string | null;
  archived_at: string | null;
}

// Everything but `name` is optional campaign-level configuration from the
// Create Campaign modal -- omitted fields are simply not sent, so the
// backend creates the campaign exactly as it always has if none are given.
export interface CreateMailCampaignOptions {
  sharing?: MailCampaignSharing;
  sending_days?: number[];
  start_time?: string; // "HH:MM"
  end_time?: string;
  timezone?: string;
  all_hours?: boolean;
  start_immediately?: boolean;
  daily_lead_start_limit?: number | null;
}

export interface MailSequenceStep {
  step_id: string;
  mail_campaign_id: string;
  step_number: number;
  subject: string;
  body: string;
  delay_days: number;
  reply_in_thread: boolean;
  created_at: string;
  updated_at: string;
}

// The only {{variable}} placeholders add_step/update_step will accept --
// anything else is rejected with a 400. Kept here so the composer UI can
// show the same whitelist it will actually be validated against.
export const MAIL_TEMPLATE_VARIABLES = ["first_name", "last_name", "company"] as const;

export type MailEnrollmentStatus = "pending" | "suppressed";

export interface MailEnrollment {
  enrollment_id: string;
  mail_campaign_id: string;
  crm_contact_id: string;
  email_at_enrollment: string;
  status: MailEnrollmentStatus;
  enrolled_at: string;
  created_at: string;
}

// Pure, read-only -- calling this never enrolls anyone, queues anything, or
// changes any contact/list data. See MailCampaignService.get_review().
export interface MailCampaignReview {
  mail_campaign_id: string;
  source_list_id: string | null;
  source_list_name: string | null;
  source_list_exists: boolean;
  total_contacts: number;
  contacts_missing_email: number;
  contacts_suppressed: number;
  contacts_eligible: number;
  sequence_step_count: number;
  theoretical_total_sends: number;
  daily_capacity_estimate: number | null;
  daily_capacity_note: string;
}

export function listMailCampaigns(): Promise<MailCampaign[]> {
  return request<MailCampaign[]>("/mail/campaigns");
}

export function createMailCampaign(name: string, options: CreateMailCampaignOptions = {}): Promise<MailCampaign> {
  return post<MailCampaign>("/mail/campaigns", { name, ...options });
}

export function getMailCampaign(mailCampaignId: string): Promise<MailCampaign> {
  return request<MailCampaign>(`/mail/campaigns/${mailCampaignId}`);
}

export function updateMailCampaign(mailCampaignId: string, patch: Record<string, unknown>): Promise<MailCampaign> {
  return request<MailCampaign>(`/mail/campaigns/${mailCampaignId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

/** DRAFT -> READY. Validates completeness and snapshots the current audience
 * into MailEnrollment rows -- see the backend docstring for exactly when/why. */
export function markMailCampaignReady(mailCampaignId: string): Promise<MailCampaign> {
  return post<MailCampaign>(`/mail/campaigns/${mailCampaignId}/ready`, {});
}

/** READY -> DRAFT. Deletes the (now-stale) enrollment snapshot so editing can resume. */
export function unlockMailCampaign(mailCampaignId: string): Promise<MailCampaign> {
  return post<MailCampaign>(`/mail/campaigns/${mailCampaignId}/unlock`, {});
}

export function archiveMailCampaign(mailCampaignId: string): Promise<MailCampaign> {
  return post<MailCampaign>(`/mail/campaigns/${mailCampaignId}/archive`, {});
}

export function getMailCampaignReview(mailCampaignId: string): Promise<MailCampaignReview> {
  return request<MailCampaignReview>(`/mail/campaigns/${mailCampaignId}/review`);
}

export function listMailEnrollments(mailCampaignId: string): Promise<MailEnrollment[]> {
  return request<MailEnrollment[]>(`/mail/campaigns/${mailCampaignId}/enrollments`);
}

export function listMailSequenceSteps(mailCampaignId: string): Promise<MailSequenceStep[]> {
  return request<MailSequenceStep[]>(`/mail/campaigns/${mailCampaignId}/steps`);
}

export function addMailSequenceStep(
  mailCampaignId: string,
  payload: { subject: string; body: string; delay_days?: number; reply_in_thread?: boolean }
): Promise<MailSequenceStep> {
  return post<MailSequenceStep>(`/mail/campaigns/${mailCampaignId}/steps`, payload);
}

export function updateMailSequenceStep(
  mailCampaignId: string,
  stepId: string,
  patch: Record<string, unknown>
): Promise<MailSequenceStep> {
  return request<MailSequenceStep>(`/mail/campaigns/${mailCampaignId}/steps/${stepId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteMailSequenceStep(mailCampaignId: string, stepId: string): Promise<MailSequenceStep[]> {
  return request<MailSequenceStep[]>(`/mail/campaigns/${mailCampaignId}/steps/${stepId}`, { method: "DELETE" });
}

export function reorderMailSequenceSteps(mailCampaignId: string, stepIds: string[]): Promise<MailSequenceStep[]> {
  return post<MailSequenceStep[]>(`/mail/campaigns/${mailCampaignId}/steps/reorder`, { step_ids: stepIds });
}

// --- Mail Suppression -- keyed by normalized email, independent of ---
// --- CrmContact.email_status. See app/models/mail.py for why. --------

export type MailSuppressionReason = "manual" | "unsubscribed" | "hard_bounce" | "complaint";

export interface MailSuppression {
  email_normalized: string;
  reason: MailSuppressionReason;
  notes: string | null;
  created_at: string;
  updated_at: string;
  active: boolean;
  unsuppressed_at: string | null;
}

export interface MailContactSuppressionStatus {
  email_normalized: string;
  suppressed: boolean;
  reason: MailSuppressionReason | null;
  notes: string | null;
  created_at: string | null;
  unsuppressed_at: string | null;
}

export function suppressMailEmail(
  email: string,
  reason: MailSuppressionReason = "manual",
  notes?: string
): Promise<MailSuppression> {
  return post<MailSuppression>("/mail/suppressions", { email, reason, notes });
}

export function unsuppressMailEmail(email: string): Promise<MailSuppression> {
  return post<MailSuppression>("/mail/suppressions/unsuppress", { email });
}

/** Never 404s -- `suppressed: false` for an address that's never been suppressed. */
export function getMailSuppressionStatus(email: string): Promise<MailContactSuppressionStatus> {
  return request<MailContactSuppressionStatus>(`/mail/suppressions/${encodeURIComponent(email)}`);
}

// --- Astronomic Mail Phase 2 -- Google Workspace Mailbox Connection -------
//
// CONNECTION ONLY -- see app/api/mailboxes.py's module docstring. No route
// here can send an email, queue one, or activate a campaign. `Mailbox` is
// the PUBLIC-safe shape returned by every one of these calls -- there is no
// refresh/access token field anywhere in this type, matching the backend's
// own Mailbox/MailboxCredential split (app/models/mailbox.py).

export type MailboxProvider = "google";
export type MailboxStatus = "connected" | "needs_reauth" | "disconnected";

export interface Mailbox {
  mailbox_id: string;
  provider: MailboxProvider;
  email: string;
  display_name: string | null;
  status: MailboxStatus;
  google_user_id: string | null;
  granted_scopes: string[];
  connected_at: string;
  updated_at: string;
  disconnected_at: string | null;
}

export function listMailboxes(): Promise<Mailbox[]> {
  return request<Mailbox[]>("/mailboxes");
}

/**
 * Returns the URL to navigate the browser to -- callers must do a full
 * top-level navigation (`window.location.href = authorize_url`), not treat
 * this like a normal API call whose result renders in place. Google's own
 * redirect back after consent lands on the BACKEND's own
 * /mailboxes/google/callback directly (bypassing this app's /backend/*
 * rewrite proxy entirely, since that's a top-level browser navigation
 * Google itself issues), which then 302s the browser to /manager/emails.
 */
export function startGoogleMailboxConnect(): Promise<{ authorize_url: string }> {
  return request<{ authorize_url: string }>("/mailboxes/google/start");
}

export function disconnectMailbox(mailboxId: string): Promise<Mailbox> {
  return post<Mailbox>(`/mailboxes/${mailboxId}/disconnect`, {});
}
