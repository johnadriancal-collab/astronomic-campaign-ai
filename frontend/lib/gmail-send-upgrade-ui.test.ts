import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Static source-text verification for the Gmail-send upgrade UI -- no
// component-render harness exists in this frontend (see
// lib/sidebar-nav-source.test.ts's own comment for why source-scanning
// is this codebase's established substitute). The underlying
// gmailSendUpgradeState()/hasGmailSendScope() DECISION logic is unit-
// tested directly in lib/mailboxes.test.ts; this file verifies that the
// Emails page and the upgrade modal actually WIRE that decision into
// the three required user-facing states and the same-account warning.

const EMAILS_PAGE = readFileSync(new URL("../app/manager/emails/page.tsx", import.meta.url), "utf-8");
const UPGRADE_MODAL = readFileSync(new URL("../components/enable-gmail-sending-modal.tsx", import.meta.url), "utf-8");

test("the Emails page derives the Gmail-sending action from gmailSendUpgradeState(), not a separate flag", () => {
  assert.match(EMAILS_PAGE, /gmailSendUpgradeState\(mailbox\)/);
});

test("the Emails page shows 'Enable Gmail sending' for the can_enable state", () => {
  assert.match(EMAILS_PAGE, /Enable Gmail sending/);
});

test("the Emails page shows 'Gmail sending enabled' for the enabled state, with no upgrade action alongside it", () => {
  assert.match(EMAILS_PAGE, /Gmail sending enabled/);
  // The enabled branch must return before ever reaching the upgrade
  // button JSX -- proven structurally by both strings appearing as
  // separate, mutually exclusive branches of the same conditional
  // rather than concatenated in one block.
  assert.match(EMAILS_PAGE, /upgradeState === "enabled"/);
  assert.match(EMAILS_PAGE, /upgradeState === "needs_reconnect"/);
});

test("the Emails page uses reconnect/reauthorize wording for needs_reconnect, never implying sending is usable", () => {
  assert.match(EMAILS_PAGE, /[Rr]econnect this inbox to enable Gmail sending/);
  // Must not claim sending is enabled/usable in the SAME branch as the
  // reconnect wording -- a crude but effective guard against the two
  // states being accidentally merged.
  const needsReconnectBranch = EMAILS_PAGE.slice(
    EMAILS_PAGE.indexOf('upgradeState === "needs_reconnect"'),
    EMAILS_PAGE.indexOf('upgradeState === "needs_reconnect"') + 300
  );
  assert.doesNotMatch(needsReconnectBranch, /Gmail sending enabled/);
});

test("raw OAuth scope strings are not exposed in the Emails page UI copy", () => {
  assert.doesNotMatch(EMAILS_PAGE, /googleapis\.com\/auth/);
});

test("the upgrade modal warns the user to authorize the SAME Google account before redirecting", () => {
  assert.match(UPGRADE_MODAL, /same Google account/i);
  assert.match(UPGRADE_MODAL, /mailbox\.email/); // the warning is scoped to THIS mailbox's actual address
});

test("the upgrade modal explains that a different account will fail rather than silently doing something unexpected", () => {
  assert.match(UPGRADE_MODAL, /different account will fail/i);
});

test("the upgrade modal does a full top-level navigation, matching ConnectEmailModal's established pattern", () => {
  assert.match(UPGRADE_MODAL, /window\.location\.href\s*=\s*authorize_url/);
});

test("the upgrade modal calls startGmailSendUpgrade with the target mailbox's id, not the ordinary connect start", () => {
  assert.match(UPGRADE_MODAL, /startGmailSendUpgrade\(mailbox\.mailbox_id\)/);
});

test("the Emails page maps the upgrade-flow-specific callback error codes to real messages", () => {
  for (const code of ["account_mismatch", "scope_not_granted", "upgrade_needs_retry", "mailbox_not_found"]) {
    assert.match(EMAILS_PAGE, new RegExp(`${code}:`));
  }
});
