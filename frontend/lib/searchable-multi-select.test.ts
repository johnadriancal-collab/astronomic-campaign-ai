import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clampHighlightIndex,
  filterOptions,
  isLegacyValue,
  nextHighlightIndex,
  previousHighlightIndex,
  removeValue,
  selectOption,
} from "./searchable-multi-select.ts";

const OPTIONS = ["Cybersecurity", "Fintech (Finance & Insurance)", "Real Estate & PropTech", "Other"];

// --- filterOptions ---------------------------------------------------

test("an empty query returns every option not already selected", () => {
  assert.deepEqual(filterOptions(OPTIONS, "", []), OPTIONS);
});

test("already-selected options never appear in the available list", () => {
  assert.deepEqual(filterOptions(OPTIONS, "", ["Cybersecurity"]), [
    "Fintech (Finance & Insurance)",
    "Real Estate & PropTech",
    "Other",
  ]);
});

test("typing 'real' surfaces Real Estate & PropTech (case-insensitive substring match)", () => {
  assert.deepEqual(filterOptions(OPTIONS, "real", []), ["Real Estate & PropTech"]);
});

test("matching is case-insensitive regardless of query casing", () => {
  assert.deepEqual(filterOptions(OPTIONS, "REAL", []), ["Real Estate & PropTech"]);
  assert.deepEqual(filterOptions(OPTIONS, "ReAl", []), ["Real Estate & PropTech"]);
});

test("matches anywhere in the option, not only at the start", () => {
  assert.deepEqual(filterOptions(OPTIONS, "insurance", []), ["Fintech (Finance & Insurance)"]);
});

test("a query with no matches returns an empty list", () => {
  assert.deepEqual(filterOptions(OPTIONS, "quantum computing", []), []);
});

test("leading/trailing whitespace in the query is ignored", () => {
  assert.deepEqual(filterOptions(OPTIONS, "  real  ", []), ["Real Estate & PropTech"]);
});

// --- selectOption ------------------------------------------------------

test("selects an approved option not yet selected", () => {
  assert.deepEqual(selectOption([], "Cybersecurity", OPTIONS), ["Cybersecurity"]);
});

test("appends to existing selections, preserving order", () => {
  assert.deepEqual(selectOption(["Cybersecurity"], "Other", OPTIONS), ["Cybersecurity", "Other"]);
});

test("refuses to add text that is not an exact approved option", () => {
  assert.deepEqual(selectOption([], "Quantum Computing", OPTIONS), []);
  assert.deepEqual(selectOption(["Cybersecurity"], "real", OPTIONS), ["Cybersecurity"]);
});

test("refuses to add a duplicate of an already-selected option", () => {
  assert.deepEqual(selectOption(["Cybersecurity"], "Cybersecurity", OPTIONS), ["Cybersecurity"]);
});

test("returns the SAME array reference when nothing changes", () => {
  const current = ["Cybersecurity"];
  assert.equal(selectOption(current, "Cybersecurity", OPTIONS), current);
  assert.equal(selectOption(current, "Not An Option", OPTIONS), current);
});

test("a legacy/noncanonical value removed via removeValue can never be recreated through selectOption", () => {
  // "Real Estate" (legacy) is not in OPTIONS -- selecting it is refused
  // even though it may have just been removed from the same contact.
  assert.deepEqual(selectOption([], "Real Estate", OPTIONS), []);
});

// --- removeValue ---------------------------------------------------------

test("removes an existing canonical value", () => {
  assert.deepEqual(removeValue(["Cybersecurity", "Other"], "Cybersecurity"), ["Other"]);
});

test("removes an existing legacy/noncanonical value just as freely as a canonical one", () => {
  assert.deepEqual(removeValue(["Real Estate", "Cybersecurity"], "Real Estate"), ["Cybersecurity"]);
});

test("removing a value not present is a no-op", () => {
  assert.deepEqual(removeValue(["Cybersecurity"], "Fintech (Finance & Insurance)"), ["Cybersecurity"]);
});

// --- isLegacyValue ---------------------------------------------------------

test("a canonical option is never legacy", () => {
  assert.equal(isLegacyValue("Cybersecurity", OPTIONS), false);
});

test("a value not in the approved list is legacy", () => {
  assert.equal(isLegacyValue("Real Estate", OPTIONS), true);
});

// --- keyboard navigation clamping ---------------------------------------

test("clampHighlightIndex returns -1 for an empty list regardless of index", () => {
  assert.equal(clampHighlightIndex(0, 0), -1);
  assert.equal(clampHighlightIndex(5, 0), -1);
  assert.equal(clampHighlightIndex(-1, 0), -1);
});

test("clampHighlightIndex clamps below zero up to the first item", () => {
  assert.equal(clampHighlightIndex(-1, 3), 0);
});

test("clampHighlightIndex clamps past the end back to the last item", () => {
  assert.equal(clampHighlightIndex(3, 3), 2);
});

test("clampHighlightIndex leaves an in-range index unchanged", () => {
  assert.equal(clampHighlightIndex(1, 3), 1);
});

test("ArrowDown (nextHighlightIndex) stops at the last item -- no wrap-around", () => {
  assert.equal(nextHighlightIndex(2, 3), 2);
});

test("ArrowUp (previousHighlightIndex) stops at the first item -- no wrap-around", () => {
  assert.equal(previousHighlightIndex(0, 3), 0);
});

test("ArrowDown/ArrowUp move exactly one position within range", () => {
  assert.equal(nextHighlightIndex(0, 3), 1);
  assert.equal(previousHighlightIndex(1, 3), 0);
});
