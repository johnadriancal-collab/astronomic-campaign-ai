import { test } from "node:test";
import assert from "node:assert/strict";
import { compareContactsByName } from "./sort-contacts.ts";

function sortedNames(contacts: { first_name?: string | null; last_name?: string | null; email?: string | null }[]) {
  return [...contacts].sort(compareContactsByName).map((c) => `${c.first_name ?? ""} ${c.last_name ?? ""}`.trim() || `<${c.email}>`);
}

test("sorts by first name then last name, alphabetically", () => {
  const contacts = [
    { first_name: "Zoe", last_name: "Adams" },
    { first_name: "Ada", last_name: "Lovelace" },
    { first_name: "Ada", last_name: "Byron" },
  ];
  assert.deepEqual(sortedNames(contacts), ["Ada Byron", "Ada Lovelace", "Zoe Adams"]);
});

test("is case-insensitive", () => {
  const contacts = [{ first_name: "bob", last_name: "zephyr" }, { first_name: "Alice", last_name: "Adams" }];
  assert.deepEqual(sortedNames(contacts), ["Alice Adams", "bob zephyr"]);
});

test("handles a missing first name gracefully", () => {
  const contacts = [{ first_name: null, last_name: "Adams" }, { first_name: "Bob", last_name: "Zephyr" }];
  assert.deepEqual(sortedNames(contacts), ["Adams", "Bob Zephyr"]);
});

test("handles a missing last name gracefully", () => {
  const contacts = [{ first_name: "Zoe", last_name: null }, { first_name: "Adam", last_name: null }];
  assert.deepEqual(sortedNames(contacts), ["Adam", "Zoe"]);
});

test("contacts with no name at all sort to the bottom", () => {
  const contacts = [
    { first_name: null, last_name: null, email: "z@example.com" },
    { first_name: "Ada", last_name: "Lovelace", email: "ada@example.com" },
  ];
  assert.deepEqual(sortedNames(contacts), ["Ada Lovelace", "<z@example.com>"]);
});

test("nameless contacts are tie-broken deterministically by email", () => {
  const contacts = [
    { first_name: "", last_name: "", email: "zed@example.com" },
    { first_name: null, last_name: null, email: "amy@example.com" },
  ];
  assert.deepEqual(sortedNames(contacts), ["<amy@example.com>", "<zed@example.com>"]);
});

test("whitespace-only names are treated as missing, not as a real name", () => {
  const contacts = [
    { first_name: "  ", last_name: "  ", email: "blank@example.com" },
    { first_name: "Ada", last_name: "Lovelace", email: "ada@example.com" },
  ];
  assert.deepEqual(sortedNames(contacts), ["Ada Lovelace", "<blank@example.com>"]);
});

test("ordering is stable/deterministic across repeated sorts of the same input", () => {
  const contacts = [
    { first_name: "Ada", last_name: "Lovelace", email: "a@example.com" },
    { first_name: null, last_name: null, email: "b@example.com" },
    { first_name: "Bob", last_name: "Zephyr", email: "c@example.com" },
    { first_name: null, last_name: null, email: "a2@example.com" },
  ];
  const first = sortedNames(contacts);
  const second = sortedNames(contacts);
  assert.deepEqual(first, second);
  assert.deepEqual(first, ["Ada Lovelace", "Bob Zephyr", "<a2@example.com>", "<b@example.com>"]);
});

test("a large mixed batch sorts correctly end to end", () => {
  const contacts = [
    { first_name: "Zoe", last_name: "Yates" },
    { first_name: null, last_name: null, email: "nobody@example.com" },
    { first_name: "alice", last_name: "adams" },
    { first_name: "Alice", last_name: "Zephyr" },
    { first_name: "Bob", last_name: null },
  ];
  assert.deepEqual(sortedNames(contacts), ["alice adams", "Alice Zephyr", "Bob", "Zoe Yates", "<nobody@example.com>"]);
});
