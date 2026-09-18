import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Leads table density pass (2026-09-18) -- same Campaign Manager table
// standard established on Campaigns (mail-campaigns.test.ts) and Emails
// (mail-emails-density.test.ts): single-line identity columns with
// ellipsis truncation and a hover tooltip, tighter cell padding, narrow
// compact columns, no wrapping on desktop. Layout-only -- the Leads
// API/aggregation/dedup/status/search/filter/pagination/sort logic is
// untouched (see tests/test_mail_leads_service.py for that coverage).

const PAGE_SOURCE = readFileSync(new URL("../app/manager/leads/page.tsx", import.meta.url), "utf-8");

test("the Name cell is single-line (whitespace-nowrap) with a truncating inner span and a title tooltip", () => {
  const nameLinkStart = PAGE_SOURCE.indexOf("href={`/manager/leads/${lead.crm_contact_id}`}");
  const nameLinkBlock = PAGE_SOURCE.slice(nameLinkStart, PAGE_SOURCE.indexOf("</Link>", nameLinkStart));
  assert.match(nameLinkBlock, /title=\{name\}/);
  assert.match(nameLinkBlock, /whitespace-nowrap/);
  assert.match(nameLinkBlock, /className="min-w-0 truncate"/);
});

test("the reply indicator sits inside the same single-line row as the name, never forcing a second line", () => {
  const nameLinkStart = PAGE_SOURCE.indexOf("href={`/manager/leads/${lead.crm_contact_id}`}");
  const nameLinkBlock = PAGE_SOURCE.slice(nameLinkStart, PAGE_SOURCE.indexOf("</Link>", nameLinkStart));
  assert.match(nameLinkBlock, /lead\.replied &&/);
  assert.match(nameLinkBlock, /MessageSquare/);
  assert.match(nameLinkBlock, /shrink-0/);
});

test("Email, Company, and Title cells truncate to one line with a title tooltip, never wrapping", () => {
  for (const field of ["lead.email", "lead.company", "lead.title"]) {
    const titleIndex = PAGE_SOURCE.indexOf(`title={${field} ?? undefined}`);
    assert.ok(titleIndex !== -1, `expected a title tooltip for ${field}`);
    const cellBlock = PAGE_SOURCE.slice(titleIndex - 200, titleIndex);
    assert.match(cellBlock, /truncate/);
    assert.match(cellBlock, /whitespace-nowrap/);
  }
});

test("the Last Campaign cell truncates to one line with the full name as a title tooltip, navigation intact", () => {
  const linkStart = PAGE_SOURCE.indexOf("href={`/manager/campaigns/mail/${lead.last_campaign_id}`}");
  const linkBlock = PAGE_SOURCE.slice(linkStart, PAGE_SOURCE.indexOf("</Link>", linkStart));
  assert.match(linkBlock, /title=\{lead\.last_campaign_name\}/);
  assert.match(linkBlock, /truncate/);
  assert.match(linkBlock, /whitespace-nowrap/);
});

test("stored names/emails/campaign names are never manually shortened -- truncation is CSS-only", () => {
  assert.doesNotMatch(PAGE_SOURCE, /\.slice\(0,|\.substring\(0,/);
});

test("Last Activity renders a compact single-line date (no year, no wrapping)", () => {
  assert.match(PAGE_SOURCE, /month: "short", day: "numeric", hour: "numeric", minute: "2-digit"/);
  const cellIndex = PAGE_SOURCE.indexOf("{formatDateTime(lead.last_activity_at)}");
  const cellBlock = PAGE_SOURCE.slice(cellIndex - 150, cellIndex);
  assert.match(cellBlock, /whitespace-nowrap/);
});

test("row cell padding is tighter than the original px-4 py-2.5 pass", () => {
  assert.doesNotMatch(PAGE_SOURCE, /px-4 py-2\.5/);
  assert.match(PAGE_SOURCE, /px-3 py-1\.5/);
});

test("identity/context columns (Name, Email, Last Campaign) get more width than compact columns (Status, Campaigns)", () => {
  assert.match(PAGE_SOURCE, /name:\s*"w-\[160px\]"/);
  assert.match(PAGE_SOURCE, /email:\s*"w-\[200px\]"/);
  assert.match(PAGE_SOURCE, /lastCampaign:\s*"w-\[220px\]"/);
  assert.match(PAGE_SOURCE, /status:\s*"w-\[100px\]"/);
  assert.match(PAGE_SOURCE, /campaigns:\s*"w-\[90px\]"/);
});

test("the table has a real min-width and scrolls horizontally rather than wrapping cells to fit", () => {
  assert.match(PAGE_SOURCE, /overflow-x-auto/);
  assert.match(PAGE_SOURCE, /min-w-\[1180px\]/);
});

test("search/filter/sort/pagination wiring is unchanged by the density pass", () => {
  assert.match(PAGE_SOURCE, /q: search\.trim\(\)/);
  assert.match(PAGE_SOURCE, /status: statusFilter/);
  assert.match(PAGE_SOURCE, /campaignId: campaignFilter/);
  assert.match(PAGE_SOURCE, /replied: repliedFilter/);
  assert.match(PAGE_SOURCE, /function handleSort/);
  assert.match(PAGE_SOURCE, /page,\s*\n\s*pageSize,/);
});
