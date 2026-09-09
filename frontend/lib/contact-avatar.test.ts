import { test } from "node:test";
import assert from "node:assert/strict";
import { getContactInitials } from "./contact-avatar.ts";

test("first and last name both present -> first letter of each", () => {
  assert.equal(getContactInitials("Ada", "Lovelace", "ada@example.com"), "AL");
});

test("first name only -> first two letters of it", () => {
  assert.equal(getContactInitials("Ada", null, "ada@example.com"), "AD");
});

test("last name only -> first two letters of it", () => {
  assert.equal(getContactInitials(null, "Lovelace", "ada@example.com"), "LO");
});

test("no name at all -> first letter of email", () => {
  assert.equal(getContactInitials(null, null, "ada@example.com"), "A");
});

test("nothing at all -> a plain fallback, never crashes", () => {
  assert.equal(getContactInitials(null, null, null), "?");
});

test("whitespace-only names are treated as missing", () => {
  assert.equal(getContactInitials("   ", "   ", "ada@example.com"), "A");
});

test("initials are always uppercase regardless of input case", () => {
  assert.equal(getContactInitials("ada", "lovelace", null), "AL");
});
