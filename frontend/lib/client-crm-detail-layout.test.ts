import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CLIENT_CRM_DETAIL_CONTAINER_CLASS,
  CLIENT_OVERVIEW_CONTACTS_GRID_CLASS,
  ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS,
} from "./client-crm-detail-layout.ts";

// --- widened container ------------------------------------------------

test("the Client CRM detail container is widened off the old narrow max-w-3xl", () => {
  assert.ok(CLIENT_CRM_DETAIL_CONTAINER_CLASS.includes("max-w-6xl"));
  assert.ok(!CLIENT_CRM_DETAIL_CONTAINER_CLASS.includes("max-w-3xl"));
});

test("the container matches this app's own established wide-detail-page convention", () => {
  // Same value as lib/crm-contact-detail-layout.ts's CRM_CONTACT_DETAIL_CONTAINER_CLASS
  // and lib/mail-campaign-layout.ts's MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS --
  // not an invented width.
  assert.equal(CLIENT_CRM_DETAIL_CONTAINER_CLASS, "mx-auto max-w-6xl px-6 py-10");
});

test("the container keeps real horizontal padding and stays centered, not edge-to-edge", () => {
  assert.ok(CLIENT_CRM_DETAIL_CONTAINER_CLASS.includes("px-6"));
  assert.ok(CLIENT_CRM_DETAIL_CONTAINER_CLASS.includes("mx-auto"));
});

// --- Client detail page: Overview+Client Information / Contacts split ---

test("Overview+Contacts stack to a single column by default (mobile-first base class)", () => {
  assert.ok(CLIENT_OVERVIEW_CONTACTS_GRID_CLASS.includes("grid-cols-1"));
});

test("the Client page's two-column split only ever applies at the lg breakpoint and above", () => {
  assert.match(CLIENT_OVERVIEW_CONTACTS_GRID_CLASS, /(?:^|\s)lg:grid-cols-/);
});

test("the Client page's base grid-cols-1 is never overridden below lg -- no sm:/md: column change exists", () => {
  assert.ok(!/(?:^|\s)sm:grid-cols-/.test(CLIENT_OVERVIEW_CONTACTS_GRID_CLASS));
  assert.ok(!/(?:^|\s)md:grid-cols-/.test(CLIENT_OVERVIEW_CONTACTS_GRID_CLASS));
});

test("the Client page's desktop split favors the primary info column over the compact Contacts list (3fr vs 2fr)", () => {
  assert.ok(CLIENT_OVERVIEW_CONTACTS_GRID_CLASS.includes("[3fr_2fr]"));
});

test("Client page columns stretch to equal height on desktop -- no items-start override at any breakpoint", () => {
  assert.ok(!CLIENT_OVERVIEW_CONTACTS_GRID_CLASS.includes("items-start"));
});

// --- Engagement detail page: Overview / Linked Luma Event split ---------

test("Overview+Linked Luma Event stack to a single column by default (mobile-first base class)", () => {
  assert.ok(ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS.includes("grid-cols-1"));
});

test("the Engagement page's two-column split only ever applies at the lg breakpoint and above", () => {
  assert.match(ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS, /(?:^|\s)lg:grid-cols-/);
});

test("the Engagement page's base grid-cols-1 is never overridden below lg -- no sm:/md: column change exists", () => {
  assert.ok(!/(?:^|\s)sm:grid-cols-/.test(ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS));
  assert.ok(!/(?:^|\s)md:grid-cols-/.test(ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS));
});

test("the Engagement page's Overview/Linked Luma Event split is an even 2-column grid -- neither card is treated as more compact than the other", () => {
  assert.match(ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS, /(?:^|\s)lg:grid-cols-2(?:\s|$)/);
});

test("Engagement page columns stretch to equal height on desktop -- no items-start override at any breakpoint", () => {
  assert.ok(!ENGAGEMENT_OVERVIEW_LUMA_GRID_CLASS.includes("items-start"));
});
