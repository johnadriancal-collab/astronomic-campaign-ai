import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Stage 1C's Client detail page must not fabricate data for features that
// don't exist yet (Pipeline, follow-up automation, analytics) -- scanned
// directly from source, same "no component-render harness" convention as
// sidebar-nav-source.test.ts. Stage 1D added a REAL Contacts section and
// Stage 1E adds a REAL Engagements section (the underlying features
// actually exist now), so "no fake Contacts"/"no fake Engagements" is not
// part of this guard -- see their own dedicated tests below instead.

const CLIENT_DETAIL_PAGE = readFileSync(new URL("../app/clients/[id]/page.tsx", import.meta.url), "utf-8");

test("no hardcoded/fake tab navigation for Deals exists yet", () => {
  for (const forbidden of [/>Deals</, /TabsTab/]) {
    assert.doesNotMatch(CLIENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated counts/placeholders for unbuilt features", () => {
  for (const forbidden of [/revenue/i, /follow-up status/i]) {
    assert.doesNotMatch(CLIENT_DETAIL_PAGE, forbidden);
  }
});

test("no fabricated Closeout/Day 3/30/90 data anywhere on the Client page", () => {
  for (const forbidden of [/Closeout/, /Day 3\b/, /Day 30/, /Day 90/, /turnout/i, /guest quality/i, /dinner dynamics/i]) {
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

// --- Stage 1E: real Engagements section ------------------------------------

test("a real Engagements section is present, linking to the Engagement detail route", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Engagements/);
  assert.match(CLIENT_DETAIL_PAGE, /Add Engagement/);
  assert.match(CLIENT_DETAIL_PAGE, /\/clients\/\$\{client\.client_id\}\/engagements\/\$\{engagement\.engagement_id\}/);
});

test("no hard-delete control exists for an Engagement either", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /Delete Engagement/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /deleteClientEngagement/);
});

// --- Stage 2B: real Touchpoints section (Last Contact/Log Touchpoint/History) ---

test("a real Touchpoints section is present, deriving Last Contact from real loaded data, not a fake field", () => {
  assert.match(CLIENT_DETAIL_PAGE, /Touchpoints/);
  assert.match(CLIENT_DETAIL_PAGE, /Log Touchpoint/);
  assert.match(CLIENT_DETAIL_PAGE, /latestActiveTouchpoint\(touchpoints\)/);
});

test("no hard-delete control exists for a Touchpoint either", () => {
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /Delete Touchpoint/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /deleteClientTouchpoint/);
});

// --- Layout widening -- desktop horizontal space use ------------------------

test("the page uses the shared, widened Client CRM detail container -- no leftover narrow max-w-3xl literal", () => {
  assert.match(CLIENT_DETAIL_PAGE, /CLIENT_CRM_DETAIL_CONTAINER_CLASS/);
  assert.doesNotMatch(CLIENT_DETAIL_PAGE, /max-w-3xl/);
});

test("Overview/Client Information and Contacts use the shared two-column desktop grid", () => {
  assert.match(CLIENT_DETAIL_PAGE, /CLIENT_OVERVIEW_CONTACTS_GRID_CLASS/);
});

test("Touchpoints and Engagements stay outside the two-column grid -- full width, unaffected by the layout change", () => {
  const gridStart = CLIENT_DETAIL_PAGE.indexOf("CLIENT_OVERVIEW_CONTACTS_GRID_CLASS");
  const touchpointsIndex = CLIENT_DETAIL_PAGE.indexOf(">Touchpoints<");
  const engagementsIndex = CLIENT_DETAIL_PAGE.indexOf(">Engagements<");
  assert.ok(gridStart > -1 && touchpointsIndex > gridStart && engagementsIndex > touchpointsIndex);
});
