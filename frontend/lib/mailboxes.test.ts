import assert from "node:assert/strict";
import { test } from "node:test";
import type { Mailbox } from "./api.ts";
import {
  DELIVERABILITY_TOOLTIP,
  EMAIL_ACCOUNT_TABLE_COLUMNS,
  deriveTld,
  filterMailboxes,
  formatSendUsage,
  mailboxDisplayName,
  mailboxStatusBadgeClass,
  mailboxStatusLabel,
  providerLabel,
} from "./mailboxes.ts";

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

// --- mailboxDisplayName ------------------------------------------------------

function makeMailbox(overrides: Partial<Mailbox> = {}): Mailbox {
  return {
    mailbox_id: "mb-1",
    provider: "google",
    email: "chris@astronomic.io",
    display_name: "Chris Beaman",
    status: "connected",
    google_user_id: "google-sub-1",
    granted_scopes: ["openid", "email", "profile"],
    connected_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
    disconnected_at: null,
    ...overrides,
  };
}

test("mailboxDisplayName uses the Google display name when present", () => {
  assert.equal(mailboxDisplayName(makeMailbox({ display_name: "Chris Beaman" })), "Chris Beaman");
});

test("mailboxDisplayName falls back to the email when there is no display name", () => {
  assert.equal(mailboxDisplayName(makeMailbox({ display_name: null, email: "chris@astronomic.io" })), "chris@astronomic.io");
});

test("mailboxDisplayName falls back to the email for an empty-string display name", () => {
  assert.equal(mailboxDisplayName(makeMailbox({ display_name: "", email: "chris@astronomic.io" })), "chris@astronomic.io");
});

// --- filterMailboxes ---------------------------------------------------------

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

test("filterMailboxes matches a null-display-name mailbox by its email fallback", () => {
  const mailboxes = [makeMailbox({ display_name: null, email: "karla@astronomic.io" })];
  assert.equal(filterMailboxes(mailboxes, "karla").length, 1);
});

test("filterMailboxes excludes non-matching rows", () => {
  const mailboxes = [makeMailbox({ display_name: "Karla Alvarez", email: "karla@astronomic.io" })];
  assert.equal(filterMailboxes(mailboxes, "brendan").length, 0);
});

test("filterMailboxes against an empty list always returns an empty list", () => {
  assert.deepEqual(filterMailboxes([], "anything"), []);
});

// --- Labels / badges ---------------------------------------------------------

test("providerLabel renders Google Workspace for the only real V1 provider", () => {
  assert.equal(providerLabel("google"), "Google Workspace");
});

test("mailboxStatusLabel covers every real status", () => {
  assert.equal(mailboxStatusLabel("connected"), "Connected");
  assert.equal(mailboxStatusLabel("needs_reauth"), "Needs Reauthorization");
  assert.equal(mailboxStatusLabel("disconnected"), "Disconnected");
});

test("mailboxStatusBadgeClass returns a non-empty class for every status", () => {
  for (const status of ["connected", "needs_reauth", "disconnected"] as const) {
    assert.ok(mailboxStatusBadgeClass(status).length > 0);
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
