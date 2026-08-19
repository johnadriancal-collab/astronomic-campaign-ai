import assert from "node:assert/strict";
import { test } from "node:test";
import {
  deliverabilityBadgeClass,
  deliverabilityLabel,
  deriveTld,
  DELIVERABILITY_TOOLTIP,
  EMAIL_ACCOUNT_TABLE_COLUMNS,
  filterMailboxes,
  formatSendUsage,
  MAILBOX_ACCOUNTS,
  providerLabel,
  type MailboxAccount,
} from "./mailboxes.ts";

// --- No fabricated data ----------------------------------------------------

test("MAILBOX_ACCOUNTS is empty -- no mailbox model/connection exists yet", () => {
  assert.deepEqual(MAILBOX_ACCOUNTS, []);
});

// --- Exact, approved column set ---------------------------------------------

test("EMAIL_ACCOUNT_TABLE_COLUMNS is exactly the 8 approved columns, in order", () => {
  assert.deepEqual(EMAIL_ACCOUNT_TABLE_COLUMNS, [
    "Name",
    "Email",
    "TLD",
    "Provider",
    "Deliverability Index",
    "Campaigns",
    "Emails Sent Today",
    "Queue",
  ]);
});

test("Signature, Custom Domain, and Smart Sending Groups are NOT columns", () => {
  const columns: readonly string[] = EMAIL_ACCOUNT_TABLE_COLUMNS;
  assert.ok(!columns.includes("Signature"));
  assert.ok(!columns.includes("Custom Domain"));
  assert.ok(!columns.includes("Smart Sending Groups"));
});

// --- deriveTld ---------------------------------------------------------------

test("deriveTld extracts the last domain segment for ordinary addresses", () => {
  assert.equal(deriveTld("brendan@bizdevdinners.com"), "com");
  assert.equal(deriveTld("chris@astronomic.io"), "io");
  assert.equal(deriveTld("team@constellationdinners.ai"), "ai");
});

test("deriveTld handles a multi-level domain", () => {
  assert.equal(deriveTld("chris@mail.astronomic.co.uk"), "uk");
});

test("deriveTld lowercases the result", () => {
  assert.equal(deriveTld("chris@Astronomic.IO"), "io");
});

test("deriveTld returns null for unusual/invalid domains rather than throwing", () => {
  assert.equal(deriveTld("not-an-email"), null);
  assert.equal(deriveTld("chris@"), null);
  assert.equal(deriveTld("chris@localhost"), null);
  assert.equal(deriveTld(""), null);
});

// --- filterMailboxes ---------------------------------------------------------

function makeMailbox(overrides: Partial<MailboxAccount> = {}): MailboxAccount {
  return {
    mailbox_id: "mb-1",
    display_name: "Chris Beaman",
    email: "chris@astronomic.io",
    provider: "google_workspace",
    deliverability_status: "unknown",
    deliverability_score: null,
    campaign_count: 0,
    emails_sent_today: 0,
    daily_send_limit: null,
    queue_count: 0,
    connected_at: null,
    ...overrides,
  };
}

test("filterMailboxes with an empty query returns every mailbox unchanged", () => {
  const mailboxes = [makeMailbox(), makeMailbox({ mailbox_id: "mb-2", display_name: "Karla Alvarez" })];
  assert.deepEqual(filterMailboxes(mailboxes, ""), mailboxes);
  assert.deepEqual(filterMailboxes(mailboxes, "   "), mailboxes);
});

test("filterMailboxes matches by display name, case-insensitively", () => {
  const mailboxes = [makeMailbox({ display_name: "Karla Alvarez" })];
  assert.equal(filterMailboxes(mailboxes, "karla").length, 1);
  assert.equal(filterMailboxes(mailboxes, "KARLA").length, 1);
});

test("filterMailboxes matches by email, case-insensitively", () => {
  const mailboxes = [makeMailbox({ email: "chris@astronomic.io" })];
  assert.equal(filterMailboxes(mailboxes, "astronomic").length, 1);
  assert.equal(filterMailboxes(mailboxes, "ASTRONOMIC.IO").length, 1);
});

test("filterMailboxes excludes non-matching rows", () => {
  const mailboxes = [makeMailbox({ display_name: "Karla Alvarez", email: "karla@astronomic.io" })];
  assert.equal(filterMailboxes(mailboxes, "brendan").length, 0);
});

test("filterMailboxes against an empty list always returns an empty list", () => {
  assert.deepEqual(filterMailboxes([], "anything"), []);
});

// --- Labels / badges -- neutral state, no fabricated score -----------------

test("providerLabel renders Google Workspace for the only real V1 provider", () => {
  assert.equal(providerLabel("google_workspace"), "Google Workspace");
});

test("deliverabilityLabel shows a neutral dash for 'unknown', not a fake score", () => {
  assert.equal(deliverabilityLabel("unknown"), "—");
});

test("deliverabilityLabel has real labels ready for when a metric exists", () => {
  assert.equal(deliverabilityLabel("good"), "Good");
  assert.equal(deliverabilityLabel("warning"), "Warning");
  assert.equal(deliverabilityLabel("poor"), "Poor");
});

test("deliverabilityBadgeClass returns a non-empty class for every status", () => {
  for (const status of ["good", "warning", "poor", "unknown"] as const) {
    assert.ok(deliverabilityBadgeClass(status).length > 0);
  }
});

test("DELIVERABILITY_TOOLTIP explains the neutral state honestly", () => {
  assert.equal(DELIVERABILITY_TOOLTIP, "Deliverability monitoring will be added later.");
});

// --- formatSendUsage ---------------------------------------------------------

test("formatSendUsage shows a plain count with no limit configured", () => {
  assert.equal(formatSendUsage(0, null), "0");
});

test("formatSendUsage shows 'sent / limit' once a real limit exists", () => {
  assert.equal(formatSendUsage(24, 50), "24 / 50");
});
