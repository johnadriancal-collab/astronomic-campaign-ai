import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Inbox V1 (2026-09-17) -- same
// source-inspection pattern as mail-campaign-channels.test.ts, since this
// project has no DOM render harness (see package.json's test script).

const INBOX_PAGE_SOURCE = readFileSync(new URL("../app/manager/inbox/page.tsx", import.meta.url), "utf-8");
const OVERVIEW_PAGE_SOURCE = readFileSync(new URL("../app/manager/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MODEL_SOURCE = readFileSync(new URL("../../app/models/mail.py", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_inbox_service.py", import.meta.url), "utf-8");
const REPLY_STORE_SOURCE = readFileSync(new URL("../../app/repositories/mail_reply_store.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

test("the Inbox page fetches real replies via listInboxReplies, not fabricated data", () => {
  assert.match(INBOX_PAGE_SOURCE, /listInboxReplies/);
  assert.match(INBOX_PAGE_SOURCE, /MailInboxReplyView/);
});

test("the Inbox page no longer says Coming soon", () => {
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /Coming soon/);
});

test("the Inbox page renders the real contact name/email, campaign name, replied timestamp, and Gmail thread id", () => {
  assert.match(INBOX_PAGE_SOURCE, /contact_name/);
  assert.match(INBOX_PAGE_SOURCE, /\.email\b/);
  assert.match(INBOX_PAGE_SOURCE, /campaign_name/);
  assert.match(INBOX_PAGE_SOURCE, /replied_at/);
  assert.match(INBOX_PAGE_SOURCE, /gmail_thread_id/);
});

test("the Inbox page never fabricates unread/read state or reply body content", () => {
  // Matches actual usage (a state var, a prop, a rendered field) -- not the
  // module's own doc comment explaining that no such model exists.
  for (const forbidden of [/\bunreadCount\b/i, /isUnread/i, /\.body\b/, /reply\.snippet/i, /reply_body/i]) {
    assert.doesNotMatch(INBOX_PAGE_SOURCE, forbidden);
  }
});

test("the Inbox page trusts the backend's ordering rather than re-sorting client-side", () => {
  // Newest-first is guaranteed by MailReplyStore.list_all() (see below) --
  // the page must not silently reorder what it's given.
  assert.doesNotMatch(INBOX_PAGE_SOURCE, /\.sort\(/);
});

test("the Inbox page renders an empty state distinct from the loading/error states", () => {
  assert.match(INBOX_PAGE_SOURCE, /No replies yet/);
  assert.match(INBOX_PAGE_SOURCE, /Loading/);
  assert.match(INBOX_PAGE_SOURCE, /Couldn.t load the Inbox/);
});

test("the Inbox detail view discloses metadata-only, no invented body content", () => {
  assert.match(INBOX_PAGE_SOURCE, /Metadata only/);
  assert.match(INBOX_PAGE_SOURCE, /gmail\.metadata/);
});

test("MailInboxReplyView (api.ts) has no body/snippet or unread/read field", () => {
  const typeBlock = API_SOURCE.slice(
    API_SOURCE.indexOf("export interface MailInboxReplyView"),
    API_SOURCE.indexOf("export function listInboxReplies")
  );
  for (const forbidden of [/\bbody\b/i, /\bsnippet\b/i, /\bunread\b/i, /\bread:\s*boolean/i]) {
    assert.doesNotMatch(typeBlock, forbidden);
  }
});

test("GET /mail/inbox/replies exists and is read-only (no mutation route alongside it)", () => {
  assert.match(API_ROUTE_SOURCE, /@router\.get\("\/inbox\/replies"/);
  assert.doesNotMatch(API_ROUTE_SOURCE, /@router\.(post|patch|put|delete)\("\/inbox/);
});

test("MailInboxReplyView (backend model) has no body/snippet or unread/read field", () => {
  const modelBlock = MODEL_SOURCE.slice(
    MODEL_SOURCE.indexOf("class MailInboxReplyView"),
    MODEL_SOURCE.indexOf("class MailInboxReplyView") + 2000
  );
  for (const forbidden of [/\bbody:/i, /\bsnippet:/i, /\bunread:/i, /\bread:\s*bool/i]) {
    assert.doesNotMatch(modelBlock, forbidden);
  }
});

test("MailInboxService composes from existing stores, never a second reply model", () => {
  assert.match(SERVICE_SOURCE, /reply_store: MailReplyStore/);
  assert.doesNotMatch(SERVICE_SOURCE, /class MailReply\b/); // never redefines MailReply itself
});

test("MailReplyStore.list_all is newest-first and every implementation provides it", () => {
  assert.match(REPLY_STORE_SOURCE, /async def list_all/);
  assert.match(REPLY_STORE_SOURCE, /reverse=True/);
});

test("Overview page no longer shows Coming soon on the built-out sections (Campaigns/Emails/Leads/Inbox)", () => {
  const sectionsBlock = OVERVIEW_PAGE_SOURCE.slice(
    OVERVIEW_PAGE_SOURCE.indexOf("const SECTIONS"),
    OVERVIEW_PAGE_SOURCE.indexOf("export default function")
  );
  assert.match(sectionsBlock, /title: "Campaigns"[\s\S]*?builtOut: true/);
  assert.match(sectionsBlock, /title: "Emails"[\s\S]*?builtOut: true/);
  assert.match(sectionsBlock, /title: "Leads"[\s\S]*?builtOut: true/);
  assert.match(sectionsBlock, /title: "Inbox"[\s\S]*?builtOut: true/);
});

test("Overview page is honest that Analytics and Settings are still unbuilt", () => {
  const sectionsBlock = OVERVIEW_PAGE_SOURCE.slice(
    OVERVIEW_PAGE_SOURCE.indexOf("const SECTIONS"),
    OVERVIEW_PAGE_SOURCE.indexOf("export default function")
  );
  assert.match(sectionsBlock, /title: "Analytics"[\s\S]*?builtOut: false/);
  assert.match(sectionsBlock, /title: "Settings"[\s\S]*?builtOut: false/);
});

test("Overview only renders the Coming soon badge conditionally, not unconditionally for every card", () => {
  assert.match(OVERVIEW_PAGE_SOURCE, /!section\.builtOut/);
});

test("every Overview card is still a Link to its real route (already-existing navigation, unchanged)", () => {
  assert.match(OVERVIEW_PAGE_SOURCE, /href=\{section\.href\}/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/campaigns"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/emails"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/leads"/);
  assert.match(OVERVIEW_PAGE_SOURCE, /"\/manager\/inbox"/);
});
