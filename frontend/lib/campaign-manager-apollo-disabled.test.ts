import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Regression coverage for disabling Apollo Campaign/Sequence integration in
// the Campaign Manager product surface. Apollo enrichment/contact-research
// code (app/apollo/people.py, contacts.py, lists.py, client.py) is
// untouched and out of scope for these tests -- this file only guards the
// Campaign Manager list page's presentation.
//
// These are source-level assertions (no DOM render harness exists in this
// project -- see package.json's test script, which runs plain
// lib/*.test.ts files via node --test, never against a rendered page), the
// same pattern as astro-campaign-isolation.test.ts.

const CAMPAIGNS_PAGE_SOURCE = readFileSync(
  new URL("../app/manager/campaigns/page.tsx", import.meta.url),
  "utf-8"
);

test("the campaigns list page no longer triggers an Apollo sync on load", () => {
  assert.doesNotMatch(CAMPAIGNS_PAGE_SOURCE, /syncCampaigns/);
});

test("the campaigns list page has no 'Synced'/'Syncing' Apollo sync indicator", () => {
  assert.doesNotMatch(CAMPAIGNS_PAGE_SOURCE, /Synced|Syncing/);
});

test("the campaigns list page no longer has the old 'both sending methods' copy", () => {
  assert.doesNotMatch(CAMPAIGNS_PAGE_SOURCE, /both sending methods/);
  assert.doesNotMatch(CAMPAIGNS_PAGE_SOURCE, /Apollo and Astronomic Mail/);
});

test("the campaigns list page uses the new Astronomic-Mail-only description copy", () => {
  assert.match(CAMPAIGNS_PAGE_SOURCE, /Create and manage your Astronomic Mail campaigns\./);
});

test("the campaigns list page no longer renders a sending-method badge", () => {
  assert.doesNotMatch(CAMPAIGNS_PAGE_SOURCE, /SendingMethodBadge/);
});

test("the campaigns list page still renders native campaign cards with a status badge", () => {
  assert.match(CAMPAIGNS_PAGE_SOURCE, /StatusBadge/);
  assert.match(CAMPAIGNS_PAGE_SOURCE, /listUnifiedCampaigns/);
});
