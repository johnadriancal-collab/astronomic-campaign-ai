import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

// Stage 7 Investment Industry Searchable Multi-Select: source-inspection
// tests, same "no component-render harness" convention as
// client-detail-no-fake-features.test.ts / add-prospects-ui.test.ts. These
// verify the WIRING (which component a given custom field routes to)
// rather than rendered DOM, since this project has no React render harness
// -- see lib/searchable-multi-select.test.ts for the actual filtering/
// selection/keyboard-nav logic, unit-tested directly.

const CONTACT_PAGE = readFileSync(new URL("../app/crm/[id]/page.tsx", import.meta.url), "utf-8");
const SEARCHABLE_MULTI_SELECT_COMPONENT = readFileSync(
  new URL("../components/searchable-multi-select.tsx", import.meta.url),
  "utf-8"
);

test("the Contact page imports SearchableMultiSelect", () => {
  assert.match(CONTACT_PAGE, /import \{ SearchableMultiSelect \} from "@\/components\/searchable-multi-select";/);
});

test("the Contact page imports the existing canonical INDUSTRY_OPTIONS, not a new taxonomy", () => {
  assert.match(CONTACT_PAGE, /INDUSTRY_OPTIONS/);
  assert.match(CONTACT_PAGE, /from "@\/lib\/crm-thesis-options"/);
});

test("investment_industry is routed to SearchableMultiSelect with INDUSTRY_OPTIONS", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(CONTACT_PAGE.indexOf('field.field_type === "multi_select"'));
  const investmentIndustryBlock = multiSelectBranch.slice(
    multiSelectBranch.indexOf('field.field_key === "investment_industry"'),
    multiSelectBranch.indexOf('field.field_type === "long_text"')
  );
  assert.match(investmentIndustryBlock, /<SearchableMultiSelect/);
  assert.match(investmentIndustryBlock, /options=\{INDUSTRY_OPTIONS\}/);
});

test("every other multi_select custom field still falls through to the generic MultiSelect, unchanged", () => {
  const multiSelectBranch = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf('field.field_type === "multi_select"'),
    CONTACT_PAGE.indexOf('field.field_type === "long_text"')
  );
  // The investment_industry special case returns early; the generic
  // <MultiSelect options={field.options} .../> call below it must still
  // exist as the fallback for every other custom field key.
  assert.match(multiSelectBranch, /<MultiSelect\s+label=\{field\.label\}\s+options=\{field\.options\}/);
});

test("Dietary Preferences, Asset Types, and Business Models are untouched -- still rendered via the generic MultiSelect", () => {
  assert.match(CONTACT_PAGE, /label="Dietary preferences"/);
  assert.match(CONTACT_PAGE, /options=\{DIETARY_PREFERENCE_OPTIONS\}/);
  assert.match(CONTACT_PAGE, /options=\{field\.options\}\s*\n\s*selected=\{\(contact\[key\]/);
});

test("the generic MultiSelect/TagMultiSelect components themselves are unmodified (still option-count-driven, no investment_industry special-casing inside them)", () => {
  const genericComponentSource = CONTACT_PAGE.slice(
    CONTACT_PAGE.indexOf("function MultiSelect("),
    CONTACT_PAGE.indexOf("export default function CrmContactDetailPage()")
  );
  assert.doesNotMatch(genericComponentSource, /investment_industry/);
  assert.match(genericComponentSource, /options\.length === 0/);
});

test("SearchableMultiSelect the component does not embed the Investment Industry taxonomy itself", () => {
  assert.doesNotMatch(SEARCHABLE_MULTI_SELECT_COMPONENT, /INDUSTRY_OPTIONS/);
  assert.doesNotMatch(SEARCHABLE_MULTI_SELECT_COMPONENT, /crm-thesis-options/);
  assert.doesNotMatch(SEARCHABLE_MULTI_SELECT_COMPONENT, /Real Estate/);
});

test("SearchableMultiSelect takes options as a prop, not a hardcoded list", () => {
  assert.match(SEARCHABLE_MULTI_SELECT_COMPONENT, /options:\s*readonly string\[\]/);
});
