import assert from "node:assert/strict";
import { test } from "node:test";
import {
  defaultSelectedFieldKeys,
  formatFieldValue,
  hasNoProposedChanges,
  senderDisplayName,
  senderEmail,
  statusBadgeClass,
  statusLabel,
} from "./email-intake.ts";

test("statusLabel maps every known status to a human label", () => {
  assert.equal(statusLabel("pending_review"), "Pending");
  assert.equal(statusLabel("needs_match"), "Needs Match");
  assert.equal(statusLabel("approved"), "Approved");
  assert.equal(statusLabel("rejected"), "Rejected");
  assert.equal(statusLabel("error"), "Error");
});

test("statusBadgeClass returns a distinct class per status", () => {
  const classes = new Set(
    ["pending_review", "needs_match", "approved", "rejected", "error"].map((s) =>
      statusBadgeClass(s as never)
    )
  );
  assert.equal(classes.size, 5);
});

test("formatFieldValue renders null/undefined as an em dash", () => {
  assert.equal(formatFieldValue(null), "—");
  assert.equal(formatFieldValue(undefined), "—");
});

test("formatFieldValue renders an empty list as an em dash", () => {
  assert.equal(formatFieldValue([]), "—");
});

test("formatFieldValue joins a non-empty list with commas", () => {
  assert.equal(formatFieldValue(["AI", "Healthcare"]), "AI, Healthcare");
});

test("formatFieldValue renders a plain string as-is", () => {
  assert.equal(formatFieldValue("Massive Capital"), "Massive Capital");
});

test("formatFieldValue treats a blank string as an em dash", () => {
  assert.equal(formatFieldValue("   "), "—");
});

test("hasNoProposedChanges is true for an empty proposal", () => {
  const item = { proposal: [] } as never;
  assert.equal(hasNoProposedChanges(item), true);
});

test("hasNoProposedChanges is false when at least one change exists", () => {
  const item = { proposal: [{ field_key: "company" }] } as never;
  assert.equal(hasNoProposedChanges(item), false);
});

test("senderDisplayName extracts the display name from 'Name <email>'", () => {
  assert.equal(senderDisplayName("Amos Ben-Meir <amos@example.com>"), "Amos Ben-Meir");
});

test("senderDisplayName falls back to the bare address with no display name", () => {
  assert.equal(senderDisplayName("amos@example.com"), "amos@example.com");
});

test("senderEmail extracts the address from 'Name <email>'", () => {
  assert.equal(senderEmail("Amos Ben-Meir <amos@example.com>"), "amos@example.com");
});

test("senderEmail returns the bare address unchanged", () => {
  assert.equal(senderEmail("amos@example.com"), "amos@example.com");
});

test("defaultSelectedFieldKeys starts every proposed change checked", () => {
  const selected = defaultSelectedFieldKeys([
    { field_key: "company" } as never,
    { field_key: "phone" } as never,
  ]);
  assert.deepEqual([...selected].sort(), ["company", "phone"]);
});

test("defaultSelectedFieldKeys is empty for an empty proposal", () => {
  assert.equal(defaultSelectedFieldKeys([]).size, 0);
});
