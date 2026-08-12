import { test } from "node:test";
import assert from "node:assert/strict";
import { describeBulkAddResult } from "./add-to-list.ts";

test("reports a plain add with no already-members", () => {
  assert.equal(describeBulkAddResult(84, 0, "Austin Family Offices"), 'Added 84 contacts to "Austin Family Offices".');
});

test("reports added + already-member counts together", () => {
  assert.equal(
    describeBulkAddResult(84, 5, "Austin Family Offices"),
    'Added 84 contacts to "Austin Family Offices". 5 were already members.'
  );
});

test("singular wording for exactly one added contact", () => {
  assert.equal(describeBulkAddResult(1, 0, "AI Investors"), 'Added 1 contact to "AI Investors".');
});

test("singular wording for exactly one already-member alongside new adds", () => {
  assert.equal(
    describeBulkAddResult(2, 1, "AI Investors"),
    'Added 2 contacts to "AI Investors". 1 was already a member.'
  );
});

test("states plainly (not as an error) when every selected contact was already a member", () => {
  assert.equal(describeBulkAddResult(0, 5, "Austin Family Offices"), 'All 5 selected contacts were already in "Austin Family Offices".');
});

test("singular wording when the one selected contact was already a member", () => {
  assert.equal(describeBulkAddResult(0, 1, "Austin Family Offices"), 'That contact was already in "Austin Family Offices".');
});
