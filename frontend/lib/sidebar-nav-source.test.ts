import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// No component-render harness exists in this frontend (see lib/*.test.ts
// generally -- everything here is plain-function testing), so the CRM and
// Campaign Manager sidebars' hardcoded top-area links are verified by
// scanning their source directly. This guards the "Campaign Builder nav
// item removed, Astro AI present at '/'" requirement that
// lib/top-level-nav.test.ts covers for the shared site header data array.

const MANAGER_SIDEBAR = readFileSync(
  new URL("../components/manager-sidebar.tsx", import.meta.url),
  "utf-8"
);
const CRM_SIDEBAR = readFileSync(new URL("../components/crm-sidebar.tsx", import.meta.url), "utf-8");

test("Campaign Builder is not linked from the Campaign Manager sidebar", () => {
  assert.doesNotMatch(MANAGER_SIDEBAR, /Campaign Builder/);
});

test("Campaign Builder is not linked from the CRM sidebar", () => {
  assert.doesNotMatch(CRM_SIDEBAR, /Campaign Builder/);
});

test("Astro AI remains linked from both sidebars, pointing at '/'", () => {
  assert.match(MANAGER_SIDEBAR, /Astro AI/);
  assert.match(CRM_SIDEBAR, /Astro AI/);
  assert.match(MANAGER_SIDEBAR, /href="\/"/);
  assert.match(CRM_SIDEBAR, /href="\/"/);
  // The Astro AI link itself must not point at the retired /astro-ai page.
  assert.doesNotMatch(MANAGER_SIDEBAR, /href="\/astro-ai"/);
  assert.doesNotMatch(CRM_SIDEBAR, /href="\/astro-ai"/);
});
