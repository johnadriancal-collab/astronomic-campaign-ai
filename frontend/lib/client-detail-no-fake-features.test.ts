import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Stage 1C's Client detail page must not fabricate data for features that
// don't exist yet (Pipeline, follow-up automation, analytics) -- scanned
// directly from source, same "no component-render harness" convention as
// sidebar-nav-source.test.ts. Stage 1D adds a REAL Contacts section (the
// ClientContact linking feature actually exists now), so "no fake
// Contacts" is no longer part of this guard -- see the Contacts-specific
// tests below instead.

const CLIENT_DETAIL_PAGE = readFileSync(new URL("../app/clients/[id]/page.tsx", import.meta.url), "utf-8");

test("no hardcoded/fake tab navigation for Dinners/Deals exists yet", () => {
  for (const forbidden of [/>Dinners</, />Deals</, /TabsTab/]) {
    assert.doesNotMatch(CLIENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated counts/placeholders for unbuilt features", () => {
  for (const forbidden of [/dinner count/i, /revenue/i, /follow-up status/i]) {
    assert.doesNotMatch(CLIENT_DETAIL_PAGE, forbidden);
  }
});

test("no hard-delete control exists on the Client detail page", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /Delete Client/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /deleteClient/);
});

test("Archive/Restore is present, distinct from any delete action", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Archive Client/);
  assert.match(CLIENT_DETAIL_PAGE, /Restore Client/);
});

// --- Stage 1D: real Contacts section --------------------------------------

test("a real Contacts section is present, using the shared CrmContactPicker, not a free-text field", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Contacts/);
  assert.match(CLIENT_DETAIL_PAGE, /Add Contact/);
  assert.match(CLIENT_DETAIL_PAGE, /Set Primary/);
});

test("no hard-delete control exists for a ClientContact relationship either", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /Delete Contact/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /deleteClientContact/);
});

test("a linked Contact links back to its canonical /crm/{id} record", () => {
  assert.match(CLIENT_DETAIL_PAGE, /\/crm\/\$\{contact\.crm_contact_id\}/);
});
