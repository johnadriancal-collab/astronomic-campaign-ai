import { test } from "node:test";
import assert from "node:assert/strict";
import { addTagValue, removeTagValue } from "./tag-multi-select.ts";

test("adds a new value to an empty list", () => {
  assert.deepEqual(addTagValue([], "Healthcare"), ["Healthcare"]);
});

test("appends a new value after existing ones", () => {
  assert.deepEqual(addTagValue(["Healthcare"], "Biotech"), ["Healthcare", "Biotech"]);
});

test("trims leading/trailing whitespace before adding", () => {
  assert.deepEqual(addTagValue([], "  Fintech  "), ["Fintech"]);
});

test("rejects an empty value", () => {
  assert.deepEqual(addTagValue(["Healthcare"], ""), ["Healthcare"]);
});

test("rejects a whitespace-only value", () => {
  assert.deepEqual(addTagValue(["Healthcare"], "   "), ["Healthcare"]);
});

test("rejects an exact duplicate value", () => {
  assert.deepEqual(addTagValue(["Healthcare"], "Healthcare"), ["Healthcare"]);
});

test("rejects a duplicate after trimming whitespace", () => {
  assert.deepEqual(addTagValue(["Healthcare"], "  Healthcare  "), ["Healthcare"]);
});

test("treats different case as a distinct value, not a duplicate", () => {
  assert.deepEqual(addTagValue(["Healthcare"], "healthcare"), ["Healthcare", "healthcare"]);
});

test("removes an existing value", () => {
  assert.deepEqual(removeTagValue(["Healthcare", "Biotech"], "Healthcare"), ["Biotech"]);
});

test("removing a value not present is a no-op", () => {
  assert.deepEqual(removeTagValue(["Healthcare"], "Fintech"), ["Healthcare"]);
});

test("addTagValue returns a new array reference when it adds a value", () => {
  const original = ["Healthcare"];
  const result = addTagValue(original, "Biotech");
  assert.notEqual(result, original);
});

test("addTagValue returns the SAME array reference when nothing changes (empty/duplicate)", () => {
  const original = ["Healthcare"];
  assert.equal(addTagValue(original, ""), original);
  assert.equal(addTagValue(original, "Healthcare"), original);
});
