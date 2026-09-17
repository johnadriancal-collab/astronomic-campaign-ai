import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Campaign Manager Leads V1
// (2026-09-17) -- same source-inspection pattern as mail-inbox.test.ts,
// since this project has no DOM render harness (see package.json's
// test script). Real join/aggregation logic (dedup, status derivation,
// campaign/replied filters, sorting, step history) is unit-tested
// directly against MailLeadsService in
// tests/test_mail_leads_service.py; these tests verify the frontend
// actually wires that data in, and never fabricates anything.

const LIST_PAGE_SOURCE = readFileSync(new URL("../app/manager/leads/page.tsx", import.meta.url), "utf-8");
const DETAIL_PAGE_SOURCE = readFileSync(new URL("../app/manager/leads/[id]/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_leads_service.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

// --- List page ---------------------------------------------------------------

test("the Leads list page fetches real leads via listMailLeads, never the old Apollo listLeads", () => {
  assert.match(LIST_PAGE_SOURCE, /listMailLeads\(/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /\blistLeads\(/); // the separate Apollo function, deliberately not this page's data source anymore
});

test("the Leads list page no longer says the old 'No leads yet' Apollo-era copy unconditionally", () => {
  // Matches visible UI copy only -- not this file's own dev comments,
  // which legitimately explain what Apollo-based content was replaced.
  assert.doesNotMatch(LIST_PAGE_SOURCE, /durable leads once a campaign built their Apollo contact/i);
});

test("the Leads list page sends search, status, campaign, and replied filters to the backend", () => {
  assert.match(LIST_PAGE_SOURCE, /q: search\.trim\(\)/);
  assert.match(LIST_PAGE_SOURCE, /status: statusFilter/);
  assert.match(LIST_PAGE_SOURCE, /campaignId: campaignFilter/);
  assert.match(LIST_PAGE_SOURCE, /replied: repliedFilter/);
});

test("the Leads list page sends sort_by/sort_dir and defaults to last_activity, newest first", () => {
  assert.match(LIST_PAGE_SOURCE, /useState<MailLeadSortBy>\("last_activity"\)/);
  assert.match(LIST_PAGE_SOURCE, /useState<"asc" \| "desc">\("desc"\)/);
  assert.match(LIST_PAGE_SOURCE, /sortBy,\s*\n\s*sortDir,/);
});

test("the Leads list page allows sorting by name and last_campaign too, via clickable headers", () => {
  assert.match(LIST_PAGE_SOURCE, /column="name"/);
  assert.match(LIST_PAGE_SOURCE, /column="last_campaign"/);
  assert.match(LIST_PAGE_SOURCE, /column="last_activity"/);
  assert.match(LIST_PAGE_SOURCE, /function handleSort/);
});

test("the Leads list page is genuinely server-paginated -- page/page_size are sent to the backend, not just used to slice an already-fetched array", () => {
  assert.match(LIST_PAGE_SOURCE, /page,\s*\n\s*pageSize,/);
  // Distinguishing marker vs. the CRM contacts page's "fetch everything,
  // slice client-side" pattern: no page_size:1 probe call, no re-fetch
  // with page_size set to a previously-learned total.
  assert.doesNotMatch(LIST_PAGE_SOURCE, /page_size:\s*1\b/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /page_size:\s*probe/);
});

test("the Leads list page resets to page 1 whenever a filter, search, or sort changes", () => {
  assert.match(LIST_PAGE_SOURCE, /function updateFilter/);
  assert.match(LIST_PAGE_SOURCE, /setPage\(1\)/);
});

test("the Leads list page shows a Replied indicator on the row, independent of the status column", () => {
  assert.match(LIST_PAGE_SOURCE, /lead\.replied\s*&&/);
  assert.match(LIST_PAGE_SOURCE, /MessageSquare/);
});

test("each Leads row is a real navigation (next/link Link) to the dedicated detail page, not a modal", () => {
  assert.match(LIST_PAGE_SOURCE, /href=\{`\/manager\/leads\/\$\{lead\.crm_contact_id\}`\}/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /<Dialog[\s>]/);
});

test("the Leads list page's Last Campaign cell links to the real campaign detail page", () => {
  assert.match(LIST_PAGE_SOURCE, /href=\{`\/manager\/campaigns\/mail\/\$\{lead\.last_campaign_id\}`\}/);
});

test("the Leads list page reuses the app's existing wide-detail-page container convention", () => {
  assert.match(LIST_PAGE_SOURCE, /MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS/);
});

test("the Leads table scrolls horizontally in its own container on narrow screens rather than corrupting the page", () => {
  assert.match(LIST_PAGE_SOURCE, /overflow-x-auto/);
});

test("the Leads list page reuses the real mailEnrollmentStatus label/badge helpers, never an invented status system", () => {
  assert.match(LIST_PAGE_SOURCE, /mailEnrollmentStatusLabel/);
  assert.match(LIST_PAGE_SOURCE, /mailEnrollmentStatusBadgeClass/);
});

// --- Detail page ---------------------------------------------------------------

test("the Lead detail page fetches real data via getMailLead, keyed off the route's crm_contact_id", () => {
  assert.match(DETAIL_PAGE_SOURCE, /getMailLead\(crmContactId\)/);
});

test("the Lead detail page shows the summary fields: campaigns, replies, first campaign, last activity", () => {
  for (const field of ["campaigns_count", "replies_count", "first_campaign_at", "last_activity_at"]) {
    assert.match(DETAIL_PAGE_SOURCE, new RegExp(field));
  }
});

test("the Lead detail page renders multiple campaign history entries, one per enrollment, newest first as returned by the backend", () => {
  assert.match(DETAIL_PAGE_SOURCE, /lead\.campaign_history\.map/);
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /campaign_history\.sort/); // trusts the backend's own newest-first ordering
});

test("the Lead detail page shows real step history per campaign (step number, status, sent_at) without fabricating activity", () => {
  assert.match(DETAIL_PAGE_SOURCE, /entry\.steps\.map/);
  assert.match(DETAIL_PAGE_SOURCE, /step\.step_number/);
  assert.match(DETAIL_PAGE_SOURCE, /step\.status/);
  assert.match(DETAIL_PAGE_SOURCE, /step\.sent_at/);
});

test("the Lead detail page links a reply directly to the Inbox reply detail page, never duplicating body content", () => {
  assert.match(DETAIL_PAGE_SOURCE, /entry\.has_reply && entry\.reply_enrollment_id/);
  assert.match(DETAIL_PAGE_SOURCE, /href=\{`\/manager\/inbox\/\$\{entry\.reply_enrollment_id\}`\}/);
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /getInboxReplyBody/); // no body fetch on this page
});

test("the Lead detail page links back to the real CRM Contacts record using crm_contact_id", () => {
  assert.match(DETAIL_PAGE_SOURCE, /href=\{`\/crm\/\$\{lead\.crm_contact_id\}`\}/);
  assert.match(DETAIL_PAGE_SOURCE, /View in Contacts/);
});

test("the Lead detail page has a Back to Leads link", () => {
  assert.match(DETAIL_PAGE_SOURCE, /Back to Leads/);
  assert.match(DETAIL_PAGE_SOURCE, /href="\/manager\/leads"/);
});

test("the Lead detail page never renders reply reading in a modal", () => {
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /<Dialog[\s>]/);
  assert.doesNotMatch(DETAIL_PAGE_SOURCE, /DialogPopup/);
});

// --- API client ---------------------------------------------------------------

test("MailLeadListItem/MailLeadDetail (api.ts) are distinct types from the legacy Apollo LeadListItem", () => {
  assert.match(API_SOURCE, /export interface MailLeadListItem/);
  assert.match(API_SOURCE, /export interface MailLeadDetail/);
  // The legacy Apollo type must still exist, untouched, for app/api/leads.py's own callers.
  assert.match(API_SOURCE, /export interface LeadListItem/);
});

test("listMailLeads sends page/search/filter/sort as query params to GET /mail/leads", () => {
  assert.match(API_SOURCE, /`\/mail\/leads\?\$\{query\.toString\(\)\}`/);
});

test("getMailLead calls GET /mail/leads/{crm_contact_id}", () => {
  assert.match(API_SOURCE, /`\/mail\/leads\/\$\{crmContactId\}`/);
});

// --- Backend model / route -----------------------------------------------------

test("MailLeadListItem (backend model) has no fabricated fields beyond what's aggregated from real data", () => {
  const modelBlock = MODEL_SOURCE.slice(
    MODEL_SOURCE.indexOf("class MailLeadListItem"),
    MODEL_SOURCE.indexOf("class MailLeadPage")
  );
  assert.doesNotMatch(modelBlock, /unread/i);
  assert.doesNotMatch(modelBlock, /engagement_stage/i); // never overwrites CRM Engagement Stage
});

test("GET /mail/leads and GET /mail/leads/{crm_contact_id} both exist and are read-only", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/leads", response_model=MailLeadPage\)/);
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/leads\/\{crm_contact_id\}", response_model=MailLeadDetail\)/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/leads/);
});

test("MailLeadsService is a pure aggregation over existing stores -- no new persistence, no write methods", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /\.create\(|\.save\(/);
  assert.match(SERVICE_SOURCE, /campaign_store: MailCampaignStore/);
  assert.match(SERVICE_SOURCE, /enrollment_store: MailEnrollmentStore/);
  assert.match(SERVICE_SOURCE, /contact_store: CrmContactStore/);
});

test("only a contact with at least one real enrollment can become a Lead -- get_lead_detail returns None otherwise", () => {
  assert.match(SERVICE_SOURCE, /if not contact_enrollments:\s*\n\s*return None/);
});

test("campaign filter matches ANY of a lead's campaigns, not only their most recent one", () => {
  const filterBlock = SERVICE_SOURCE.slice(SERVICE_SOURCE.indexOf("if campaign_id:"), SERVICE_SOURCE.indexOf("if replied is not None:"));
  assert.match(filterBlock, /any\(campaign\.mail_campaign_id == campaign_id/);
});

test("last_activity_at is computed only from real fields (step sent_at, enrollment replied_at/enrolled_at) -- never a fabricated timestamp", () => {
  const methodBlock = SERVICE_SOURCE.slice(SERVICE_SOURCE.indexOf("def _last_activity"), SERVICE_SOURCE.indexOf("def _contact_name"));
  assert.match(methodBlock, /replied_at/);
  assert.match(methodBlock, /sent_at/);
  assert.match(methodBlock, /enrolled_at/);
  assert.doesNotMatch(methodBlock, /datetime\.now\(\)/); // never "now" as a fallback -- only real recorded timestamps
});
