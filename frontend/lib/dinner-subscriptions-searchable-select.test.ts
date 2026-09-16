import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Dinner Subscriptions Searchable Multi-Select (2026-09-16): source-inspection
// tests, same convention as investment-industry-searchable-select.test.ts /
// dinners-attended-searchable-select.test.ts (no React render harness in this
// project -- see either file's own header comment, and
// lib/searchable-multi-select.test.ts for the actual filtering/selection/
// keyboard-nav logic, unit-tested directly).
//
// UI-only change: the option list itself (DINNER_SUBSCRIPTION_OPTIONS,
// app/models/crm.py) is deliberately untouched -- same 14 values, same
// order, nothing added/removed/reordered/migrated. Only WHICH component
// renders them changes, from the generic checkbox-grid MultiSelect to the
// same SearchableMultiSelect already used for Investment Industry and
// Dinners Attended, sourced from this field's own LIVE `field.options`.

const CONTACT_PAGE = readFileSync(new URL("../app/crm/[id]/page.tsx", import.meta.url), "utf-8");
const SEARCHABLE_MULTI_SELECT_COMPONENT = readFileSync(
  new URL("../components/searchable-multi-select.tsx", import.meta.url),
  "utf-8"
);

test("dinner_subscriptions is routed to SearchableMultiSelect using the field's own live options, not a duplicated frontend list", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(CONTACT_PAGE.indexOf('field.field_type === "multi_select"'));
  const dinnerSubscriptionsBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "dinner_subscriptions"'),
    multiSelectBranch.indexOf('field.field_type === "long_text"')
  );
  assert.match(dinnerSubscriptionsBlock, /<SearchableMultiSelect/);
  assert.match(dinnerSubscriptionsBlock, /options=\{field\.options\}/);
  assert.doesNotMatch(dinnerSubscriptionsBlock, /DINNER_SUBSCRIPTION_OPTIONS/);
});

test("the dinner_subscriptions branch is checked before, and returns before, the generic checkbox-grid MultiSelect fallback", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf('field.field_type === "multi_select"'),
    CONTACT_PAGE.indexOf('field.field_type === "long_text"')
  );
  const dinnerSubscriptionsIndex = multiSelectBranch.indexOf('field.field_key === "dinner_subscriptions"');
  const genericFallbackIndex = multiSelectBranch.indexOf("<MultiSelect\n");
  assert.ok(dinnerSubscriptionsIndex !== -1, "dinner_subscriptions branch must exist");
  assert.ok(genericFallbackIndex !== -1, "generic MultiSelect fallback must still exist");
  assert.ok(dinnerSubscriptionsIndex < genericFallbackIndex, "dinner_subscriptions must be checked before the generic fallback");
});

test("every other multi_select custom field still falls through to the generic MultiSelect, unchanged", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf('field.field_type === "multi_select"'),
    CONTACT_PAGE.indexOf('field.field_type === "long_text"')
  );
  assert.match(multiSelectBranch, /<MultiSelect\s+label=\{field\.label\}\s+options=\{field\.options\}/);
});

test("Investment Industry and Dinners Attended routing are both unchanged by the dinner_subscriptions addition", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(CONTACT_PAGE.indexOf('field.field_type === "multi_select"'));
  const investmentIndustryBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "investment_industry"'),
    multiSelectBranch.indexOf('field.field_key === "dinners_attended"')
  );
  assert.match(investmentIndustryBlock, /<SearchableMultiSelect/);
  assert.match(investmentIndustryBlock, /options=\{INDUSTRY_OPTIONS\}/);

  const dinnersAttendedBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "dinners_attended"'),
    multiSelectBranch.indexOf('field.field_key === "dinner_subscriptions"')
  );
  assert.match(dinnersAttendedBlock, /<SearchableMultiSelect/);
  assert.match(dinnersAttendedBlock, /placeholder="Search dinners\.\.\."/);
});

test("the generic MultiSelect/TagMultiSelect components themselves are unmodified (no dinner_subscriptions special-casing inside them)", () => {
  const genericComponentSource = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf("function MultiSelect("),
    CONTACT_PAGE.indexOf("export default function CrmContactDetailPage()")
  );
  assert.doesNotMatch(genericComponentSource, /dinner_subscriptions/);
});

test("the Dinner Subscriptions call site passes its own noMatchesLabel, distinct from the other two searchable fields", () => {
  assert.match(CONTACT_PAGE, /noMatchesLabel="No matching industries"/);
  assert.match(CONTACT_PAGE, /noMatchesLabel="No matching dinners"/);
  assert.match(CONTACT_PAGE, /noMatchesLabel="No matching subscriptions"/);
});

test("SearchableMultiSelect renders every selected value as a chip, even one absent from `options` -- preserves any legacy/unknown Dinner Subscriptions selection", () => {
  assert.match(
    SEARCHABLE_MULTI_SELECT_COMPONENT,
    /Selected chips render EVERY value in `values`, including a value\s*\n\/\/ that isn't in `options`/
  );
});
