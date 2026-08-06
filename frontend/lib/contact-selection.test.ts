import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clearSelection,
  isPageFullySelected,
  isPagePartiallySelected,
  selectAllMatching,
  toggleOne,
  toggleSelectAllOnPage,
} from "./contact-selection.ts";

// --- toggleOne (individual checkbox) ---

test("toggleOne adds an id that isn't selected yet", () => {
  const result = toggleOne(new Set(), "c1");
  assert.deepEqual([...result], ["c1"]);
});

test("toggleOne removes an id that is already selected", () => {
  const result = toggleOne(new Set(["c1", "c2"]), "c1");
  assert.deepEqual([...result], ["c2"]);
});

test("toggleOne does not mutate the input set", () => {
  const original = new Set(["c1"]);
  toggleOne(original, "c2");
  assert.deepEqual([...original], ["c1"]);
});

// --- isPageFullySelected / isPagePartiallySelected (checkbox indeterminate state) ---

test("isPageFullySelected is true only when every id on the page is selected", () => {
  assert.equal(isPageFullySelected(new Set(["c1", "c2"]), ["c1", "c2"]), true);
  assert.equal(isPageFullySelected(new Set(["c1"]), ["c1", "c2"]), false);
});

test("isPageFullySelected is false for an empty page", () => {
  assert.equal(isPageFullySelected(new Set(), []), false);
});

test("isPagePartiallySelected is true when some but not all page ids are selected", () => {
  assert.equal(isPagePartiallySelected(new Set(["c1"]), ["c1", "c2", "c3"]), true);
});

test("isPagePartiallySelected is false when none of the page is selected", () => {
  assert.equal(isPagePartiallySelected(new Set(), ["c1", "c2"]), false);
});

test("isPagePartiallySelected is false when the whole page is selected", () => {
  assert.equal(isPagePartiallySelected(new Set(["c1", "c2"]), ["c1", "c2"]), false);
});

test("isPagePartiallySelected ignores selections outside the current page", () => {
  assert.equal(isPagePartiallySelected(new Set(["other-page-id"]), ["c1", "c2"]), false);
});

// --- toggleSelectAllOnPage ("Select all on this page") ---

test("selecting all on a page with none selected selects exactly that page", () => {
  const result = toggleSelectAllOnPage(new Set(), ["c1", "c2", "c3"]);
  assert.deepEqual([...result].sort(), ["c1", "c2", "c3"]);
});

test("toggling select-all-on-page again (already fully selected) deselects just that page", () => {
  const selected = new Set(["c1", "c2"]);
  const result = toggleSelectAllOnPage(selected, ["c1", "c2"]);
  assert.deepEqual([...result], []);
});

test("selecting all on page 2 preserves an existing selection carried over from page 1", () => {
  const selectedFromPage1 = new Set(["p1-a", "p1-b"]);
  const result = toggleSelectAllOnPage(selectedFromPage1, ["p2-a", "p2-b"]);
  assert.deepEqual([...result].sort(), ["p1-a", "p1-b", "p2-a", "p2-b"]);
});

test("deselecting a fully-selected page leaves other pages' selections intact", () => {
  const selected = new Set(["p1-a", "p1-b", "p2-a", "p2-b"]);
  const result = toggleSelectAllOnPage(selected, ["p2-a", "p2-b"]);
  assert.deepEqual([...result].sort(), ["p1-a", "p1-b"]);
});

test("select-all-on-page treats a page that is only partially selected as 'select the rest', not deselect", () => {
  const selected = new Set(["c1"]); // only one of three already selected
  const result = toggleSelectAllOnPage(selected, ["c1", "c2", "c3"]);
  assert.deepEqual([...result].sort(), ["c1", "c2", "c3"]);
});

// --- selectAllMatching ("Select all N matching contacts") ---

test("selectAllMatching selects every id passed in, spanning multiple pages", () => {
  const allMatchingIds = ["p1-a", "p1-b", "p2-a", "p2-b", "p3-a"];
  const result = selectAllMatching(allMatchingIds);
  assert.deepEqual([...result].sort(), [...allMatchingIds].sort());
});

test("selectAllMatching replaces a prior selection rather than adding to it", () => {
  // Simulates: user had hand-picked a couple of contacts, then clicked "select all
  // matching" -- the stale hand-picked selection must not linger alongside it.
  const staleSelection = ["irrelevant-old-id"];
  void staleSelection; // selectAllMatching takes only the new matching set, by design
  const result = selectAllMatching(["m1", "m2"]);
  assert.deepEqual([...result].sort(), ["m1", "m2"]);
});

test("selectAllMatching with an empty result set selects nothing", () => {
  assert.deepEqual([...selectAllMatching([])], []);
});

// --- clearSelection ---

test("clearSelection always returns an empty set", () => {
  assert.deepEqual([...clearSelection()], []);
});
