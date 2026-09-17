import assert from "node:assert/strict";
import { test } from "node:test";
import type { Mailbox } from "./api.ts";
import {
  DELIVERABILITY_TOOLTIP,
  EMAIL_ACCOUNT_TABLE_COLUMNS,
  GMAIL_METADATA_SCOPE,
  GMAIL_READONLY_SCOPE,
  GMAIL_SEND_SCOPE,
  deriveTld,
  filterMailboxes,
  formatSendUsage,
  gmailSendUpgradeState,
  hasGmailMetadataScope,
  hasGmailReadonlyScope,
  hasGmailSendScope,
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

// --- Gmail-send upgrade state --------------------------------------------------
// Matches app/google/oauth_client.py's SCOPES/GMAIL_SEND_SCOPE and
// app/services/mailbox_service.py's begin_gmail_send_upgrade() contract --
// derived ENTIRELY from status + granted_scopes, never a separate flag.

test("GMAIL_SEND_SCOPE matches the backend's exact scope string", () => {
  assert.equal(GMAIL_SEND_SCOPE, "https://www.googleapis.com/auth/gmail.send");
});

test("hasGmailSendScope is false for a base-scope-only mailbox", () => {
  assert.equal(hasGmailSendScope(makeMailbox({ granted_scopes: ["openid", "email", "profile"] })), false);
});

test("hasGmailSendScope is true once gmail.send is present, alongside the base scopes", () => {
  assert.equal(
    hasGmailSendScope(makeMailbox({ granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE] })),
    true
  );
});

test("gmailSendUpgradeState is 'can_enable' for a connected, base-scope-only mailbox", () => {
  const mailbox = makeMailbox({ status: "connected", granted_scopes: ["openid", "email", "profile"] });
  assert.equal(gmailSendUpgradeState(mailbox), "can_enable");
});

test("hasGmailMetadataScope is false for a base-scope-only mailbox", () => {
  assert.equal(hasGmailMetadataScope(makeMailbox({ granted_scopes: ["openid", "email", "profile"] })), false);
});

test("hasGmailMetadataScope is true once gmail.metadata is present", () => {
  assert.equal(
    hasGmailMetadataScope(makeMailbox({ granted_scopes: ["openid", "email", "profile", GMAIL_METADATA_SCOPE] })),
    true
  );
});

test("gmailSendUpgradeState is 'can_enable' -- NOT 'enabled' -- for a mailbox with gmail.send but not yet gmail.metadata", () => {
  // The exact gap this test guards against: a mailbox granted gmail.send
  // before gmail.metadata existed as a requested scope must still show
  // the upgrade action, or there is no way to ever complete the upgrade
  // for it -- a real production incident this test reproduces.
  const mailbox = makeMailbox({ status: "connected", granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE] });
  assert.equal(gmailSendUpgradeState(mailbox), "can_enable");
});

test("gmailSendUpgradeState is 'can_enable' -- NOT 'enabled' -- for a mailbox with send+metadata but not yet gmail.readonly (Inbox V2, 2026-09-17)", () => {
  // The exact gap this test guards against, one more time: a mailbox
  // granted send+metadata before gmail.readonly existed as a requested
  // scope must still show the upgrade action -- a real production
  // incident (Victoria's mailbox) this test reproduces.
  const mailbox = makeMailbox({
    status: "connected",
    granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE],
  });
  assert.equal(gmailSendUpgradeState(mailbox), "can_enable");
});

test("hasGmailReadonlyScope is false until gmail.readonly is present", () => {
  assert.equal(
    hasGmailReadonlyScope(makeMailbox({ granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE] })),
    false
  );
  assert.equal(
    hasGmailReadonlyScope(
      makeMailbox({ granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE] })
    ),
    true
  );
});

test("gmailSendUpgradeState is 'enabled' only once ALL THREE of gmail.send, gmail.metadata, and gmail.readonly are present", () => {
  const mailbox = makeMailbox({
    status: "connected",
    granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE],
  });
  assert.equal(gmailSendUpgradeState(mailbox), "enabled");
});

test("gmailSendUpgradeState is 'needs_reconnect' for a needs_reauth mailbox regardless of granted_scopes", () => {
  const mailboxWithoutSendScope = makeMailbox({ status: "needs_reauth", granted_scopes: ["openid", "email", "profile"] });
  const mailboxWithAllThreeScopes = makeMailbox({
    status: "needs_reauth",
    granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE],
  });
  assert.equal(gmailSendUpgradeState(mailboxWithoutSendScope), "needs_reconnect");
  // Critically: even a mailbox that WAS granted all three scopes before
  // it needed reauth must not be shown as "enabled" -- it isn't currently usable.
  assert.equal(gmailSendUpgradeState(mailboxWithAllThreeScopes), "needs_reconnect");
});

test("gmailSendUpgradeState is 'needs_reconnect' for a disconnected mailbox", () => {
  const mailbox = makeMailbox({
    status: "disconnected",
    granted_scopes: ["openid", "email", "profile", GMAIL_SEND_SCOPE, GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE],
  });
  assert.equal(gmailSendUpgradeState(mailbox), "needs_reconnect");
});
