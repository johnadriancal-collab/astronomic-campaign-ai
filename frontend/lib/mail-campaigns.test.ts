import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Campaign Manager Campaigns
// (V1 2026-09-17, QuickMail-style column/progress redefinition
// 2026-09-18) -- same source-inspection pattern as mail-leads.test.ts,
// since this project has no DOM render harness (see package.json's test
// script). Real aggregation logic (available/progress/reply-rate calc,
// search/filter/sort/pagination) is unit-tested directly against
// MailCampaignListService in tests/test_mail_campaign_list_service.py;
// these tests verify the frontend actually wires that data in as a wide
// table -- never side-by-side cards, never Mailbox/Sent columns any
// more -- and never fabricates a metric (Open rate stays a static
// "not tracked" cell, never a real-looking number).

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

test("the total-campaigns label is compact QuickMail-style text, using the real dynamic total", () => {
  assert.match(LIST_PAGE_SOURCE, /Total campaigns: \{total\}/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /Campaigns \(\{total\}\)/);
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

test("the Campaigns page allows sorting by every column the spec calls sortable", () => {
  assert.match(LIST_PAGE_SOURCE, /column="name"/);
  assert.match(LIST_PAGE_SOURCE, /column="status"/);
  assert.match(LIST_PAGE_SOURCE, /column="available"/);
  assert.match(LIST_PAGE_SOURCE, /column="total_leads"/);
  assert.match(LIST_PAGE_SOURCE, /column="progress"/);
  assert.match(LIST_PAGE_SOURCE, /column="reply_rate"/);
  assert.match(LIST_PAGE_SOURCE, /column="replied"/);
  assert.match(LIST_PAGE_SOURCE, /column="created_at"/);
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

test("Mailbox and Sent columns are gone -- Mailbox is a Channels-tab concept now, Sent is superseded by Available/Progress", () => {
  assert.doesNotMatch(LIST_PAGE_SOURCE, /mailbox_email|mailbox_count|mailbox_id/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, />Mailbox</);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /campaign\.sent\b/);
});

test("there is no separate Leads column -- Total is the only lead-count column, using total_leads", () => {
  assert.doesNotMatch(LIST_PAGE_SOURCE, />Leads</);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /label="Leads"/);
  assert.match(LIST_PAGE_SOURCE, /label="Total"/);
  assert.match(LIST_PAGE_SOURCE, /campaign\.total_leads/);
});

test("Available renders the campaign's own real available_leads field", () => {
  assert.match(LIST_PAGE_SOURCE, /label="Available"/);
  assert.match(LIST_PAGE_SOURCE, /campaign\.available_leads/);
});

test("Open rate is a static unsupported-state cell, never a real-looking percentage or a fabricated 0%", () => {
  const cellIndex = LIST_PAGE_SOURCE.indexOf("title={OPEN_RATE_TOOLTIP}");
  assert.ok(cellIndex !== -1, "expected an Open rate cell with title={OPEN_RATE_TOOLTIP}");
  const cellBlock = LIST_PAGE_SOURCE.slice(cellIndex, cellIndex + 120);
  assert.match(cellBlock, /—/);
  assert.doesNotMatch(LIST_PAGE_SOURCE, /open_rate/);
});

test("Reply rate renders the campaign's own real reply_rate_percent field", () => {
  assert.match(LIST_PAGE_SOURCE, /label="Reply rate"/);
  assert.match(LIST_PAGE_SOURCE, /campaign\.reply_rate_percent/);
});

test("Campaign created renders the campaign's own real created_at as a compact date, distinct from Last updated", () => {
  assert.match(LIST_PAGE_SOURCE, /label="Campaign created"/);
  assert.match(LIST_PAGE_SOURCE, /function formatDate\(/);
  assert.match(LIST_PAGE_SOURCE, /formatDate\(campaign\.created_at\)/);
  assert.match(LIST_PAGE_SOURCE, /formatDateTime\(campaign\.updated_at\)/);
});

test("Create Campaign stays prominent on the Campaigns page", () => {
  assert.match(LIST_PAGE_SOURCE, /href="\/manager\/campaigns\/new"/);
  assert.match(LIST_PAGE_SOURCE, /Create Campaign/);
});

// --- Column order (the exact spec) ------------------------------------------

test("the header row renders columns in the exact required order", () => {
  // Sortable headers are rendered via <SortHeader label="X" .../> (no
  // literal ">X<" text node); the rest (Open rate/Suppressed/Failed/
  // Steps) are plain static <th> text, but JSX formatting puts
  // whitespace/newlines between the ">" and the label. Locate each by
  // whichever marker actually appears, tolerating that whitespace, and
  // confirm their positions are in the exact required order.
  const sortableLabels = new Set(["Status", "Campaign", "Available", "Total", "Progress", "Reply rate", "Replied", "Campaign created", "Last updated"]);
  const order = ["Status", "Campaign", "Available", "Total", "Progress", "Open rate", "Reply rate", "Replied", "Suppressed", "Failed", "Steps", "Campaign created", "Last updated"];
  const headSection = LIST_PAGE_SOURCE.slice(LIST_PAGE_SOURCE.indexOf("<thead"), LIST_PAGE_SOURCE.indexOf("</thead>"));
  const indices = order.map((label) => {
    if (sortableLabels.has(label)) {
      const idx = headSection.indexOf(`label="${label}"`);
      assert.ok(idx !== -1, `expected a sortable header for "${label}"`);
      return idx;
    }
    const match = headSection.match(new RegExp(`>\\s*${label}\\s*<`));
    assert.ok(match, `expected a static header for "${label}"`);
    return match.index;
  });
  const sorted = [...indices].sort((a, b) => a - b);
  assert.deepEqual(indices, sorted, "header columns are not in the required order");
});

// --- API client ---------------------------------------------------------------

test("MailCampaignListItem/MailCampaignListPage (api.ts) are distinct types from the existing MailCampaign", () => {
  assert.match(API_SOURCE, /export interface MailCampaignListItem/);
  assert.match(API_SOURCE, /export interface MailCampaignListPage/);
  // The existing type/route must still exist, untouched, for other callers.
  assert.match(API_SOURCE, /export interface MailCampaign\b/);
});

test("MailCampaignListItem (api.ts) carries available_leads/finished_leads/in_progress_leads/reply_rate_percent and no mailbox/sent/completed fields", () => {
  const start = API_SOURCE.indexOf("export interface MailCampaignListItem");
  const end = API_SOURCE.indexOf("export interface MailCampaignListPage");
  const block = API_SOURCE.slice(start, end);
  assert.match(block, /available_leads: number/);
  assert.match(block, /finished_leads: number/);
  assert.match(block, /in_progress_leads: number/);
  assert.match(block, /reply_rate_percent: number/);
  assert.doesNotMatch(block, /mailbox_id|mailbox_email|mailbox_count|\bsent:|\bcompleted:/);
});

test("listMailCampaignList sends page/search/filter/sort as query params to GET /mail/campaign-list, with no mailbox_email filter any more", () => {
  assert.match(API_SOURCE, /`\/mail\/campaign-list\?\$\{query\.toString\(\)\}`/);
  assert.doesNotMatch(API_SOURCE, /mailboxEmail/);
});

// --- Backend model / route -----------------------------------------------------

test("MailCampaignListItem (backend model) has no open/click-rate field, but does carry the real reply_rate_percent", () => {
  const modelBlock = MODEL_SOURCE.slice(
    MODEL_SOURCE.indexOf("class MailCampaignListItem"),
    MODEL_SOURCE.indexOf("class MailCampaignListPage")
  );
  assert.doesNotMatch(modelBlock, /open_rate|click_rate/i);
  assert.match(modelBlock, /reply_rate_percent: float/);
  assert.match(modelBlock, /available_leads: int/);
  assert.match(modelBlock, /finished_leads: int/);
  assert.match(modelBlock, /in_progress_leads: int/);
  assert.doesNotMatch(modelBlock, /mailbox_id|mailbox_email|mailbox_count/);
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

test("available_leads is total minus leads whose Step 1 actually reached SENT -- never a terminal-enrollment ratio", () => {
  const methodBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def _build_item"),
    SERVICE_SOURCE.indexOf("async def list_campaigns")
  );
  assert.match(methodBlock, /step\.step_number == 1 and step\.status == MailEnrollmentStepStatus\.SENT/);
  assert.match(methodBlock, /available = total - started/);
});

test("progress_percent is finished/total (sequence-completion progress), never lead-start progress or a step-send count", () => {
  const methodBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def _build_item"),
    SERVICE_SOURCE.indexOf("async def list_campaigns")
  );
  assert.match(methodBlock, /round\(\(finished \/ total\) \* 100, 1\) if total > 0 else 0\.0/);
  assert.match(methodBlock, /finished = counts\[MailEnrollmentStatus\.COMPLETED\]/);
  assert.match(methodBlock, /in_progress = total - finished/);
});

test("finished_leads counts only COMPLETED -- REPLIED/SUPPRESSED/FAILED (each also terminal) are explicitly excluded", () => {
  const methodBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def _build_item"),
    SERVICE_SOURCE.indexOf("async def list_campaigns")
  );
  // Only one counts[...] read feeds `finished` -- COMPLETED -- never a
  // sum that also folds in REPLIED/SUPPRESSED/FAILED.
  const finishedLine = methodBlock.match(/finished = .+/)?.[0] ?? "";
  assert.match(finishedLine, /MailEnrollmentStatus\.COMPLETED/);
  assert.doesNotMatch(finishedLine, /REPLIED|SUPPRESSED|FAILED/);
});

test("reply_rate_percent uses the exact same replied/total formula as the campaign Dashboard stats strip", () => {
  const methodBlock = SERVICE_SOURCE.slice(
    SERVICE_SOURCE.indexOf("async def _build_item"),
    SERVICE_SOURCE.indexOf("async def list_campaigns")
  );
  assert.match(methodBlock, /round\(\(replied \/ total\) \* 100, 1\) if total > 0 else 0\.0/);
});

test("a zero-enrollment campaign (e.g. a fresh Draft) shows 0% progress rather than dividing by zero", () => {
  assert.match(SERVICE_SOURCE, /if total > 0 else 0\.0/);
});

test("archived/historical campaigns are never excluded from the list", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /status\s*!=\s*MailCampaignStatus\.ARCHIVED/);
  assert.doesNotMatch(SERVICE_SOURCE, /exclude.*archived/i);
});

// --- Table density / campaign-name truncation (2026-09-17 tightening) --------
//
// QuickMail-density pass: single-line, ellipsis-truncated campaign names,
// tighter cell padding, a bounded Campaign column width, and a hover
// tooltip carrying the full name -- layout-only, no change to the read
// model, sorting, search, filters, or pagination above.

const NAME_LINK_START = LIST_PAGE_SOURCE.indexOf("href={`/manager/campaigns/mail/");
const NAME_CELL = LIST_PAGE_SOURCE.slice(NAME_LINK_START, LIST_PAGE_SOURCE.indexOf("</Link>", NAME_LINK_START));

test("the campaign name renders as a single line with ellipsis truncation, never wraps", () => {
  assert.match(NAME_CELL, /truncate/);
  assert.match(NAME_CELL, /whitespace-nowrap/);
});

test("the campaign name's full text is preserved as the stored name and exposed via a title attribute (hover tooltip)", () => {
  assert.match(NAME_CELL, /title=\{campaign\.name\}/);
  // The rendered text itself is still the real, untruncated campaign.name
  // -- truncation is CSS-only, never a manually shortened string.
  assert.doesNotMatch(LIST_PAGE_SOURCE, /campaign\.name\.slice\(|campaign\.name\.substring\(/);
});

test("the Campaign column has a bounded width so it can't consume the whole table or collapse to near-zero", () => {
  assert.match(LIST_PAGE_SOURCE, /w-\[240px\]/);
});

test("table cell padding is tighter than the original px-4 py-2.5 pass, for a more compact row height", () => {
  assert.doesNotMatch(LIST_PAGE_SOURCE, /px-4 py-2\.5/);
  assert.match(LIST_PAGE_SOURCE, /px-3 py-2\b/);
});

test("narrow numeric/status columns are pinned to a fixed width so they never steal space from Campaign", () => {
  assert.match(LIST_PAGE_SOURCE, /Status:\s*"w-\[90px\]"/);
  assert.match(LIST_PAGE_SOURCE, /Available:\s*"w-\[80px\]"/);
  assert.match(LIST_PAGE_SOURCE, /Total:\s*"w-\[70px\]"/);
  assert.match(LIST_PAGE_SOURCE, /Steps:\s*"w-\[60px\]"/);
});

test("Status and date cells never wrap, keeping every row's height consistent", () => {
  const statusCellStart = LIST_PAGE_SOURCE.indexOf('className={cn("whitespace-nowrap px-3 py-2", COLUMN_WIDTH_CLASS.Status)}');
  assert.ok(statusCellStart !== -1);
  assert.match(LIST_PAGE_SOURCE, /whitespace-nowrap px-3 py-2 text-right text-muted-foreground",\s*COLUMN_WIDTH_CLASS\["Campaign created"\]/);
  assert.match(LIST_PAGE_SOURCE, /whitespace-nowrap px-3 py-2 text-right text-muted-foreground",\s*COLUMN_WIDTH_CLASS\["Last updated"\]/);
});

test("Last updated still renders a real formatted date/time, just a more compact one, with the exact timestamp in a tooltip", () => {
  assert.match(LIST_PAGE_SOURCE, /function formatDateTime/);
  assert.match(LIST_PAGE_SOURCE, /month: "short", day: "numeric", hour: "numeric", minute: "2-digit"/);
  assert.match(LIST_PAGE_SOURCE, /function exactTimestamp/);
});

test("the table still scrolls horizontally in its own container rather than corrupting the page on narrow screens", () => {
  assert.match(LIST_PAGE_SOURCE, /overflow-x-auto/);
});

test("search, sort, filter, and pagination wiring are unchanged by the column redefinition", () => {
  assert.match(LIST_PAGE_SOURCE, /q: search\.trim\(\)/);
  assert.match(LIST_PAGE_SOURCE, /status: statusFilter/);
  assert.match(LIST_PAGE_SOURCE, /function handleSort/);
  assert.match(LIST_PAGE_SOURCE, /page,\s*\n\s*pageSize,/);
});

// --- Progress bar redefinition (2026-09-18b) --------------------------------
//
// Colored = finished the sequence (finished_leads), neutral = still in
// progress (in_progress_leads) -- a SEPARATE concept from Available
// (lead-start progress). No visible percentage text any more; a
// compact native-tooltip hover carries the exact counts instead. The
// bar itself is roughly double its original width.

const PROGRESS_CELL_SOURCE = LIST_PAGE_SOURCE.slice(
  LIST_PAGE_SOURCE.indexOf("function ProgressCell"),
  LIST_PAGE_SOURCE.indexOf("// Narrow/pinned columns")
);

test("ProgressCell takes finished/inProgress/total, never a bare percent prop", () => {
  assert.match(PROGRESS_CELL_SOURCE, /finished: number; inProgress: number; total: number/);
  assert.doesNotMatch(PROGRESS_CELL_SOURCE, /percent: number \}: \{ percent/);
});

test("no visible percentage text renders next to the Progress bar any more", () => {
  assert.doesNotMatch(PROGRESS_CELL_SOURCE, /<span[^>]*>\{percent\}%<\/span>/);
  assert.doesNotMatch(PROGRESS_CELL_SOURCE, /\{percent\}%/);
});

test("hovering the Progress bar shows a compact tooltip with the exact in-progress and completed/total counts", () => {
  assert.match(PROGRESS_CELL_SOURCE, /\$\{inProgress\} leads in progress/);
  assert.match(PROGRESS_CELL_SOURCE, /\$\{finished\} completed of \$\{total\} total/);
  assert.match(PROGRESS_CELL_SOURCE, /title=\{tooltip\}/);
});

test("the Progress bar itself is roughly double its original width, both the column and the inner track", () => {
  assert.match(LIST_PAGE_SOURCE, /Progress:\s*"w-\[300px\]"/); // was w-[150px]
  assert.match(PROGRESS_CELL_SOURCE, /w-32/); // was w-16
});

test("the ProgressCell call site passes real finished_leads/in_progress_leads/total_leads, never a raw progress_percent prop", () => {
  const callSiteIndex = LIST_PAGE_SOURCE.indexOf("<ProgressCell");
  const callSite = LIST_PAGE_SOURCE.slice(callSiteIndex, LIST_PAGE_SOURCE.indexOf("/>", callSiteIndex));
  assert.match(callSite, /finished=\{campaign\.finished_leads\}/);
  assert.match(callSite, /inProgress=\{campaign\.in_progress_leads\}/);
  assert.match(callSite, /total=\{campaign\.total_leads\}/);
  assert.doesNotMatch(callSite, /percent=\{campaign\.progress_percent\}/);
});
