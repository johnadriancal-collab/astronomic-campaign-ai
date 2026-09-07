import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Stage 1C's Client detail page must not fabricate data for features that
// don't exist yet (ClientContact/Engagement/ClientNote CRUD, Pipeline,
// follow-up automation, analytics) -- scanned directly from source, same
// "no component-render harness" convention as sidebar-nav-source.test.ts.

const CLIENT_DETAIL_PAGE = readFileSync(new URL("../app/clients/[id]/page.tsx", import.meta.url), "utf-8");

test("no hardcoded/fake tab navigation for Contacts, Dinners, or Activity exists yet", () => {
  for (const forbidden of [/>Contacts</, />Dinners</, />Deals</, /TabsTab/]) {
    assert.doesNotMatch(CLIENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated counts/placeholders for unbuilt features", () => {
  for (const forbidden of [/contact count/i, /dinner count/i, /revenue/i, /follow-up status/i]) {
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
