import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Campaign detail dashboard tab distribution + stats strip (2026-09-17).
// Source-level assertions (no DOM render harness in this project -- see
// package.json's test script), same pattern as mail-campaign-dashboard.test.ts.
// Real reply/unsub-rate computation is unit-tested directly against
// MailCampaignStatsService in tests/test_mail_campaign_stats_service.py;
// these tests verify the frontend wires that real data in, distributes
// the tabs full-width, and never fabricates Open/Bounce rate.

const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const STRIP_SOURCE = readFileSync(new URL("../components/mail-campaign-stats-strip.tsx", import.meta.url), "utf-8");
const DASHBOARD_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-dashboard-tab.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_campaign_stats_service.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

// --- Tab distribution ---------------------------------------------------------

test("all six tabs are rendered with a full-width-distributing class, not left-packed", () => {
  const TAB_LIST_BLOCK = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("<TabsList"), PAGE_SOURCE.indexOf("</TabsList>"));
  assert.match(TAB_LIST_BLOCK, /className="w-full/);
  for (const tab of ["dashboard", "leads", "steps", "channels", "schedule", "settings"]) {
    const tabMatch = TAB_LIST_BLOCK.match(new RegExp(`value="${tab}"[^>]*className="[^"]*flex-1[^"]*"`));
    assert.ok(tabMatch, `expected tab "${tab}" to carry a flex-1 (equal-width) class`);
  }
});

test("tabs are never squeezed to an unreadable width -- each keeps its natural min width and text never wraps", () => {
  const TAB_LIST_BLOCK = PAGE_SOURCE.slice(PAGE_SOURCE.indexOf("<TabsList"), PAGE_SOURCE.indexOf("</TabsList>"));
  assert.match(TAB_LIST_BLOCK, /min-w-fit/);
  assert.match(TAB_LIST_BLOCK, /whitespace-nowrap/);
});

test("the tab bar scrolls horizontally rather than corrupting the page on narrow screens", () => {
  assert.match(PAGE_SOURCE, /<TabsList className="w-full overflow-x-auto"/);
});

test("active-tab styling is preserved -- no override of the shared Tabs component's active-state classes", () => {
  const tabsComponentSource = readFileSync(new URL("../components/ui/tabs.tsx", import.meta.url), "utf-8");
  assert.match(tabsComponentSource, /data-\[active\]/);
});

// --- Stats strip: exactly 4 metrics -------------------------------------------

test("the stats strip renders exactly Open rate, Reply rate, Unsub rate, and Bounce rate, in that order", () => {
  const labels = [...STRIP_SOURCE.matchAll(/label="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(labels, ["Open rate", "Reply rate", "Unsub rate", "Bounce rate"]);
});

test("the stats strip is a compact single row, not four oversized cards", () => {
  assert.doesNotMatch(STRIP_SOURCE, /<Card/);
  assert.match(STRIP_SOURCE, /grid-cols-1[\s\S]*sm:grid-cols-4/);
});

test("each metric keeps its label and value on the same line -- no stacked flex-col layout", () => {
  const statItemBlock = STRIP_SOURCE.slice(STRIP_SOURCE.indexOf("function StatItem"));
  assert.doesNotMatch(statItemBlock, /flex-col/);
  assert.match(statItemBlock, /whitespace-nowrap/);
  // "Label:" rendered inline, immediately followed by the value in the same row.
  assert.match(statItemBlock, /\{label\}:/);
});

test("Reply rate and Unsub rate are wired to real MailCampaignStats fields", () => {
  assert.match(STRIP_SOURCE, /stats\.reply_rate_percent/);
  assert.match(STRIP_SOURCE, /stats\.unsub_rate_percent/);
});

test("Open rate and Bounce rate are hardcoded 'Not tracked' -- never derived from stats, never a fabricated percentage", () => {
  assert.match(STRIP_SOURCE, /label="Open rate" value="Not tracked"/);
  assert.match(STRIP_SOURCE, /label="Bounce rate" value="Not tracked"/);
  // No percent-sign literal anywhere near either forbidden label -- if
  // one were ever added, it would mean a fabricated number crept in.
  assert.doesNotMatch(STRIP_SOURCE, /Open rate.*stats\./);
  assert.doesNotMatch(STRIP_SOURCE, /Bounce rate.*stats\./);
});

test("the stats strip sits directly below the tabs and above Audience & Sequence on the Dashboard tab", () => {
  const stripIndex = DASHBOARD_TAB_SOURCE.indexOf("<MailCampaignStatsStrip");
  const audienceIndex = DASHBOARD_TAB_SOURCE.indexOf("Audience &amp; Sequence");
  assert.ok(stripIndex !== -1 && audienceIndex !== -1);
  assert.ok(stripIndex < audienceIndex);
});

test("the campaign detail page fetches real campaign stats and passes them to the Dashboard tab", () => {
  assert.match(PAGE_SOURCE, /getMailCampaignStats\(campaignId\)/);
  assert.match(PAGE_SOURCE, /<MailCampaignDashboardTab[^>]*stats=\{stats\}/);
});

// --- Stale scheduler copy -------------------------------------------------------

test("the stale 'no scheduler yet' claim is removed from the Dashboard tab", () => {
  assert.doesNotMatch(DASHBOARD_TAB_SOURCE, /no scheduler yet/i);
});

// --- API client ------------------------------------------------------------------

test("getMailCampaignStats calls the campaign-scoped stats route", () => {
  assert.match(API_SOURCE, /`\/mail\/campaigns\/\$\{mailCampaignId\}\/stats`/);
});

test("MailCampaignStats (api.ts) has no open_rate/bounce_rate field", () => {
  const typeBlock = API_SOURCE.slice(API_SOURCE.indexOf("interface MailCampaignStats"), API_SOURCE.indexOf("export function getMailCampaignStats"));
  assert.doesNotMatch(typeBlock, /open_rate|bounce_rate/i);
});

// --- Backend model / route / service ---------------------------------------------

test("GET /mail/campaigns/{id}/stats exists, is read-only", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/campaigns\/\{mail_campaign_id\}\/stats", response_model=MailCampaignStats\)/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/campaigns\/\{mail_campaign_id\}\/stats/);
});

test("MailCampaignStats (backend model) has no open_rate/bounce_rate field", () => {
  const modelBlock = MODEL_SOURCE.slice(MODEL_SOURCE.indexOf("class MailCampaignStats"), MODEL_SOURCE.indexOf("class MailCampaignStats") + 700);
  assert.doesNotMatch(modelBlock, /open_rate|bounce_rate/i);
});

test("MailCampaignStatsService is a pure read -- no write methods, no new persistence", () => {
  assert.doesNotMatch(SERVICE_SOURCE, /\.create\(|\.save\(|\.upsert\(/);
});

test("unsub rate counts only the UNSUBSCRIBED suppression reason, never every suppression reason lumped together", () => {
  assert.match(SERVICE_SOURCE, /MailSuppressionReason\.UNSUBSCRIBED/);
  assert.doesNotMatch(SERVICE_SOURCE, /s\.active\s*$/m); // never filters by active alone, without also filtering by reason
});

test("reply_rate_percent uses the same rounding/zero-division convention as progress_percent elsewhere", () => {
  assert.match(SERVICE_SOURCE, /round\(\(replied \/ total\) \* 100, 1\) if total > 0 else 0\.0/);
  assert.match(SERVICE_SOURCE, /round\(\(unsubscribed \/ total\) \* 100, 1\) if total > 0 else 0\.0/);
});

// --- Sending/reply behavior untouched --------------------------------------------

test("MailCampaignStatsService never imports or calls into sending, scheduling, or reply-detection services", () => {
  // Checks actual imports/usage, not doc prose -- this service's own
  // docstring legitimately explains that REPLIED status is set by
  // MailSendingService elsewhere, without this service ever depending on it.
  assert.doesNotMatch(SERVICE_SOURCE, /^from app\.services\.mail_sending_service import/m);
  assert.doesNotMatch(SERVICE_SOURCE, /^from app\.services\.mail_execution_worker import/m);
  assert.doesNotMatch(SERVICE_SOURCE, /^from app\.services\.mail_reply_detection_service import/m);
  assert.doesNotMatch(SERVICE_SOURCE, /resolve_next_send_time\(/);
});
