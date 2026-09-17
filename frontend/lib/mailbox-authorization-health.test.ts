import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  estimatedExpiryLabel,
  formatAuthorizationAge,
  mailboxAuthorizationHealthBadgeClass,
  mailboxAuthorizationHealthLabel,
  reconnectRequiredBeforeNextSend,
} from "./mailboxes.ts";

// Proactive Gmail OAuth expiration warnings (2026-09-17). Pure logic
// unit-tested directly (unlike most of this repo's frontend coverage,
// which is source-inspection only, since there's real branching logic
// here worth exercising with real inputs); wiring into the three surfaces
// the approved spec asked for (Emails page, campaign detail, Overview) is
// then verified via source-inspection, same pattern as mail-campaigns.test.ts.

const NOW = new Date("2026-09-17T12:00:00Z");

// --- estimatedExpiryLabel -----------------------------------------------------

test("estimatedExpiryLabel returns null when there is no estimate", () => {
  assert.equal(estimatedExpiryLabel(null, NOW), null);
});

test("estimatedExpiryLabel rounds to hours when under a day away", () => {
  const in18h = new Date(NOW.getTime() + 18 * 60 * 60 * 1000).toISOString();
  assert.match(estimatedExpiryLabel(in18h, NOW)!, /expires in approximately 18 hours/);
});

test("estimatedExpiryLabel rounds to days when a day or more away", () => {
  const in3d = new Date(NOW.getTime() + 3 * 24 * 60 * 60 * 1000).toISOString();
  assert.match(estimatedExpiryLabel(in3d, NOW)!, /expires in approximately 3 days/);
});

test("estimatedExpiryLabel handles an already-past estimate", () => {
  const twoHoursAgo = new Date(NOW.getTime() - 2 * 60 * 60 * 1000).toISOString();
  assert.match(estimatedExpiryLabel(twoHoursAgo, NOW)!, /expired approximately 2 hours/);
});

test("estimatedExpiryLabel uses singular hour/day correctly", () => {
  const in1h = new Date(NOW.getTime() + 60 * 60 * 1000).toISOString();
  assert.match(estimatedExpiryLabel(in1h, NOW)!, /1 hour\./);
  const in1d = new Date(NOW.getTime() + 24 * 60 * 60 * 1000).toISOString();
  assert.match(estimatedExpiryLabel(in1d, NOW)!, /1 day\./);
});

// --- formatAuthorizationAge ---------------------------------------------------

test("formatAuthorizationAge handles null", () => {
  assert.equal(formatAuthorizationAge(null), "—");
});

test("formatAuthorizationAge handles under an hour", () => {
  assert.equal(formatAuthorizationAge(60), "Less than an hour ago");
});

test("formatAuthorizationAge handles hours", () => {
  assert.equal(formatAuthorizationAge(5 * 3600), "5 hours ago");
});

test("formatAuthorizationAge handles days", () => {
  assert.equal(formatAuthorizationAge(6 * 24 * 3600), "6 days ago");
});

// --- reconnectRequiredBeforeNextSend ------------------------------------------

test("reconnectRequiredBeforeNextSend is false when either input is missing", () => {
  assert.equal(reconnectRequiredBeforeNextSend(null, "2026-09-20T00:00:00Z"), false);
  assert.equal(reconnectRequiredBeforeNextSend("2026-09-20T00:00:00Z", null), false);
});

test("reconnectRequiredBeforeNextSend is true when the next send is after estimated expiry", () => {
  assert.equal(
    reconnectRequiredBeforeNextSend("2026-09-20T00:00:00Z", "2026-09-22T00:00:00Z"),
    true
  );
});

test("reconnectRequiredBeforeNextSend is false when the next send is before estimated expiry", () => {
  assert.equal(
    reconnectRequiredBeforeNextSend("2026-09-20T00:00:00Z", "2026-09-18T00:00:00Z"),
    false
  );
});

// --- label/badge coverage for all three states + null -------------------------

test("mailboxAuthorizationHealthLabel covers connected/reconnect_soon/needs_reauth/null", () => {
  assert.equal(mailboxAuthorizationHealthLabel("connected"), "Connected");
  assert.equal(mailboxAuthorizationHealthLabel("reconnect_soon"), "Reconnect soon");
  assert.equal(mailboxAuthorizationHealthLabel("needs_reauth"), "Needs reauthorization");
  assert.equal(mailboxAuthorizationHealthLabel(null), "");
});

test("mailboxAuthorizationHealthBadgeClass gives a distinct class per state", () => {
  const classes = new Set([
    mailboxAuthorizationHealthBadgeClass("connected"),
    mailboxAuthorizationHealthBadgeClass("reconnect_soon"),
    mailboxAuthorizationHealthBadgeClass("needs_reauth"),
    mailboxAuthorizationHealthBadgeClass(null),
  ]);
  assert.equal(classes.size, 4);
});

// --- Emails page wiring --------------------------------------------------------

const EMAILS_PAGE_SOURCE = readFileSync(new URL("../app/manager/emails/page.tsx", import.meta.url), "utf-8");
const RECONNECT_MODAL_SOURCE = readFileSync(new URL("../components/reconnect-mailbox-modal.tsx", import.meta.url), "utf-8");
const CAMPAIGN_DETAIL_SOURCE = readFileSync(
  new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url),
  "utf-8"
);
const OVERVIEW_PAGE_SOURCE = readFileSync(new URL("../app/manager/page.tsx", import.meta.url), "utf-8");
const HEALTH_WARNING_SOURCE = readFileSync(new URL("../components/mailbox-health-warning.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");
const MAILBOX_SERVICE_SOURCE = readFileSync(new URL("../../app/services/mailbox_service.py", import.meta.url), "utf-8");

test("the Emails page renders authorization health, last-authorized age, and an expiry sentence", () => {
  assert.match(EMAILS_PAGE_SOURCE, /mailboxAuthorizationHealthLabel/);
  assert.match(EMAILS_PAGE_SOURCE, /formatAuthorizationAge/);
  assert.match(EMAILS_PAGE_SOURCE, /estimatedExpiryLabel/);
});

test("the Emails page's Reconnect action works while the mailbox is still connected -- not gated on needs_reauth alone", () => {
  assert.match(EMAILS_PAGE_SOURCE, /reconnect_soon[\s\S]*setReconnectTarget|setReconnectTarget[\s\S]*reconnect_soon/);
});

test("the Emails page uses the dedicated ReconnectMailboxModal, not the full-upgrade modal, for routine reconnects", () => {
  assert.match(EMAILS_PAGE_SOURCE, /<ReconnectMailboxModal/);
});

test("ReconnectMailboxModal calls startGmailReconnect (the narrow, same-scopes flow), never startGmailSendUpgrade", () => {
  assert.match(RECONNECT_MODAL_SOURCE, /startGmailReconnect\(/);
  assert.doesNotMatch(RECONNECT_MODAL_SOURCE, /startGmailSendUpgrade/);
});

// --- Campaign detail wiring ----------------------------------------------------

test("the campaign detail page fetches campaign-scoped mailbox next-send data", () => {
  assert.match(CAMPAIGN_DETAIL_SOURCE, /getMailCampaignMailboxNextSend\(/);
});

test("the campaign detail page's mailbox-health banner is informational only -- it never calls a pause/archive/status-mutating action", () => {
  const bannerBlock = CAMPAIGN_DETAIL_SOURCE.slice(
    CAMPAIGN_DETAIL_SOURCE.indexOf("atRiskAssignedMailboxes.map"),
    CAMPAIGN_DETAIL_SOURCE.indexOf("{actionError &&")
  );
  assert.doesNotMatch(bannerBlock, /handlePause|handleArchive|updateMailCampaign|setMailCampaignChannels/);
});

test("the campaign detail page shows the stronger warning only when the next send is after estimated expiry", () => {
  assert.match(CAMPAIGN_DETAIL_SOURCE, /reconnectRequiredBeforeNextSend/);
  assert.match(CAMPAIGN_DETAIL_SOURCE, /Reconnect required before the next scheduled send/);
});

// --- Overview / global wiring ---------------------------------------------------

test("the Overview page renders the global mailbox-health warning", () => {
  assert.match(OVERVIEW_PAGE_SOURCE, /<MailboxHealthWarning/);
});

test("MailboxHealthWarning counts reconnect_soon and needs_reauth mailboxes and links to Emails", () => {
  assert.match(HEALTH_WARNING_SOURCE, /reconnect_soon/);
  assert.match(HEALTH_WARNING_SOURCE, /needs_reauth/);
  assert.match(HEALTH_WARNING_SOURCE, /href="\/manager\/emails"/);
});

test("MailboxHealthWarning fails silently on a load error -- never breaks the Overview page", () => {
  assert.match(HEALTH_WARNING_SOURCE, /\.catch\(/);
});

// --- API client ------------------------------------------------------------------

test("startGmailReconnect calls the narrow reconnect-start route", () => {
  assert.match(API_SOURCE, /`\/mailboxes\/\$\{mailboxId\}\/google\/gmail-reconnect\/start`/);
});

test("getMailCampaignMailboxNextSend calls the campaign-scoped mailbox-next-send route", () => {
  assert.match(API_SOURCE, /`\/mail\/campaigns\/\$\{mailCampaignId\}\/mailbox-next-send`/);
});

// --- Backend safety: sending/reply logic untouched ------------------------------

test("mailbox_service.py's reconnect flow never touches enrollment, engine, or reply-detection code", () => {
  assert.doesNotMatch(MAILBOX_SERVICE_SOURCE, /MailEnrollment|MailSendingService|ReplyDetection/);
});
