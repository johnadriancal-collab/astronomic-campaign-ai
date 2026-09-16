import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Dinners Attended Searchable Multi-Select (2026-09-16): source-inspection
// tests, same convention as investment-industry-searchable-select.test.ts
// (no React render harness in this project -- see that file's own header
// comment, and lib/searchable-multi-select.test.ts for the actual
// filtering/selection/keyboard-nav logic, unit-tested directly).
//
// Root cause this fixes: "Hive ASMBLD [10.06.2025] Austin" was already
// correctly stored on Cory Gulotta's contact, but had no checkbox in the
// old generic MultiSelect (which only renders one per `field.options`
// entry) -- so it was invisible/uneditable despite being real data. Unlike
// Investment Industry, Dinners Attended's LIVE `field.options` (from the
// backend's DINNERS_ATTENDED_OPTIONS, app/models/crm.py) is never empty,
// so this field is routed to SearchableMultiSelect using `field.options`
// directly rather than a second, frontend-duplicated option list.

const CONTACT_PAGE = readFileSync(new URL("../app/crm/[id]/page.tsx", import.meta.url), "utf-8");
const SEARCHABLE_MULTI_SELECT_COMPONENT = readFileSync(
  new URL("../components/searchable-multi-select.tsx", import.meta.url),
  "utf-8"
);

test("dinners_attended is routed to SearchableMultiSelect using the field's own live options, not a duplicated frontend list", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(CONTACT_PAGE.indexOf('field.field_type === "multi_select"'));
  const dinnersAttendedBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "dinners_attended"'),
    multiSelectBranch.indexOf('field.field_type === "long_text"')
  );
  assert.match(dinnersAttendedBlock, /<SearchableMultiSelect/);
  assert.match(dinnersAttendedBlock, /options=\{field\.options\}/);
  // Deliberately NOT a hardcoded DINNERS_ATTENDED_OPTIONS-style frontend
  // constant -- the backend's live field.options is the one source of truth.
  assert.doesNotMatch(dinnersAttendedBlock, /DINNERS_ATTENDED_OPTIONS/);
});

test("the dinners_attended branch is checked before, and returns before, the generic checkbox-grid MultiSelect fallback", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf('field.field_type === "multi_select"'),
    CONTACT_PAGE.indexOf('field.field_type === "long_text"')
  );
  const dinnersAttendedIndex = multiSelectBranch.indexOf('field.field_key === "dinners_attended"');
  const genericFallbackIndex = multiSelectBranch.indexOf("<MultiSelect\n");
  assert.ok(dinnersAttendedIndex !== -1, "dinners_attended branch must exist");
  assert.ok(genericFallbackIndex !== -1, "generic MultiSelect fallback must still exist");
  assert.ok(dinnersAttendedIndex < genericFallbackIndex, "dinners_attended must be checked before the generic fallback");
});

test("every other multi_select custom field still falls through to the generic MultiSelect, unchanged", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf('field.field_type === "multi_select"'),
    CONTACT_PAGE.indexOf('field.field_type === "long_text"')
  );
  assert.match(multiSelectBranch, /<MultiSelect\s+label=\{field\.label\}\s+options=\{field\.options\}/);
});

test("investment_industry routing is unchanged by the dinners_attended addition", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(CONTACT_PAGE.indexOf('field.field_type === "multi_select"'));
  const investmentIndustryBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "investment_industry"'),
    multiSelectBranch.indexOf('field.field_key === "dinners_attended"')
  );
  assert.match(investmentIndustryBlock, /<SearchableMultiSelect/);
  assert.match(investmentIndustryBlock, /options=\{INDUSTRY_OPTIONS\}/);
});

test("the generic MultiSelect/TagMultiSelect components themselves are unmodified (no dinners_attended special-casing inside them)", () => {
  const genericComponentSource = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf("function MultiSelect("),
    CONTACT_PAGE.indexOf("export default function CrmContactDetailPage()")
  );
  assert.doesNotMatch(genericComponentSource, /dinners_attended/);
});

test("SearchableMultiSelect supports a per-caller empty-state message instead of a hardcoded 'No matching industries'", () => {
  assert.match(SEARCHABLE_MULTI_SELECT_COMPONENT, /noMatchesLabel/);
  assert.doesNotMatch(SEARCHABLE_MULTI_SELECT_COMPONENT, /No matching industries/);
});

test("the Dinners Attended and Investment Industry call sites each pass their own noMatchesLabel", () => {
  assert.match(CONTACT_PAGE, /noMatchesLabel="No matching industries"/);
  assert.match(CONTACT_PAGE, /noMatchesLabel="No matching dinners"/);
});

test("SearchableMultiSelect renders every selected value as a chip, even one absent from `options` -- the actual root-cause fix", () => {
  // See that component's own docstring: a legacy/pre-existing value renders
  // as a chip regardless of whether it's in `options`. This is what makes
  // Cory Gulotta's already-stored "Hive ASMBLD [10.06.2025] Austin"
  // selection visible immediately, with zero migration to his own data.
  assert.match(
    SEARCHABLE_MULTI_SELECT_COMPONENT,
    /Selected chips render EVERY value in `values`, including a value\s*\n\/\/ that isn't in `options`/
  );
  assert.match(SEARCHABLE_MULTI_SELECT_COMPONENT, /values\.map\(\(value\) => \(/);
});
