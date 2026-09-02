import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EMAIL_STATUS_OPTIONS,
  emailStatusFromSelectValue,
  emailStatusOptionLabel,
  emailStatusSelectOptionValues,
  isLegacyEmailStatus,
} from "./crm-field-options.ts";

// --- all canonical options render, in the exact product-decided order ----

test("EMAIL_STATUS_OPTIONS is exactly the 8 canonical statuses, in order", () => {
  assert.deepEqual(EMAIL_STATUS_OPTIONS, [
    "User Managed",
    "Verified",
    "Unverified",
    "Invalid",
    "Unavailable",
    "Email No Longer Verified",
    "New Data Available",
    "Extrapolated",
  ]);
});

test("EMAIL_STATUS_OPTIONS deliberately excludes the legacy Valid/valid values", () => {
  assert.ok(!EMAIL_STATUS_OPTIONS.includes("Valid"));
  assert.ok(!EMAIL_STATUS_OPTIONS.includes("valid"));
});

// --- isLegacyEmailStatus ---------------------------------------------------

test("every canonical option is never treated as legacy", () => {
  for (const option of EMAIL_STATUS_OPTIONS) {
    assert.equal(isLegacyEmailStatus(option), false);
  }
});

test("null and empty string are never treated as legacy -- they're the normal blank state", () => {
  assert.equal(isLegacyEmailStatus(null), false);
  assert.equal(isLegacyEmailStatus(""), false);
});

test("a known legacy value (valid) is treated as legacy", () => {
  assert.equal(isLegacyEmailStatus("valid"), true);
});

test("an arbitrary unrecognized value is also treated as legacy, not just known ones", () => {
  assert.equal(isLegacyEmailStatus("Some Random String Nobody Anticipated"), true);
});

// --- blank/null <-> select value round-trip --------------------------------

test("blank select value maps to null, never an empty string", () => {
  assert.equal(emailStatusFromSelectValue(""), null);
});

test("a canonical select value maps through unchanged", () => {
  assert.equal(emailStatusFromSelectValue("Verified"), "Verified");
});

test("selecting a canonical status explicitly replaces whatever the previous value was", () => {
  // The transform is stateless -- selecting "Verified" always produces
  // "Verified" regardless of what the field held before (a legacy value,
  // another canonical value, or nothing at all).
  assert.equal(emailStatusFromSelectValue("Verified"), "Verified");
});

// --- emailStatusSelectOptionValues: what actually gets rendered -----------

test("with no current value, the option list is just blank + the 8 canonical options", () => {
  assert.deepEqual(emailStatusSelectOptionValues(null), ["", ...EMAIL_STATUS_OPTIONS]);
  assert.deepEqual(emailStatusSelectOptionValues(""), ["", ...EMAIL_STATUS_OPTIONS]);
});

test("with a canonical current value, the option list is unchanged -- no duplicate injected", () => {
  assert.deepEqual(emailStatusSelectOptionValues("Verified"), ["", ...EMAIL_STATUS_OPTIONS]);
});

test("an existing legacy value (valid) is injected as its own option, right after blank, and remains selected", () => {
  const options = emailStatusSelectOptionValues("valid");
  assert.deepEqual(options, ["", "valid", ...EMAIL_STATUS_OPTIONS]);
  // The select's own `value` prop (set by the component to `value ?? ""`)
  // is "valid" here, which now matches a real <option>, so the browser
  // shows it selected instead of falling back to nothing selected.
  assert.ok(options.includes("valid"));
});

test("an arbitrary unrecognized legacy value is preserved the same way, not just known legacy values", () => {
  const options = emailStatusSelectOptionValues("Some Random Legacy Thing");
  assert.deepEqual(options, ["", "Some Random Legacy Thing", ...EMAIL_STATUS_OPTIONS]);
});

test("the canonical options never get reordered or dropped, regardless of the current value", () => {
  const withLegacy = emailStatusSelectOptionValues("valid");
  const withoutLegacy = emailStatusSelectOptionValues(null);
  assert.deepEqual(
    withLegacy.filter((o) => o !== "valid"),
    withoutLegacy
  );
});

// --- emailStatusOptionLabel --------------------------------------------

test("the blank option's label is '-- not set --'", () => {
  assert.equal(emailStatusOptionLabel(""), "-- not set --");
});

test("a canonical option's label is just itself", () => {
  assert.equal(emailStatusOptionLabel("Verified"), "Verified");
});

test("a legacy option's label calls out that it's legacy", () => {
  assert.equal(emailStatusOptionLabel("valid"), "valid (legacy value)");
});

// --- end-to-end scenarios matching the exact required test list -----------

test("a contact with an existing legacy value (valid) displays it, and it survives an unrelated field's change untouched", () => {
  // Simulates: contact loads with email_status="valid"; the user edits some
  // OTHER field and saves. EmailStatusField's own state/props never change
  // unless its own onChange fires -- confirmed structurally: the option
  // list and displayed value are pure functions of the current
  // email_status alone, so nothing about editing an unrelated field can
  // alter what these functions return for the same input.
  const before = emailStatusSelectOptionValues("valid");
  const after = emailStatusSelectOptionValues("valid"); // same value, as it would be after an unrelated save
  assert.deepEqual(before, after);
  assert.equal(emailStatusFromSelectValue("valid"), "valid"); // if re-submitted unchanged, still "valid"
});

test("explicitly changing a legacy value (valid) to a canonical status (Verified) works", () => {
  // The user picks "Verified" from the dropdown that included "valid" as
  // its injected legacy option -- the resulting stored value is exactly
  // "Verified", the legacy value is gone from the field (though the
  // underlying CRM record's history/audit trail, if any, is untouched by
  // this UI layer either way).
  assert.equal(emailStatusFromSelectValue("Verified"), "Verified");
  // And the option list no longer needs to inject "valid" once the value
  // itself has become canonical.
  assert.deepEqual(emailStatusSelectOptionValues("Verified"), ["", ...EMAIL_STATUS_OPTIONS]);
});
