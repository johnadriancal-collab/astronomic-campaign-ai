import { test } from "node:test";
import assert from "node:assert/strict";
import {
  contactResultsMode,
  contactResultsSummaryText,
  formatContactLocation,
  formatContactName,
  formatContactTitleCompany,
} from "./contact-results-view.ts";

// --- contactResultsMode ---
// Requirement 1: Astro Search hides pagination but (as of the full-
// selection/export feature) DOES show selection chrome -- the two are
// independent, not one combined "simple" flag.

test("Astro Search shows selection controls but still hides pagination", () => {
  const mode = contactResultsMode({ hidePagination: true });
  assert.equal(mode.showSelectionChrome, true);
  assert.equal(mode.showPagination, false);
});

test("hiding selection alone leaves pagination visible", () => {
  const mode = contactResultsMode({ hideSelection: true });
  assert.equal(mode.showSelectionChrome, false);
  assert.equal(mode.showPagination, true);
});

// Requirement 2: the normal Contacts/More Filters usage (no flags passed --
// those pages pass neither) keeps its existing controls.

test("normal mode (CRM Contacts / More Filters, no flags) shows both selection chrome and pagination", () => {
  const mode = contactResultsMode();
  assert.equal(mode.showSelectionChrome, true);
  assert.equal(mode.showPagination, true);
});

// --- contactResultsSummaryText ---
// Result count must still display regardless of pagination visibility (just
// worded differently -- hidden pagination has no real page to report a
// range within).

test("hidden-pagination mode reports a plain count when everything matching is rendered", () => {
  const text = contactResultsSummaryText({ hidePagination: true, total: 4, page: 1, pageSize: 50, renderedCount: 4 });
  assert.equal(text, "4 contacts");
});

test("hidden-pagination mode notes how many are shown when fewer than the total are rendered (Astro Search)", () => {
  const text = contactResultsSummaryText({ hidePagination: true, total: 127, page: 1, pageSize: 50, renderedCount: 50 });
  assert.equal(text, "127 contacts (showing the first 50)");
});

test("hidden-pagination mode pluralizes a single contact correctly", () => {
  const text = contactResultsSummaryText({ hidePagination: true, total: 1, page: 1, pageSize: 50, renderedCount: 1 });
  assert.equal(text, "1 contact");
});

test("normal mode reports the paginated range", () => {
  const text = contactResultsSummaryText({ hidePagination: false, total: 89, page: 1, pageSize: 50 });
  assert.equal(text, "Showing 1–50 of 89 contacts");
});

test("normal mode's range reflects the current page, not always page 1", () => {
  const text = contactResultsSummaryText({ hidePagination: false, total: 89, page: 2, pageSize: 50 });
  assert.equal(text, "Showing 51–89 of 89 contacts");
});

test("both modes report nothing when there are zero total contacts", () => {
  assert.equal(contactResultsSummaryText({ hidePagination: true, total: 0, page: 1, pageSize: 50, renderedCount: 0 }), null);
  assert.equal(contactResultsSummaryText({ hidePagination: false, total: 0, page: 1, pageSize: 50 }), null);
});

// --- Contact rendering fields ---
// Requirement 3: these are the exact functions the card grid calls in BOTH
// modes -- there is no simple-mode branch in the formatting logic itself, so
// proving them here proves contact rendering is identical regardless of mode.

test("formatContactName joins first and last name", () => {
  assert.equal(formatContactName({ first_name: "Ada", last_name: "Lovelace" }), "Ada Lovelace");
});

test("formatContactName falls back to 'Unnamed contact' when both names are missing", () => {
  assert.equal(formatContactName({ first_name: null, last_name: null }), "Unnamed contact");
});

test("formatContactName handles a missing last name gracefully", () => {
  assert.equal(formatContactName({ first_name: "Ada", last_name: null }), "Ada");
});

test("formatContactLocation joins city and state", () => {
  assert.equal(formatContactLocation({ city: "Austin", state: "Texas" }), "Austin, Texas");
});

test("formatContactLocation handles a missing state", () => {
  assert.equal(formatContactLocation({ city: "Austin", state: null }), "Austin");
});

test("formatContactLocation is an empty string when both are missing", () => {
  assert.equal(formatContactLocation({ city: null, state: null }), "");
});

test("formatContactTitleCompany joins title and company with '@'", () => {
  assert.equal(formatContactTitleCompany({ title: "CEO", company: "Astronomic" }), "CEO @ Astronomic");
});

test("formatContactTitleCompany falls back when both are missing", () => {
  assert.equal(formatContactTitleCompany({ title: null, company: null }), "No title/company on file");
});
