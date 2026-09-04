import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Source-level regression coverage for Campaign Manager's Add Prospects
// flow (Stage 4B, 2026-09-03) -- same source-level-assertion pattern as
// mail-campaign-channels.test.ts/mail-campaign-schedule.test.ts, since
// this project has no DOM render harness (see package.json's test script).
// Complements lib/add-prospects-flow.test.ts, which covers the pure
// reducer/logic; this file proves the COMPONENTS actually wire that logic
// to the real endpoints/fields, and never fabricate anything.

const MODAL_SOURCE = readFileSync(new URL("../components/add-prospects-modal.tsx", import.meta.url), "utf-8");
const LEADS_TAB_SOURCE = readFileSync(new URL("../components/mail-campaign-leads-tab.tsx", import.meta.url), "utf-8");
const WORKLOAD_SOURCE = readFileSync(new URL("../components/mail-campaign-workload-summary.tsx", import.meta.url), "utf-8");
const BATCH_HISTORY_SOURCE = readFileSync(new URL("../components/mail-campaign-batch-history.tsx", import.meta.url), "utf-8");
const UPLOAD_STEP_SOURCE = readFileSync(new URL("../components/crm-import/csv-upload-step.tsx", import.meta.url), "utf-8");
const MAPPING_STEP_SOURCE = readFileSync(new URL("../components/crm-import/csv-column-mapping-step.tsx", import.meta.url), "utf-8");
const REVIEW_STEP_SOURCE = readFileSync(new URL("../components/crm-import/csv-review-step.tsx", import.meta.url), "utf-8");
const CRM_IMPORT_PAGE_SOURCE = readFileSync(new URL("../app/crm/import/page.tsx", import.meta.url), "utf-8");
const PAGE_SOURCE = readFileSync(new URL("../app/manager/campaigns/mail/[id]/page.tsx", import.meta.url), "utf-8");
const API_SOURCE = readFileSync(new URL("./api.ts", import.meta.url), "utf-8");

// Strips // line comments and /* */ block comments so "forbidden word"
// checks below can't false-positive on a comment explaining that a field
// deliberately does NOT exist (e.g. "no Delivered field here").
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

const ALL_ADD_PROSPECTS_UI_SOURCE = [MODAL_SOURCE, LEADS_TAB_SOURCE, WORKLOAD_SOURCE, BATCH_HISTORY_SOURCE];

// --- Add Prospects entry point ---------------------------------------------

test("the Leads tab renders an Add Prospects button gated on isAddProspectsEligible, not an inline status check", () => {
  assert.match(LEADS_TAB_SOURCE, /Add Prospects/);
  assert.match(LEADS_TAB_SOURCE, /isAddProspectsEligible/);
  assert.match(LEADS_TAB_SOURCE, /AddProspectsModal/);
});

test("the legacy Apollo campaign page is never touched by Add Prospects", () => {
  const legacyPage = readFileSync(new URL("../app/manager/campaigns/[id]/page.tsx", import.meta.url), "utf-8");
  assert.doesNotMatch(legacyPage, /AddProspectsModal/);
  assert.doesNotMatch(legacyPage, /Add Prospects/);
});

test("the Add Prospects modal uses the existing Dialog system, not a bespoke overlay", () => {
  assert.match(MODAL_SOURCE, /from "@\/components\/ui\/dialog"/);
  assert.match(MODAL_SOURCE, /<Dialog /);
  assert.match(MODAL_SOURCE, /from "@\/components\/ui\/tabs"/);
});

// --- CRM List tab reuses the existing route, no second API ------------------

test("the CRM List tab calls the existing addProspectsFromCrmList wrapper, never a hand-rolled fetch", () => {
  assert.match(MODAL_SOURCE, /addProspectsFromCrmList\(/);
});

test("addProspectsFromCrmList posts to the existing, unmodified POST .../prospects route", () => {
  assert.match(API_SOURCE, /addProspectsFromCrmList[\s\S]{0,300}\/mail\/campaigns\/\$\{mailCampaignId\}\/prospects`/);
  assert.match(API_SOURCE, /source:\s*"crm_list"/);
});

// --- CRM List tab: idempotency-key lifecycle, traced structurally ----------
//
// The CRM List tab has no reducer (it's a handful of independent useState
// hooks, like CreateMailCampaignModal's own convention) -- so unlike the
// CSV flow, its key-lifecycle guarantees can't be unit-tested as pure
// state transitions. These tests instead prove the same guarantees by
// examining the actual call sites in the component source.

test("crmListKey is a separate useState from sourceListId -- selecting/changing the list can never touch the key", () => {
  assert.match(MODAL_SOURCE, /const \[sourceListId, setSourceListId\] = useState/);
  assert.match(MODAL_SOURCE, /const \[crmListKey, setCrmListKey\] = useState/);
  // setSourceListId is called from exactly one place: the <select>'s
  // onChange -- never from inside handleAddFromCrmList, which is the only
  // place crmListKey is established/reused.
  const handlerMatch = MODAL_SOURCE.match(/async function handleAddFromCrmList\(\)[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "handleAddFromCrmList not found");
  assert.doesNotMatch(handlerMatch[0], /setSourceListId/);
});

test("setCrmListKey is called in exactly the two expected places: establish-on-submit and reset-on-close -- never on error", () => {
  const setCalls = [...MODAL_SOURCE.matchAll(/setCrmListKey\(/g)];
  assert.equal(setCalls.length, 2, `expected exactly 2 setCrmListKey( call sites, found ${setCalls.length}`);

  const handlerMatch = MODAL_SOURCE.match(/async function handleAddFromCrmList\(\)[\s\S]*?\n  \}/);
  assert.ok(handlerMatch);
  const handlerBody = handlerMatch[0];
  // The one call inside the handler must be the reuse-if-present pattern,
  // and must sit OUTSIDE the try/catch -- i.e. before the network call,
  // not something a caught error could roll back or replace.
  assert.match(handlerBody, /const key = crmListKey \?\? crypto\.randomUUID\(\);\s*\n\s*setCrmListKey\(key\);\s*\n\s*try \{/);
  const catchMatch = handlerBody.match(/catch \(err\) \{[\s\S]*?\n {4}\}/);
  assert.ok(catchMatch, "catch block not found in handleAddFromCrmList");
  assert.doesNotMatch(catchMatch[0], /setCrmListKey/);
});

test("a retry (handleAddFromCrmList called again after a failure) reuses crmListKey via the same reuse-if-present expression, not a fresh uuid", () => {
  assert.match(MODAL_SOURCE, /const key = crmListKey \?\? crypto\.randomUUID\(\);/);
});

test("the CRM List submit form and button unmount entirely once a result exists -- no UI path re-invokes it without closing the modal", () => {
  assert.match(MODAL_SOURCE, /\{crmListResult \? \(\s*\n\s*<BatchResultPanel result=\{crmListResult\} \/>\s*\n\s*\) : \(/);
});

test("the CRM List submit is guarded by busy AND requires a selection, checked before any state mutation", () => {
  const handlerMatch = MODAL_SOURCE.match(/async function handleAddFromCrmList\(\)[\s\S]*?\n  \}/);
  assert.ok(handlerMatch);
  assert.match(handlerMatch[0].trimStart(), /^async function handleAddFromCrmList\(\) \{\s*\n\s*if \(!sourceListId \|\| crmListBusy\) return;/);
});

// --- CSV tab: exactly the 3 approved endpoints, never a direct commit ------

test("the CSV tab calls uploadCrmImport, previewCrmImport, and addProspectsFromCsv -- never commitCrmImport", () => {
  assert.match(MODAL_SOURCE, /uploadCrmImport\(/);
  assert.match(MODAL_SOURCE, /previewCrmImport\(/);
  assert.match(MODAL_SOURCE, /addProspectsFromCsv\(/);
  assert.doesNotMatch(stripComments(MODAL_SOURCE), /commitCrmImport/);
});

test("addProspectsFromCsv posts to the new orchestration route, a genuinely separate path from /prospects", () => {
  assert.match(API_SOURCE, /addProspectsFromCsv[\s\S]{0,400}\/mail\/campaigns\/\$\{mailCampaignId\}\/prospects\/csv`/);
});

test("the modal never constructs a /crm/import/{id}/commit URL itself", () => {
  assert.doesNotMatch(stripComments(MODAL_SOURCE), /\/commit/);
});

// --- CSV step gating: a retry after failure can't re-trigger upload/preview

test("the three CSV step components are rendered on mutually exclusive step values -- upload/mapping/review never overlap", () => {
  assert.match(MODAL_SOURCE, /\{csvState\.step === "upload" && <CsvUploadStep/);
  assert.match(MODAL_SOURCE, /\{csvState\.step === "mapping" && csvState\.batch && \(/);
  assert.match(MODAL_SOURCE, /\{csvState\.step === "review" && csvState\.batch && \(/);
});

test("a confirm_error leaves step as 'review', so retrying renders ONLY CsvReviewStep -- upload/preview can't be re-invoked from the same retry", () => {
  // confirm_error (lib/add-prospects-flow.ts) deliberately never changes
  // `step` away from "review". Since CsvUploadStep/CsvColumnMappingStep
  // are gated on step === "upload"/"mapping" respectively (mutually
  // exclusive with "review", asserted above), a post-failure retry can
  // only re-render CsvReviewStep and re-call handleCsvConfirm -- there is
  // no code path back to uploadCrmImport/previewCrmImport without an
  // explicit reset.
  const flowSource = readFileSync(new URL("./add-prospects-flow.ts", import.meta.url), "utf-8");
  const confirmErrorMatch = flowSource.match(/case "confirm_error":[\s\S]*?return \{([^}]*)\};/);
  assert.ok(confirmErrorMatch, "confirm_error case not found");
  assert.doesNotMatch(confirmErrorMatch[1], /step:/);
});

test("the CSV result panel replaces the entire step UI once step is 'result' -- Confirm cannot be re-invoked post-success", () => {
  assert.match(MODAL_SOURCE, /\{csvState\.step === "result" && csvState\.result \? \(\s*\n\s*<BatchResultPanel result=\{csvState\.result\} \/>\s*\n\s*\) : \(/);
});

// --- POSSIBLE_DUPLICATE decisions preserved exactly -------------------------

test("the shared review step still renders the create/update/skip decision select, unchanged", () => {
  assert.match(REVIEW_STEP_SOURCE, /possible_duplicate/);
  assert.match(REVIEW_STEP_SOURCE, /onDecisionChange/);
  assert.match(REVIEW_STEP_SOURCE, /option value="create"/);
  assert.match(REVIEW_STEP_SOURCE, /option value="update"/);
  assert.match(REVIEW_STEP_SOURCE, /option value="skip"/);
});

test("the modal wires review decisions into the SAME csvFlowReducer set_decision action the reducer tests cover", () => {
  assert.match(MODAL_SOURCE, /type:\s*"set_decision"/);
});

// --- Standalone CRM Import: extraction is behavior-preserving --------------

test("the standalone CRM Import page now composes the three extracted shared step components", () => {
  assert.match(CRM_IMPORT_PAGE_SOURCE, /from "@\/components\/crm-import\/csv-upload-step"/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /from "@\/components\/crm-import\/csv-column-mapping-step"/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /from "@\/components\/crm-import\/csv-review-step"/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /<CsvUploadStep[\s>]/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /<CsvColumnMappingStep[\s>]/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /<CsvReviewStep[\s>]/);
});

test("the standalone CRM Import page still calls commitCrmImport itself -- the ONLY caller that may", () => {
  assert.match(CRM_IMPORT_PAGE_SOURCE, /commitCrmImport\(/);
});

test("the standalone page passes no label overrides to the shared steps -- it relies on their defaults, which equal its original literal button text", () => {
  assert.doesNotMatch(CRM_IMPORT_PAGE_SOURCE, /submitLabel=|busyLabel=|confirmLabel=/);
  assert.match(MAPPING_STEP_SOURCE, /submitLabel = "Preview import"/);
  assert.match(MAPPING_STEP_SOURCE, /busyLabel = "Checking for duplicates\.\.\."/);
  assert.match(REVIEW_STEP_SOURCE, /confirmLabel = "Commit import"/);
  assert.match(REVIEW_STEP_SOURCE, /busyLabel = "Committing\.\.\."/);
});

test("the standalone page still owns unchanged upload/preview/commit handler bodies -- payloads, error formatting, and report display untouched by extraction", () => {
  assert.match(CRM_IMPORT_PAGE_SOURCE, /previewCrmImport\(batch\.import_batch_id, mapping\)/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /commitCrmImport\(batch\.import_batch_id, decisions\)/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /setMapping\(uploaded\.suggested_mapping\)/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Upload failed \(\$\{err\.status\}\): \$\{err\.message\}/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Preview failed \(\$\{err\.status\}\): \$\{err\.message\}/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Commit failed \(\$\{err\.status\}\): \$\{err\.message\}/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Badge>Created: \{report\.created\}/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Badge variant="outline">Updated: \{report\.updated\}/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Badge variant="outline">Skipped: \{report\.skipped\}/);
});

test("the standalone CRM Import page still owns its own numbered headings and final report card unchanged", () => {
  assert.match(CRM_IMPORT_PAGE_SOURCE, /1\. Upload/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /2\. Map columns/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /3\. Review &amp;? ?commit|Review & commit/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /Import complete/);
  assert.match(CRM_IMPORT_PAGE_SOURCE, /report\.created/);
});

test("the shared step components never call an API function themselves -- every network call is caller-injected", () => {
  for (const source of [UPLOAD_STEP_SOURCE, MAPPING_STEP_SOURCE, REVIEW_STEP_SOURCE]) {
    assert.doesNotMatch(source, /uploadCrmImport\(|previewCrmImport\(|commitCrmImport\(|addProspectsFromCsv\(/);
  }
});

// --- Workload: real fields only, lifecycle stays separate -------------------

test("the workload summary renders total plus each WORKLOAD_FIELD_LABELS entry, not hand-picked fields", () => {
  assert.match(WORKLOAD_SOURCE, /from "@\/lib\/add-prospects-flow"/);
  assert.match(WORKLOAD_SOURCE, /WORKLOAD_FIELD_LABELS/);
  assert.match(WORKLOAD_SOURCE, /workload\.total/);
  assert.match(WORKLOAD_SOURCE, /WORKLOAD_FIELD_LABELS\.map/);
  assert.match(WORKLOAD_SOURCE, /workload\[key\]/);
});

test("WORKLOAD_FIELD_LABELS itself covers exactly the six real enrollment-status fields, never total", () => {
  const flowSource = readFileSync(new URL("./add-prospects-flow.ts", import.meta.url), "utf-8");
  const match = flowSource.match(/WORKLOAD_FIELD_LABELS[\s\S]*?=\s*\[([\s\S]*?)\];/);
  assert.ok(match, "WORKLOAD_FIELD_LABELS definition not found");
  const body = match[1];
  for (const field of ["active", "pending", "paused", "completed", "suppressed", "failed"]) {
    assert.match(body, new RegExp(`key:\\s*"${field}"`));
  }
  assert.doesNotMatch(body, /key:\s*"total"/);
});

test("the workload summary never infers or displays a lifecycle status from workload counts", () => {
  assert.doesNotMatch(stripComments(WORKLOAD_SOURCE), /campaign\.status/);
  assert.doesNotMatch(stripComments(WORKLOAD_SOURCE), /mailCampaignStatusLabel/);
});

// --- Batch History: real Stage 3 fields, no raw CSV/ids exposed ------------

test("Batch History never renders import_batch_id or idempotency_key", () => {
  assert.doesNotMatch(stripComments(BATCH_HISTORY_SOURCE), /import_batch_id/);
  assert.doesNotMatch(stripComments(BATCH_HISTORY_SOURCE), /idempotency_key/);
});

test("Batch History never renders raw CSV row contents", () => {
  assert.doesNotMatch(stripComments(BATCH_HISTORY_SOURCE), /\.rows\b/);
  assert.doesNotMatch(stripComments(BATCH_HISTORY_SOURCE), /mapped_fields/);
});

test("Batch History shows source, created date, and the real Stage 3 counts via the shared summary helper", () => {
  assert.match(BATCH_HISTORY_SOURCE, /mailEnrollmentBatchSourceLabel/);
  assert.match(BATCH_HISTORY_SOURCE, /created_at/);
  assert.match(BATCH_HISTORY_SOURCE, /summarizeBatchResult/);
  assert.match(BATCH_HISTORY_SOURCE, /suppressedSubsetNote/);
});

// --- No fake Phase 3 metrics anywhere in this whole surface -----------------

test("no Open/Reply/Bounce/Delivered rate or Journeys UI appears anywhere in the Add Prospects surface", () => {
  for (const source of [...ALL_ADD_PROSPECTS_UI_SOURCE, ...[UPLOAD_STEP_SOURCE, MAPPING_STEP_SOURCE, REVIEW_STEP_SOURCE]]) {
    const code = stripComments(source);
    for (const forbidden of [/open rate/i, /reply rate/i, /bounce rate/i, /delivered/i, /\bjourneys?\b/i, /unsubscribe rate/i]) {
      assert.doesNotMatch(code, forbidden);
    }
  }
});

test("the Leads tab still renders real MailEnrollment identity/status fields only, no invented ones", () => {
  assert.match(LEADS_TAB_SOURCE, /enrollment\.email_at_enrollment/);
  assert.match(LEADS_TAB_SOURCE, /enrollment\.status/);
  assert.doesNotMatch(stripComments(LEADS_TAB_SOURCE), /opened_at|replied_at|bounced_at|delivered_at/);
});

// --- Result counts: real Stage 3 semantics, suppression as subset ---------

test("the modal's result panel uses summarizeBatchResult/suppressedSubsetNote, never hand-computed counts", () => {
  assert.match(MODAL_SOURCE, /summarizeBatchResult/);
  assert.match(MODAL_SOURCE, /suppressedSubsetNote/);
});

test("the result panel labels submitted_count as usable contacts, not a raw 'Submitted' count", () => {
  assert.match(MODAL_SOURCE, /Usable contacts/);
  assert.doesNotMatch(stripComments(MODAL_SOURCE), /\bSubmitted\b/);
});

// --- Idempotency: modal uses crypto.randomUUID, key not regenerated per click -

test("the modal generates the idempotency key via crypto.randomUUID and only when one doesn't already exist", () => {
  assert.match(MODAL_SOURCE, /crypto\.randomUUID\(\)/);
  assert.match(MODAL_SOURCE, /idempotencyKey \?\? crypto\.randomUUID\(\)|crmListKey \?\? crypto\.randomUUID\(\)/);
});

test("the CSV flow's idempotency/retry/double-submit guarantees are delegated to the tested reducer, not reimplemented inline", () => {
  assert.match(MODAL_SOURCE, /from "@\/lib\/add-prospects-flow"/);
  assert.match(MODAL_SOURCE, /useReducer\(csvFlowReducer/);
});

// --- Page wiring: refresh after success, no lifecycle/config reset --------

test("the campaign detail page refreshes enrollments/workload/batches after Add Prospects succeeds", () => {
  assert.match(PAGE_SOURCE, /onProspectsAdded=\{refreshLeadsSection\}/);
  assert.match(PAGE_SOURCE, /async function refreshLeadsSection/);
  assert.match(PAGE_SOURCE, /getMailCampaignWorkload\(campaignId\)/);
  assert.match(PAGE_SOURCE, /listMailCampaignBatches\(campaignId\)/);
});

test("refreshLeadsSection also refetches the campaign object itself -- so a legacy COMPLETED reopened to ACTIVE by the backend is never shown as stale", () => {
  const match = PAGE_SOURCE.match(/async function refreshLeadsSection\(\)[\s\S]*?\n  \}/);
  assert.ok(match, "refreshLeadsSection function body not found");
  const body = match[0];
  assert.match(body, /getMailCampaign\(campaignId\)/);
  assert.match(body, /setCampaign\(c\)/);
});

test("refreshLeadsSection never touches steps/schedule/channels/settings state", () => {
  const match = PAGE_SOURCE.match(/async function refreshLeadsSection\(\)[\s\S]*?\n  \}/);
  assert.ok(match, "refreshLeadsSection function body not found");
  const body = match[0];
  for (const forbidden of ["setSteps(", "setWindows(", "setSelectedMailboxIds(", "setSharing(", "setDailyLeadStartLimit("]) {
    assert.doesNotMatch(body, new RegExp(forbidden.replace(/[()]/g, "\\$&")));
  }
});
