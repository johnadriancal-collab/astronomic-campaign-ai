import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Emails table density pass (2026-09-18) -- same table-standard as the
// Campaigns list page's own density pass (see mail-campaigns.test.ts):
// single-line identity columns with ellipsis truncation and a hover
// tooltip, tighter cell padding, narrow numeric columns, and a compact
// (not three-line) Authorization cell. Layout-only -- mailbox data,
// OAuth/reconnect logic, and authorization-health calculations are
// untouched (see tests/test_mailbox_authorization_health.py and
// tests/test_mailbox_service.py for that coverage).

const PAGE_SOURCE = readFileSync(new URL("../app/manager/emails/page.tsx", import.meta.url), "utf-8");

test("the Name cell truncates to a single line and carries the full name as a title attribute", () => {
  const nameCellIndex = PAGE_SOURCE.indexOf('title={name}>');
  assert.ok(nameCellIndex !== -1, "expected a Name cell with title={name}");
  const cellBlock = PAGE_SOURCE.slice(nameCellIndex - 200, nameCellIndex + 50);
  assert.match(cellBlock, /truncate/);
  assert.match(cellBlock, /whitespace-nowrap/);
  assert.match(cellBlock, /max-w-\[170px\]/);
});

test("the Email cell truncates to a single line and carries the full email as a title attribute", () => {
  const emailCellIndex = PAGE_SOURCE.indexOf("title={mailbox.email}");
  assert.ok(emailCellIndex !== -1, "expected an Email cell with title={mailbox.email}");
  const cellBlock = PAGE_SOURCE.slice(emailCellIndex - 200, emailCellIndex + 50);
  assert.match(cellBlock, /truncate/);
  assert.match(cellBlock, /whitespace-nowrap/);
});

test("stored mailbox names/emails are never manually shortened -- truncation is CSS-only", () => {
  assert.doesNotMatch(PAGE_SOURCE, /mailbox\.email\.slice\(|mailbox\.email\.substring\(/);
  assert.doesNotMatch(PAGE_SOURCE, /name\.slice\(|name\.substring\(/);
});

test("small numeric/compact columns (TLD, Campaigns, Emails Sent Today, Queue) are pinned to a narrow width", () => {
  assert.match(PAGE_SOURCE, /TLD:\s*"w-\[56px\]"/);
  assert.match(PAGE_SOURCE, /Campaigns:\s*"w-\[74px\]"/);
  assert.match(PAGE_SOURCE, /"Emails Sent Today":\s*"w-\[92px\]"/);
  assert.match(PAGE_SOURCE, /Queue:\s*"w-\[60px\]"/);
});

test("Name and Email columns get real width, wider than any compact numeric column", () => {
  assert.match(PAGE_SOURCE, /Name:\s*"w-\[170px\]"/);
  assert.match(PAGE_SOURCE, /Email:\s*"w-\[210px\]"/);
});

test("row cell padding is tighter than the original px-3 py-2.5 pass", () => {
  assert.doesNotMatch(PAGE_SOURCE, /px-3 py-2\.5/);
  assert.match(PAGE_SOURCE, /px-3 py-1\.5/);
});

test("the Authorization cell is at most two lines -- a single status+age row, plus an optional warning row only when at risk", () => {
  const authCellStart = PAGE_SOURCE.indexOf('<td className="px-3 py-1.5">');
  const authCellEnd = PAGE_SOURCE.indexOf("</td>", authCellStart);
  const authBlock = PAGE_SOURCE.slice(authCellStart, authCellEnd);
  // Status badge and the age text now sit in the SAME flex row, not
  // separate stacked rows.
  assert.match(authBlock, /flex items-center gap-1\.5 whitespace-nowrap/);
  assert.match(authBlock, /flex flex-col gap-0\.5/);
});

test("the Reconnect action in the Authorization cell still fires for reconnect_soon and needs_reauth", () => {
  // atRisk is computed once per row (outside the Authorization <td>
  // itself) and consumed inside it -- check both halves.
  assert.match(PAGE_SOURCE, /const atRisk = mailbox\.authorization_health === "reconnect_soon"/);
  assert.match(PAGE_SOURCE, /mailbox\.authorization_health === "needs_reauth"/);
  const authCellStart = PAGE_SOURCE.indexOf('<td className="px-3 py-1.5">');
  const authCellEnd = PAGE_SOURCE.indexOf("</td>", authCellStart);
  const authBlock = PAGE_SOURCE.slice(authCellStart, authCellEnd);
  assert.match(authBlock, /atRisk &&/);
  assert.match(authBlock, /setReconnectTarget/);
});

test("the table has a real min-width and scrolls horizontally rather than wrapping Name/Email to fit", () => {
  assert.match(PAGE_SOURCE, /overflow-x-auto/);
  assert.match(PAGE_SOURCE, /min-w-\[1180px\]/);
});

test("search and Connect Email remain wired exactly as before", () => {
  assert.match(PAGE_SOURCE, /filterMailboxes\(mailboxes, query\)/);
  assert.match(PAGE_SOURCE, /Connect Email/);
});
