import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CRM_CONTACT_DETAIL_CONTAINER_CLASS,
  OVERVIEW_EVENT_HISTORY_GRID_CLASS,
  addToListSelectedIds,
} from "./crm-contact-detail-layout.ts";

// --- widened container ------------------------------------------------

test("the contact detail container is widened off the old narrow max-w-3xl", () => {
  assert.ok(CRM_CONTACT_DETAIL_CONTAINER_CLASS.includes("max-w-6xl"));
  assert.ok(!CRM_CONTACT_DETAIL_CONTAINER_CLASS.includes("max-w-3xl"));
});

test("the container matches this app's own established wide-detail-page convention", () => {
  // Same value as lib/mail-campaign-layout.ts's MAIL_CAMPAIGN_DETAIL_CONTAINER_CLASS
  // -- not an invented width.
  assert.equal(CRM_CONTACT_DETAIL_CONTAINER_CLASS, "mx-auto max-w-6xl px-6 py-10");
});

test("the container keeps real horizontal padding and stays centered, not edge-to-edge", () => {
  assert.ok(CRM_CONTACT_DETAIL_CONTAINER_CLASS.includes("px-6"));
  assert.ok(CRM_CONTACT_DETAIL_CONTAINER_CLASS.includes("mx-auto"));
});

// --- Overview / Event History responsive two-column layout -------------

test("Overview and Event History stack to a single column by default (mobile-first base class)", () => {
  assert.ok(OVERVIEW_EVENT_HISTORY_GRID_CLASS.includes("grid-cols-1"));
});

test("the two-column split only ever applies at the lg breakpoint and above", () => {
  assert.match(OVERVIEW_EVENT_HISTORY_GRID_CLASS, /(?:^|\s)lg:grid-cols-/);
});

test("the base grid-cols-1 is never overridden below lg -- no sm:/md: column change exists", () => {
  assert.ok(!/(?:^|\s)sm:grid-cols-/.test(OVERVIEW_EVENT_HISTORY_GRID_CLASS));
  assert.ok(!/(?:^|\s)md:grid-cols-/.test(OVERVIEW_EVENT_HISTORY_GRID_CLASS));
});

test("the desktop split slightly favors Overview over Event History (3fr vs 2fr)", () => {
  assert.ok(OVERVIEW_EVENT_HISTORY_GRID_CLASS.includes("[3fr_2fr]"));
});

test("cards stretch to equal height on desktop -- no items-start override at any breakpoint", () => {
  // CSS Grid's own default (align-items: stretch) is what we rely on here;
  // an explicit items-start anywhere would defeat equal-height stretching.
  assert.ok(!OVERVIEW_EVENT_HISTORY_GRID_CLASS.includes("items-start"));
});

// --- Add to List always targets exactly this one contact ----------------

test("Add to List on the contact detail page targets exactly this one contact", () => {
  assert.deepEqual(addToListSelectedIds({ crm_contact_id: "abc-123" }), ["abc-123"]);
});

test("a different contact produces a different, still single-element selection", () => {
  const result = addToListSelectedIds({ crm_contact_id: "xyz-999" });
  assert.deepEqual(result, ["xyz-999"]);
  assert.equal(result.length, 1);
});
