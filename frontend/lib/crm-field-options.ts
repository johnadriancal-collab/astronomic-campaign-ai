/**
 * Canonical dropdown option lists for CRM CORE contact fields that aren't
 * part of the Investor Thesis form (see lib/crm-thesis-options.ts for
 * those) -- kept in its own file since email_status isn't a thesis-form
 * question. Mirrors app/models/crm.py exactly; if the backend list ever
 * changes, update both files together.
 */

// Derived from a full production tally (2026-09-02, 2,727 contacts), not
// invented -- see app/models/crm.py's EMAIL_STATUS_OPTIONS for the full
// rationale, including why "User Managed" is included (trusted/sendable,
// not a deliverability status) and why "Valid"/"valid" are deliberately
// excluded (legacy values, not migrated in this change). This list only
// drives the UI -- email_status itself stays a plain, unvalidated
// `string | null` field. A contact holding a value NOT in this list must
// stay visible wherever it's rendered, never silently blanked.
export const EMAIL_STATUS_OPTIONS = [
  "User Managed",
  "Verified",
  "Unverified",
  "Invalid",
  "Unavailable",
  "Email No Longer Verified",
  "New Data Available",
  "Extrapolated",
];

// True for any non-blank value that isn't one of the canonical options
// above -- covers both known-legacy values (e.g. "valid", from CSV
// import/ITF) and any other value already stored that this list has never
// heard of. Blank ("" or null) is never "legacy" -- it's the normal
// no-selection state.
export function isLegacyEmailStatus(value: string | null): boolean {
  return value !== null && value !== "" && !EMAIL_STATUS_OPTIONS.includes(value);
}

// The exact ordered list of raw <option> values a <select> bound to
// email_status should render for a given CURRENT value: the blank/
// not-set option first, then -- ONLY if the current value is a legacy
// value -- that exact value injected as its own option (so it stays
// selected/visible instead of the browser falling back to showing nothing
// selected), then every canonical option, in their fixed order. Never
// drops, reorders, or renames the canonical list regardless of the
// current value; never invents a legacy option when there isn't one.
export function emailStatusSelectOptionValues(currentValue: string | null): string[] {
  const options = ["", ...EMAIL_STATUS_OPTIONS];
  if (isLegacyEmailStatus(currentValue)) {
    options.splice(1, 0, currentValue as string);
  }
  return options;
}

// Human-readable label for one raw <option> value produced by
// emailStatusSelectOptionValues -- the blank option and an injected
// legacy option both need special-cased text; every canonical option's
// label is just itself.
export function emailStatusOptionLabel(optionValue: string): string {
  if (optionValue === "") return "-- not set --";
  if (isLegacyEmailStatus(optionValue)) return `${optionValue} (legacy value)`;
  return optionValue;
}

// A <select>'s raw string value ("" for the blank option) -> the real
// field value to store. The blank option must produce null, matching
// email_status's own `string | null` type -- never a stored empty string.
export function emailStatusFromSelectValue(raw: string): string | null {
  return raw === "" ? null : raw;
}
