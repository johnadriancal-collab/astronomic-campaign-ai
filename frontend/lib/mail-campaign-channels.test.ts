import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for the Channels tab (campaign<->mailbox
// selection) -- same source-level-assertion pattern as
// mail-campaign-dashboard.test.ts, since this project has no DOM render
// harness (see package.json's test script).

const CHANNELS_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-channels-tab.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const SERVICE_SOURCE = readFileSync(new URL("../../app/services/mail_campaign_service.py", import.meta.url), "utf-8");
const API_ROUTE_SOURCE = readFileSync(new URL("../../app/api/mail.py", import.meta.url), "utf-8");

test("the Channels tab never fabricates a QuickMail-only sending metric", () => {
  for (const forbidden of [
    /Deliverability/i,
    /Emails Sent/i,
    /\bQueue\b/,
    /Smart sending group/i,
    /Campaigns\s*<\/th>/i, // the Emails page's "Campaigns" column header specifically
    /quota/i,
  ]) {
    assert.doesNotMatch(CHANNELS_TAB_SOURCE, forbidden);
  }
});

test("the Channels tab reuses the real mailbox display/status helpers, not invented ones", () => {
  assert.match(CHANNELS_TAB_SOURCE, /mailboxDisplayName/);
  assert.match(CHANNELS_TAB_SOURCE, /mailboxStatusLabel/);
  assert.match(CHANNELS_TAB_SOURCE, /mailboxStatusBadgeClass/);
  assert.match(CHANNELS_TAB_SOURCE, /providerLabel/);
});

test("the Channels tab renders real Mailbox fields only (email, provider, status)", () => {
  assert.match(CHANNELS_TAB_SOURCE, /mailbox\.email/);
  assert.match(CHANNELS_TAB_SOURCE, /mailbox\.status/);
});

test("a mailbox that isn't currently connected can't be newly selected", () => {
  assert.match(CHANNELS_TAB_SOURCE, /canBeNewlySelected/);
  assert.match(CHANNELS_TAB_SOURCE, /"connected"/);
});

test("the empty state links to the existing Emails page rather than a new connect flow", () => {
  assert.match(CHANNELS_TAB_SOURCE, /href="\/manager\/emails"/);
  assert.doesNotMatch(CHANNELS_TAB_SOURCE, /startGoogleMailboxConnect/);
  assert.doesNotMatch(CHANNELS_TAB_SOURCE, /ConnectEmailModal/);
});

test("the Channels tab has a clear Save action", () => {
  assert.match(CHANNELS_TAB_SOURCE, /Save Channels/);
});

test("the Channels API client functions never touch OAuth/credential endpoints", () => {
  assert.match(API_SOURCE, /getMailCampaignChannels/);
  assert.match(API_SOURCE, /setMailCampaignChannels/);
  assert.match(API_SOURCE, /\/mail\/campaigns\/\$\{mailCampaignId\}\/channels/);
});

// --- Archived: read-only Channels -----------------------------------------

test("the Channels tab accepts a readOnly prop and disables every switch when set", () => {
  assert.match(CHANNELS_TAB_SOURCE, /readOnly/);
  assert.match(CHANNELS_TAB_SOURCE, /toggleDisabled\s*=\s*readOnly/);
});

test("the Channels tab hides the Save action entirely when readOnly", () => {
  assert.match(CHANNELS_TAB_SOURCE, /\{!readOnly\s*&&\s*\(/);
});

test("the Channels tab still displays the existing selection when archived, not an empty/hidden table", () => {
  // readOnly must gate the SWITCH and the SAVE action only -- the mailbox
  // rows/table themselves must not be conditionally hidden on readOnly.
  assert.doesNotMatch(CHANNELS_TAB_SOURCE, /readOnly\s*&&\s*mailboxes\.length/);
  assert.doesNotMatch(CHANNELS_TAB_SOURCE, /!readOnly\s*&&\s*mailboxes\.map/);
});

test("the campaign detail page marks Channels readOnly only once archived", () => {
  assert.match(PAGE_SOURCE, /readOnly=\{campaign\.status === "archived"\}/);
});

test("the campaign detail page's Channels handlers refuse to act on an archived campaign", () => {
  assert.match(PAGE_SOURCE, /handleToggleChannel[\s\S]{0,120}campaign\?\.status === "archived"/);
  assert.match(PAGE_SOURCE, /handleSaveChannels[\s\S]{0,120}campaign\?\.status === "archived"/);
});

test("the backend independently rejects PUT .../channels for an archived campaign (not just the frontend)", () => {
  assert.match(SERVICE_SOURCE, /MailCampaignChannelsFrozenError/);
  assert.match(SERVICE_SOURCE, /MailCampaignStatus\.ARCHIVED/);
  assert.match(API_ROUTE_SOURCE, /MailCampaignChannelsFrozenError/);
  assert.match(API_ROUTE_SOURCE, /status_code=409/);
});
