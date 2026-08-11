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
// Requirement 1: Astro Search (simple=true) hides pagination/selection chrome.

test("simple mode (Astro Search) hides both selection chrome and pagination", () => {
  const mode = contactResultsMode(true);
  assert.equal(mode.showSelectionChrome, false);
  assert.equal(mode.showPagination, false);
});

// Requirement 2: the normal Contacts/More Filters usage (simple=false, the
// default -- those pages never pass `simple`) keeps its existing controls.

test("normal mode (CRM Contacts / More Filters) shows both selection chrome and pagination", () => {
  const mode = contactResultsMode(false);
  assert.equal(mode.showSelectionChrome, true);
  assert.equal(mode.showPagination, true);
});

// --- contactResultsSummaryText ---
// Result count must still display in BOTH modes (just worded differently --
// simple mode has no real page to report a range within).

test("simple mode reports a plain count, not a page range", () => {
  const text = contactResultsSummaryText({ simple: true, total: 4, page: 1, pageSize: 50 });
  assert.equal(text, "4 contacts");
});

test("simple mode pluralizes a single contact correctly", () => {
  const text = contactResultsSummaryText({ simple: true, total: 1, page: 1, pageSize: 50 });
  assert.equal(text, "1 contact");
});

test("normal mode reports the paginated range", () => {
  const text = contactResultsSummaryText({ simple: false, total: 89, page: 1, pageSize: 50 });
  assert.equal(text, "Showing 1–50 of 89 contacts");
});

test("normal mode's range reflects the current page, not always page 1", () => {
  const text = contactResultsSummaryText({ simple: false, total: 89, page: 2, pageSize: 50 });
  assert.equal(text, "Showing 51–89 of 89 contacts");
});

test("both modes report nothing when there are zero total contacts", () => {
  assert.equal(contactResultsSummaryText({ simple: true, total: 0, page: 1, pageSize: 50 }), null);
  assert.equal(contactResultsSummaryText({ simple: false, total: 0, page: 1, pageSize: 50 }), null);
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
