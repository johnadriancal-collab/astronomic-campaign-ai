import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Campaign Manager Campaigns V1
// (2026-09-17) -- same source-inspection pattern as mail-leads.test.ts,
// since this project has no DOM render harness (see package.json's test
// script). Real aggregation logic (workload counts, progress calc,
// search/filter/sort/pagination) is unit-tested directly against
// MailCampaignListService in tests/test_mail_campaign_list_service.py;
// these tests verify the frontend actually wires that data in as a wide
// table -- never side-by-side cards -- and never fabricates a metric.

const LIST_PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_campaign_list_service.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

// --- List page ---------------------------------------------------------------

test("the Campaigns page fetches real campaigns via listMailCampaignList, not the old card-grid listUnifiedCampaigns", () => {
  assert.match(LIST_PAGE_SOURCE, /listMailCampaignList\(/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /listUnifiedCampaigns\(/);
});

test("the Campaigns page renders campaigns as table rows, not a side-by-side card grid", () => {
  assert.match(LIST_PAGE_SOURCE, /<table/);
  assert.match(LIST_PAGE_SOURCE, /<tbody/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /grid gap-4 sm:grid-cols-2/);
});

test("the Campaigns page sends search and status filter to the backend", () => {
  assert.match(LIST_PAGE_SOURCE, /q: search\.trim\(\)/);
  assert.match(LIST_PAGE_SOURCE, /status: statusFilter/);
});

test("the Campaigns page sends sort_by/sort_dir and defaults to updated_at, newest first", () => {
  assert.match(LIST_PAGE_SOURCE, /useState<MailCampaignListSortBy>\("updated_at"\)/);
  assert.match(LIST_PAGE_SOURCE, /useState<"asc" \| "desc">\("desc"\)/);
  assert.match(LIST_PAGE_SOURCE, /sortBy,\s*\n\s*sortDir,/);
});

test("the Campaigns page allows sorting by name, leads, replied, and progress via clickable headers", () => {
  assert.match(LIST_PAGE_SOURCE, /column="name"/);
  assert.match(LIST_PAGE_SOURCE, /column="total_leads"/);
  assert.match(LIST_PAGE_SOURCE, /column="replied"/);
  assert.match(LIST_PAGE_SOURCE, /column="progress"/);
  assert.match(LIST_PAGE_SOURCE, /column="updated_at"/);
  assert.match(LIST_PAGE_SOURCE, /function handleSort/);
});

test("the Campaigns page is genuinely server-paginated -- page/page_size are sent to the backend, not just used to slice an already-fetched array", () => {
  assert.match(LIST_PAGE_SOURCE, /page,\s*\n\s*pageSize,/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /page_size:\s*1\b/);
});

test("the Campaigns page resets to page 1 whenever a filter, search, or sort changes", () => {
  assert.match(LIST_PAGE_SOURCE, /function updateFilter/);
  assert.match(LIST_PAGE_SOURCE, /setPage\(1\)/);
});

test("each Campaigns row navigates to the existing campaign detail page, preserving current behavior", () => {
  assert.match(LIST_PAGE_SOURCE, /href=\{`\/manager\/campaigns\/mail\/\$\{campaign\.mail_campaign_id\}`\}/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /<Dialog[\s>]/);
});

test("the Campaigns page reuses the app's existing wide-detail-page container convention", () => {
  assert.match(LIST_PAGE_SOURCE, /MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS/);
});

test("the Campaigns table scrolls horizontally in its own container on narrow screens rather than corrupting the page", () => {
  assert.match(LIST_PAGE_SOURCE, /overflow-x-auto/);
});

test("the Campaigns page reuses the real mailCampaignStatus label/badge helpers, never an invented status system", () => {
  assert.match(LIST_PAGE_SOURCE, /mailCampaignStatusLabel/);
  assert.match(LIST_PAGE_SOURCE, /mailCampaignStatusBadgeClass/);
});

test("the Campaigns page never renders fabricated engagement metrics like opens/clicks/reply rate", () => {
  assert.doesNotMatch(LIST_PAGE_SOURCE, /open_rate|click_rate|opens|clicks|reply_rate|replyRate/i);
});

test("Create Campaign stays prominent on the Campaigns page", () => {
  assert.match(LIST_PAGE_SOURCE, /href="\/manager\/campaigns\/new"/);
  assert.match(LIST_PAGE_SOURCE, /Create Campaign/);
});

// --- API client ---------------------------------------------------------------

test("MailCampaignListItem/MailCampaignListPage (api.ts) are distinct types from the existing MailCampaign", () => {
  assert.match(API_SOURCE, /export interface MailCampaignListItem/);
  assert.match(API_SOURCE, /export interface MailCampaignListPage/);
  // The existing type/route must still exist, untouched, for other callers.
  assert.match(API_SOURCE, /export interface MailCampaign\b/);
});

test("listMailCampaignList sends page/search/filter/sort as query params to GET /mail/campaign-list", () => {
  assert.match(API_SOURCE, /`\/mail\/campaign-list\?\$\{query\.toString\(\)\}`/);
});

// --- Backend model / route -----------------------------------------------------

test("MailCampaignListItem (backend model) has no fabricated engagement fields", () => {
  const modelBlock = MODEL_SOURCE.slice(
    MODEL_SOURCE.indexOf("class MailCampaignListItem"),
    MODEL_SOURCE.indexOf("class MailCampaignListPage")
  );
  assert.doesNotMatch(modelBlock, /open_rate|click_rate|reply_rate/i);
});

test("GET /mail/campaign-list exists, is read-only, and is a separate route from GET /mail/campaigns", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/campaign-list", response_model=MailCampaignListPage\)/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/campaign-list/);
});

test("MailCampaignListService is a pure aggregation over existing stores -- no new persistence, no write methods", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /\.create\(|\.save\(/);
  assert.match(SERVICE_SOURCE, /campaign_store: MailCampaignStore/);
  assert.match(SERVICE_SOURCE, /enrollment_store: MailEnrollmentStore/);
});

test("progress_percent is terminal enrollments over total, never fabricated to look stuck at 0% for an active campaign", () => {
  const methodBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def _build_item"),
    SERVICE_SOURCE.indexOf("async def list_campaigns")
  );
  assert.match(methodBlock, /terminal \/ total/);
  assert.match(methodBlock, /if total > 0 else 0\.0/);
});

test("a zero-enrollment campaign (e.g. a fresh Draft) shows 0% progress rather than dividing by zero", () => {
  assert.match(SERVICE_SOURCE, /if total > 0 else 0\.0/);
});

test("archived/historical campaigns are never excluded from the list", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /status\s*!=\s*MailCampaignStatus\.ARCHIVED/);
  assert.doesNotMatch(SERVICE_SOURCE, /exclude.*archived/i);
});
