// Pure logic for Campaign Manager's Add Prospects flow (Stage 4B,
// 2026-09-03) -- kept separate from AddProspectsModal so the tricky
// lifecycle guarantees (one stable idempotency key per modal session,
// a retry after a failed final step reuses the same key/frozen CSV
// state, double-submit protection) are unit-testable as pure state
// transitions, same split as every other lib/*.ts file in this project
// (see package.json's test script -- there is no DOM render harness here).

import type { CrmImportBatch, MailCampaignStatus, MailEnrollmentBatch, MailEnrollmentBatchSource } from "@/lib/api";

// --- Eligibility (frontend gating is convenience only -- the backend's own
// authoritative check in MailCampaignService.add_prospects() is what
// actually enforces this; see PROSPECT_ELIGIBLE_CAMPAIGN_STATUSES there) --

export function isAddProspectsEligible(status: MailCampaignStatus): boolean {
  return status === "active" || status === "paused" || status === "completed";
}

// --- Result display (backend Stage 3 count semantics, exactly) ------------
//
// submitted_count is NOT a raw row/membership count -- it's already
// deduped and already live-email-filtered (see MailEnrollmentBatch's own
// backend docstring), true for BOTH crm_list and csv_upload sources alike.
// "Usable contacts" is the truthful label for both; suppressed_count is
// always a SUBSET of enrolled_count, never a fourth additive category.

export interface BatchResultSummary {
  usableContacts: number;
  newlyAdded: number;
  alreadyInCampaign: number;
  suppressedOfNewlyAdded: number;
}

export function summarizeBatchResult(batch: {
  submitted_count: number | null;
  enrolled_count: number | null;
  already_enrolled_count: number | null;
  suppressed_count: number | null;
}): BatchResultSummary {
  return {
    usableContacts: batch.submitted_count ?? 0,
    newlyAdded: batch.enrolled_count ?? 0,
    alreadyInCampaign: batch.already_enrolled_count ?? 0,
    suppressedOfNewlyAdded: batch.suppressed_count ?? 0,
  };
}

export function suppressedSubsetNote(summary: BatchResultSummary): string {
  return `${summary.suppressedOfNewlyAdded} of the newly added are suppressed and won't receive this sequence.`;
}

// --- Batch History -----------------------------------------------------

export function mailEnrollmentBatchSourceLabel(source: MailEnrollmentBatchSource): string {
  return source === "csv_upload" ? "CSV Upload" : "CRM List";
}

// --- Workload -- real backend fields only, `total` shown separately -------

export const WORKLOAD_FIELD_LABELS: { key: "pending" | "active" | "paused" | "completed" | "suppressed" | "failed"; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "pending", label: "Pending" },
  { key: "paused", label: "Paused" },
  { key: "completed", label: "Completed" },
  { key: "suppressed", label: "Suppressed" },
  { key: "failed", label: "Failed" },
];

// --- CSV flow state machine ------------------------------------------------
//
// Lives in the modal (useReducer), NOT remounted per-tab, so switching
// between "CRM List" and "Upload CSV" never touches this state at all --
// only an explicit "reset" action (dispatched on intentional modal close)
// clears it. The idempotencyKey is seeded ONCE, on the first "upload_start"
// action, and is a pure function of PRIOR state from then on -- no action
// after that can ever change it except "reset". A "confirm_error" leaves
// every other field (batch/mapping/decisions/idempotencyKey) untouched so
// Confirm can be retried without re-upload/re-map/re-review. "*_start"
// actions no-op while already busy, guarding against a double-submit
// racing ahead of a not-yet-committed React state update.

export type CsvFlowStep = "upload" | "mapping" | "review" | "result";

export interface CsvFlowState {
  step: CsvFlowStep;
  idempotencyKey: string | null;
  batch: CrmImportBatch | null;
  mapping: Record<string, string>;
  decisions: Record<string, string>;
  result: MailEnrollmentBatch | null;
  error: string | null;
  busy: boolean;
}

export const initialCsvFlowState: CsvFlowState = {
  step: "upload",
  idempotencyKey: null,
  batch: null,
  mapping: {},
  decisions: {},
  result: null,
  error: null,
  busy: false,
};

export type CsvFlowAction =
  | { type: "reset" }
  | { type: "upload_start"; idempotencyKey: string }
  | { type: "upload_success"; batch: CrmImportBatch }
  | { type: "upload_error"; error: string }
  | { type: "set_mapping"; mapping: Record<string, string> }
  | { type: "preview_start" }
  | { type: "preview_success"; batch: CrmImportBatch }
  | { type: "preview_error"; error: string }
  | { type: "set_decision"; rowIndex: number; decision: string }
  | { type: "confirm_start" }
  | { type: "confirm_success"; result: MailEnrollmentBatch }
  | { type: "confirm_error"; error: string };

export function csvFlowReducer(state: CsvFlowState, action: CsvFlowAction): CsvFlowState {
  switch (action.type) {
    case "reset":
      return initialCsvFlowState;

    case "upload_start":
      if (state.busy) return state;
      return {
        ...state,
        busy: true,
        error: null,
        // Seeded once; never overwritten by a later upload_start (there
        // realistically is only ever one per session, but this makes the
        // "stable key" guarantee a property of the reducer itself, not of
        // callers remembering not to re-dispatch with a fresh uuid).
        idempotencyKey: state.idempotencyKey ?? action.idempotencyKey,
      };

    case "upload_success":
      return { ...state, busy: false, error: null, batch: action.batch, mapping: action.batch.suggested_mapping, step: "mapping" };

    case "upload_error":
      return { ...state, busy: false, error: action.error };

    case "set_mapping":
      return { ...state, mapping: action.mapping };

    case "preview_start":
      if (state.busy) return state;
      return { ...state, busy: true, error: null };

    case "preview_success":
      return { ...state, busy: false, error: null, batch: action.batch, step: "review" };

    case "preview_error":
      return { ...state, busy: false, error: action.error };

    case "set_decision":
      return { ...state, decisions: { ...state.decisions, [String(action.rowIndex)]: action.decision } };

    case "confirm_start":
      if (state.busy) return state;
      return { ...state, busy: true, error: null };

    case "confirm_success":
      return { ...state, busy: false, error: null, result: action.result, step: "result" };

    case "confirm_error":
      // Deliberately does NOT reset step/batch/mapping/decisions/
      // idempotencyKey -- see this module's own docstring.
      return { ...state, busy: false, error: action.error };

    default:
      return state;
  }
}

// --- Server error presentation ---------------------------------------------
//
// ApiError.message is the raw response body text -- for a FastAPI
// HTTPException that's a JSON blob like {"detail":"..."}, not a bare
// string (see api.ts's request()). The standalone CRM Import page's OWN
// upload/preview/commit error handling is untouched (still shows that raw
// text verbatim -- see csv-upload-step.tsx/csv-column-mapping-step.tsx/
// csv-review-step.tsx, which don't touch error formatting at all; only
// the PAGE's own catch blocks do, unchanged). AddProspectsModal is a
// wholly separate render, so it's free to use this everywhere within
// itself (CRM List submit, CSV upload/preview/confirm alike) without
// affecting that page at all: parse out `detail` when present, never show
// a raw exception/stack, never surface an internal id the user didn't
// supply themselves (import_batch_id, idempotency_key).
const DEFAULT_API_ERROR_MESSAGE = "Something went wrong. Please try again.";

export function formatApiErrorMessage(rawMessage: string): string {
  const trimmed = rawMessage.trim();
  if (!trimmed) return DEFAULT_API_ERROR_MESSAGE;
  try {
    const parsed = JSON.parse(trimmed) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail;
    // Valid JSON but no usable `detail` string (e.g. "{}") -- never show
    // the raw JSON blob itself, that's exactly the "raw backend exception
    // details" this function exists to avoid surfacing.
    return DEFAULT_API_ERROR_MESSAGE;
  } catch {
    // Not JSON -- a plain-text message (e.g. "Couldn't reach the
    // backend.") is already human-readable, show it as-is.
    return trimmed;
  }
}
