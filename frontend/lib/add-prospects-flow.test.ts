import assert from "node:assert/strict";
import { test } from "node:test";
import {
  csvFlowReducer,
  formatApiErrorMessage,
  initialCsvFlowState,
  isAddProspectsEligible,
  mailEnrollmentBatchSourceLabel,
  summarizeBatchResult,
  suppressedSubsetNote,
  WORKLOAD_FIELD_LABELS,
  type CsvFlowState,
} from "./add-prospects-flow.ts";
import type { CrmImportBatch, MailEnrollmentBatch } from "./api.ts";

// --- Eligibility -------------------------------------------------------

test("isAddProspectsEligible is true only for active, paused, and legacy completed", () => {
  assert.equal(isAddProspectsEligible("active"), true);
  assert.equal(isAddProspectsEligible("paused"), true);
  assert.equal(isAddProspectsEligible("completed"), true);
  assert.equal(isAddProspectsEligible("draft"), false);
  assert.equal(isAddProspectsEligible("ready"), false);
  assert.equal(isAddProspectsEligible("archived"), false);
});

// --- Result summary / suppression subset wording ------------------------

test("summarizeBatchResult maps submitted_count to usableContacts and the rest 1:1", () => {
  const summary = summarizeBatchResult({
    submitted_count: 490,
    enrolled_count: 450,
    already_enrolled_count: 40,
    suppressed_count: 20,
  });
  assert.deepEqual(summary, { usableContacts: 490, newlyAdded: 450, alreadyInCampaign: 40, suppressedOfNewlyAdded: 20 });
});

test("summarizeBatchResult treats null counts (still-PREPARING batch) as zero, never NaN/undefined", () => {
  const summary = summarizeBatchResult({
    submitted_count: null,
    enrolled_count: null,
    already_enrolled_count: null,
    suppressed_count: null,
  });
  assert.deepEqual(summary, { usableContacts: 0, newlyAdded: 0, alreadyInCampaign: 0, suppressedOfNewlyAdded: 0 });
});

test("suppressedSubsetNote phrases suppression as a subset of newly added, never a fifth additive category", () => {
  const note = suppressedSubsetNote({ usableContacts: 490, newlyAdded: 450, alreadyInCampaign: 40, suppressedOfNewlyAdded: 20 });
  assert.equal(note, "20 of the newly added are suppressed and won't receive this sequence.");
  assert.doesNotMatch(note, /submitted/i);
});

test("the invariant submitted_count == enrolled_count + already_enrolled_count holds for the approved worked example", () => {
  const summary = summarizeBatchResult({ submitted_count: 490, enrolled_count: 450, already_enrolled_count: 40, suppressed_count: 20 });
  assert.equal(summary.usableContacts, summary.newlyAdded + summary.alreadyInCampaign);
  assert.ok(summary.suppressedOfNewlyAdded <= summary.newlyAdded);
});

// --- Batch History source label ------------------------------------------

test("mailEnrollmentBatchSourceLabel", () => {
  assert.equal(mailEnrollmentBatchSourceLabel("crm_list"), "CRM List");
  assert.equal(mailEnrollmentBatchSourceLabel("csv_upload"), "CSV Upload");
});

// --- Workload fields ---------------------------------------------------

test("WORKLOAD_FIELD_LABELS covers exactly the real backend fields, never total/mail_campaign_id, never an invented metric", () => {
  const keys = WORKLOAD_FIELD_LABELS.map((f) => f.key).sort();
  assert.deepEqual(keys, ["active", "completed", "failed", "paused", "pending", "suppressed"]);
});

// --- CSV flow reducer: idempotency key stability --------------------------

function upload(state: CsvFlowState, key: string): CsvFlowState {
  return csvFlowReducer(state, { type: "upload_start", idempotencyKey: key });
}

const sampleBatch: CrmImportBatch = {
  import_batch_id: "b1",
  filename: "p.csv",
  uploaded_at: "2026-09-03T00:00:00Z",
  status: "uploaded",
  headers: ["Email"],
  rows: [],
  row_count: 1,
  suggested_mapping: { Email: "email" },
  column_mapping: null,
  preview: null,
  new_count: null,
  existing_count: null,
  possible_duplicate_count: null,
  error_count: null,
};

test("the idempotency key is set on the first upload_start and never changes after", () => {
  let state = upload(initialCsvFlowState, "key-1");
  assert.equal(state.idempotencyKey, "key-1");

  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });
  state = csvFlowReducer(state, { type: "preview_start" });
  state = csvFlowReducer(state, { type: "preview_success", batch: { ...sampleBatch, preview: [] } });
  state = csvFlowReducer(state, { type: "confirm_start" });
  state = csvFlowReducer(state, { type: "confirm_error", error: "boom" });

  // A second upload_start (e.g. a bug that re-dispatched it) must NOT
  // overwrite the already-seeded key.
  state = upload(state, "key-2-should-never-win");
  assert.equal(state.idempotencyKey, "key-1");
});

test("reset is the only action that clears the idempotency key", () => {
  let state = upload(initialCsvFlowState, "key-1");
  state = csvFlowReducer(state, { type: "reset" });
  assert.equal(state.idempotencyKey, null);
  assert.deepEqual(state, initialCsvFlowState);
});

test("close-then-reopen (reset, then a fresh upload_start) gets a genuinely new key and does not resume the abandoned import", () => {
  let state = upload(initialCsvFlowState, "old-key");
  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });
  state = csvFlowReducer(state, { type: "set_mapping", mapping: { Email: "email" } });
  state = csvFlowReducer(state, { type: "set_decision", rowIndex: 0, decision: "create" });

  state = csvFlowReducer(state, { type: "reset" });
  // Reopening the modal and uploading a new file dispatches a fresh
  // upload_start with a newly-generated uuid -- the reducer must not
  // silently resume the old batch/mapping/decisions under the new key.
  state = upload(state, "new-key");
  assert.equal(state.idempotencyKey, "new-key");
  assert.notEqual(state.idempotencyKey, "old-key");
  assert.deepEqual(state.mapping, {});
  assert.deepEqual(state.decisions, {});
  assert.equal(state.batch, null);
  assert.equal(state.step, "upload");
});

// --- Retry after a final-step (confirm) failure preserves everything ------

test("confirm_error preserves step/batch/mapping/decisions/idempotencyKey so Confirm can be retried with the same operation identity", () => {
  let state = upload(initialCsvFlowState, "stable-key");
  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });
  state = csvFlowReducer(state, { type: "set_mapping", mapping: { Email: "email" } });
  state = csvFlowReducer(state, { type: "preview_start" });
  const previewed = { ...sampleBatch, status: "mapped" as const, preview: [] };
  state = csvFlowReducer(state, { type: "preview_success", batch: previewed });
  state = csvFlowReducer(state, { type: "set_decision", rowIndex: 0, decision: "create" });
  state = csvFlowReducer(state, { type: "confirm_start" });

  const beforeError = state;
  state = csvFlowReducer(state, { type: "confirm_error", error: "Network error" });

  assert.equal(state.step, "review");
  assert.equal(state.idempotencyKey, "stable-key");
  assert.deepEqual(state.batch, beforeError.batch);
  assert.deepEqual(state.mapping, beforeError.mapping);
  assert.deepEqual(state.decisions, { "0": "create" });
  assert.equal(state.busy, false);
  assert.equal(state.error, "Network error");

  // The retry itself reuses the SAME key -- confirm_start doesn't touch it.
  const retrying = csvFlowReducer(state, { type: "confirm_start" });
  assert.equal(retrying.idempotencyKey, "stable-key");
  assert.equal(retrying.busy, true);
  assert.equal(retrying.error, null);
});

// --- A completed result is a terminal state, except for an explicit reset --

test("once confirm_success reaches step 'result', no action but reset can move it, and the result itself never mutates", () => {
  let state = upload(initialCsvFlowState, "key-1");
  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });
  state = csvFlowReducer(state, { type: "preview_start" });
  state = csvFlowReducer(state, { type: "preview_success", batch: { ...sampleBatch, preview: [] } });
  state = csvFlowReducer(state, { type: "confirm_start" });
  const result: MailEnrollmentBatch = {
    batch_id: "batch-1",
    mail_campaign_id: "campaign-1",
    source: "csv_upload",
    source_list_id: null,
    source_import_batch_id: "b1",
    idempotency_key: "key-1",
    status: "ready",
    created_at: "2026-09-04T00:00:00Z",
    created_by_actor: null,
    submitted_count: 10,
    enrolled_count: 9,
    already_enrolled_count: 1,
    suppressed_count: 0,
  };
  state = csvFlowReducer(state, { type: "confirm_success", result });
  assert.equal(state.step, "result");

  // Simulate every "in-flight" action a stray double-click or retry could
  // dispatch against a completed flow -- none may re-enter "review" or
  // re-run the confirm request, and the stored result must stay untouched.
  for (const action of [
    { type: "confirm_start" as const },
    { type: "preview_start" as const },
    { type: "set_decision" as const, rowIndex: 0, decision: "skip" },
  ]) {
    const after = csvFlowReducer(state, action);
    assert.equal(after.step, "result");
    assert.deepEqual(after.result, result);
  }

  // Only reset ends the terminal "result" state.
  const afterReset = csvFlowReducer(state, { type: "reset" });
  assert.equal(afterReset.step, "upload");
  assert.equal(afterReset.result, null);
});

// --- Double-submit protection ---------------------------------------------

test("upload_start no-ops while already busy (protects against a double-click racing ahead of a state update)", () => {
  const busyState = upload(initialCsvFlowState, "key-1");
  assert.equal(busyState.busy, true);

  const secondClick = upload(busyState, "key-should-be-ignored");
  assert.deepEqual(secondClick, busyState); // truly a no-op, not just "same key"
});

test("preview_start and confirm_start also no-op while busy", () => {
  let state = upload(initialCsvFlowState, "key-1");
  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });

  const previewing = csvFlowReducer(state, { type: "preview_start" });
  const doublePreview = csvFlowReducer(previewing, { type: "preview_start" });
  assert.deepEqual(doublePreview, previewing);

  const ready = csvFlowReducer(previewing, { type: "preview_success", batch: { ...sampleBatch, preview: [] } });
  const confirming = csvFlowReducer(ready, { type: "confirm_start" });
  const doubleConfirm = csvFlowReducer(confirming, { type: "confirm_start" });
  assert.deepEqual(doubleConfirm, confirming);
});

// --- Tab-switch survival (structural: no action = no change) --------------

test("nothing about switching tabs is modeled as a reducer action -- CSV state is untouched by anything except its own actions", () => {
  // There is no 'switch_tab' action in CsvFlowAction at all -- structurally,
  // a tab switch can only ever be a UI navigation event that never
  // dispatches to this reducer, so in-progress CSV state (including the
  // idempotency key) survives by construction. This test documents that
  // invariant by confirming an unrelated action never mutates unrelated
  // fields -- including mapping and decisions, which a switch_tab-modeled
  // implementation could plausibly have dropped.
  let state = upload(initialCsvFlowState, "key-1");
  state = csvFlowReducer(state, { type: "upload_success", batch: sampleBatch });
  state = csvFlowReducer(state, { type: "set_mapping", mapping: { Email: "email" } });
  state = csvFlowReducer(state, { type: "preview_start" });
  state = csvFlowReducer(state, { type: "preview_success", batch: { ...sampleBatch, preview: [] } });
  state = csvFlowReducer(state, { type: "set_decision", rowIndex: 0, decision: "create" });
  const before = state;

  // set_decision on a DIFFERENT row is the "unrelated action" here --
  // simulating any further in-tab interaction that happens after a
  // (simulated) round trip through the CRM List tab and back.
  const after = csvFlowReducer(state, { type: "set_decision", rowIndex: 1, decision: "skip" });
  assert.equal(after.idempotencyKey, before.idempotencyKey);
  assert.equal(after.batch, before.batch);
  assert.equal(after.step, before.step);
  assert.deepEqual(after.mapping, before.mapping);
  assert.deepEqual(after.decisions, { "0": "create", "1": "skip" });
});

// --- Error formatting: no raw exception/id exposure ------------------------

test("formatApiErrorMessage extracts the FastAPI detail field from a JSON body", () => {
  assert.equal(formatApiErrorMessage('{"detail":"Selected CSV import not found."}'), "Selected CSV import not found.");
});

test("formatApiErrorMessage falls back to the raw text for a non-JSON body, never throws", () => {
  assert.equal(formatApiErrorMessage("Couldn't reach the backend."), "Couldn't reach the backend.");
});

test("formatApiErrorMessage never leaves an empty message", () => {
  assert.equal(formatApiErrorMessage(""), "Something went wrong. Please try again.");
  assert.equal(formatApiErrorMessage("{}"), "Something went wrong. Please try again.");
});
