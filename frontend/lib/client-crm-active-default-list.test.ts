import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Client CRM list, post-historical-migration stage (2026-09-14): the
// default Client CRM view must be Active-only, but a text search must
// still reach every status (an inactive historical Client like "Valorem
// Capital" or "Ristretto" must still be findable without the user
// manually switching the status dropdown first). The actual filter-state
// logic is unit-tested directly in client-crm.test.ts
// (applySearchStatus/applyManualStatusFilter/isNonDefaultStatusFilter) --
// this file only confirms app/clients/page.tsx is actually WIRED to that
// logic and that nothing else on the page (search box, status/relationship
// dropdowns, owner filter, archived toggle, pagination) was disturbed.
// Scanned directly from source, same "no component-render harness"
// convention as client-list-summary-columns.test.ts /
// sidebar-nav-source.test.ts.

const CLIENT_LIST_PAGE = readFileSync(new URL("../app/clients/page.tsx", import.meta.url), "utf-8");
const CLIENT_CRM_TS = readFileSync(new URL("../lib/client-crm.ts", import.meta.url), "utf-8");

test("runSearch resolves status through applySearchStatus, keyed off the remembered manual pick, not a hardcoded value", () => {
  const runSearchBlock = CLIENT_LIST_PAGE.match(/function runSearch\(\)[\s\S]*?\n  \}/);
  assert.ok(runSearchBlock, "expected to find runSearch()");
  assert.match(runSearchBlock![0], /applySearchStatus\(searchInput, lastManualStatus\)/);
});

test("applyStatus (the dropdown handler) resolves through applyManualStatusFilter, not a raw cast straight into filters, and remembers the pick", () => {
  const applyStatusBlock = CLIENT_LIST_PAGE.match(/function applyStatus\([\s\S]*?\n  \}/);
  assert.ok(applyStatusBlock, "expected to find applyStatus()");
  assert.match(applyStatusBlock![0], /applyManualStatusFilter\(/);
  assert.match(applyStatusBlock![0], /setLastManualStatus\(/);
});

test("a lastManualStatus piece of state exists to remember a deliberate pick across a later search override", () => {
  assert.match(CLIENT_LIST_PAGE, /const \[lastManualStatus, setLastManualStatus\] = useState<ClientStatus \| "" \| null>\(null\)/);
});

test("hasActiveFilters uses isNonDefaultStatusFilter, so the plain Active default never shows the wrong empty-state copy", () => {
  assert.match(CLIENT_LIST_PAGE, /isNonDefaultStatusFilter\(filters\.status\)/);
});

test("defaultClientListFilters defaults status to the Active constant, not blank", () => {
  const defaultsMatch = CLIENT_CRM_TS.match(/export function defaultClientListFilters\(\)[\s\S]*?\n\}/);
  assert.ok(defaultsMatch, "expected to find defaultClientListFilters()");
  assert.match(defaultsMatch![0], /status:\s*ACTIVE_STATUS_DEFAULT/);
  assert.doesNotMatch(defaultsMatch![0], /status:\s*""/);
});

test("the status dropdown itself is untouched -- Any status / Active / Inactive options still present", () => {
  assert.match(CLIENT_LIST_PAGE, /<option value="">Any status<\/option>/);
  assert.match(CLIENT_LIST_PAGE, /CLIENT_STATUS_OPTIONS\.map/);
});

test("every other existing filter/control is still present and untouched", () => {
  assert.match(CLIENT_LIST_PAGE, /Search Clients/);
  assert.match(CLIENT_LIST_PAGE, /Any relationship/);
  assert.match(CLIENT_LIST_PAGE, /placeholder="Owner"/);
  assert.match(CLIENT_LIST_PAGE, /Show archived Clients/);
  assert.match(CLIENT_LIST_PAGE, /function applyRelationship\(/);
  assert.match(CLIENT_LIST_PAGE, /function toggleIncludeArchived\(/);
});

test("pagination is still present and untouched", () => {
  assert.match(CLIENT_LIST_PAGE, /goToPage\(page\.page - 1\)/);
  assert.match(CLIENT_LIST_PAGE, /goToPage\(page\.page \+ 1\)/);
  assert.match(CLIENT_LIST_PAGE, /Page \{page\.page\} of \{totalPages\}/);
});

test("the default sort (next_dinner ascending) is untouched by this stage", () => {
  const defaultsMatch = CLIENT_CRM_TS.match(/export function defaultClientListFilters\(\)[\s\S]*?\n\}/);
  assert.ok(defaultsMatch);
  assert.match(defaultsMatch![0], /sortBy:\s*"next_dinner"/);
  assert.match(defaultsMatch![0], /sortDir:\s*"asc"/);
});
