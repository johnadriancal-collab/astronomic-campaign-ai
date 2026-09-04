"use client";

import { useEffect, useReducer, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPopup,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsPanel, TabsTab } from "@/components/ui/tabs";
import { CsvColumnMappingStep } from "@/components/crm-import/csv-column-mapping-step";
import { CsvReviewStep } from "@/components/crm-import/csv-review-step";
import { CsvUploadStep } from "@/components/crm-import/csv-upload-step";
import {
  csvFlowReducer,
  formatApiErrorMessage,
  initialCsvFlowState,
  suppressedSubsetNote,
  summarizeBatchResult,
} from "@/lib/add-prospects-flow";
import {
  addProspectsFromCrmList,
  addProspectsFromCsv,
  ApiError,
  listCrmCustomFields,
  previewCrmImport,
  uploadCrmImport,
  type CrmContactListSummary,
  type CrmCustomFieldDefinition,
  type MailEnrollmentBatch,
} from "@/lib/api";

// Campaign Manager's native Add Prospects entry point (Stage 4B,
// 2026-09-03): Campaign -> Leads -> Add Prospects -> CRM List | Upload CSV.
// The user never sees an import_batch_id or idempotency key, never visits
// /crm/import, and never leaves this modal. Underneath: the CRM List tab
// reuses the existing, UNCHANGED POST /prospects route; the Upload CSV tab
// reuses the existing, UNCHANGED /crm/import/upload and /crm/import/{id}/
// preview routes for its first two steps, then hands off to the ONE new
// orchestration route (POST /prospects/csv) for commit + campaign attach
// -- this modal never calls /crm/import/{id}/commit itself, by design (see
// that route's own backend docstring for why: it owns commit atomically
// with the campaign-batch idempotency link).

export function AddProspectsModal({
  open,
  onOpenChange,
  campaignId,
  lists,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  campaignId: string;
  lists: CrmContactListSummary[];
  onSuccess: (batch: MailEnrollmentBatch) => void;
}) {
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[]>([]);

  // --- CRM List tab -- simple form state, same double-submit-guard
  // convention as CreateMailCampaignModal (`if (busy) return`), and the
  // same "generate the key lazily, once, keep it for any retry within
  // this modal session" rule as the CSV flow, just without a reducer
  // (this tab has no multi-step state worth modeling as one).
  const [sourceListId, setSourceListId] = useState("");
  const [crmListKey, setCrmListKey] = useState<string | null>(null);
  const [crmListBusy, setCrmListBusy] = useState(false);
  const [crmListError, setCrmListError] = useState<string | null>(null);
  const [crmListResult, setCrmListResult] = useState<MailEnrollmentBatch | null>(null);

  // --- Upload CSV tab -- see lib/add-prospects-flow.ts's own docstring
  // for the full state-machine contract (stable idempotency key, retry-
  // preserves-state, double-submit guard). Lives here, at the MODAL
  // level, not inside a per-tab-mounted child, so switching to the CRM
  // List tab and back never touches it.
  const [csvState, dispatchCsv] = useReducer(csvFlowReducer, initialCsvFlowState);

  useEffect(() => {
    if (!open) return;
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
  }, [open]);

  function handleOpenChange(next: boolean) {
    // Closing intentionally resets the entire flow (both tabs) -- see
    // this modal's own docstring. Reopening starts fresh: a new
    // idempotency key, a new import if the user uploads again. Ignored
    // while a request is in flight, matching CreateMailCampaignModal's
    // own close-while-busy convention.
    if (!next && !crmListBusy && !csvState.busy) {
      setSourceListId("");
      setCrmListKey(null);
      setCrmListError(null);
      setCrmListResult(null);
      dispatchCsv({ type: "reset" });
    }
    if (next || (!crmListBusy && !csvState.busy)) onOpenChange(next);
  }

  async function handleAddFromCrmList() {
    if (!sourceListId || crmListBusy) return;
    setCrmListBusy(true);
    setCrmListError(null);
    const key = crmListKey ?? crypto.randomUUID();
    setCrmListKey(key);
    try {
      const result = await addProspectsFromCrmList(campaignId, sourceListId, key);
      setCrmListResult(result);
      onSuccess(result);
    } catch (err) {
      setCrmListError(err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.");
    } finally {
      setCrmListBusy(false);
    }
  }

  async function handleCsvUpload(file: File) {
    const key = csvState.idempotencyKey ?? crypto.randomUUID();
    dispatchCsv({ type: "upload_start", idempotencyKey: key });
    try {
      const uploaded = await uploadCrmImport(file);
      dispatchCsv({ type: "upload_success", batch: uploaded });
    } catch (err) {
      dispatchCsv({
        type: "upload_error",
        error: err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.",
      });
    }
  }

  async function handleCsvPreview() {
    if (!csvState.batch) return;
    dispatchCsv({ type: "preview_start" });
    try {
      const previewed = await previewCrmImport(csvState.batch.import_batch_id, csvState.mapping);
      dispatchCsv({ type: "preview_success", batch: previewed });
    } catch (err) {
      dispatchCsv({
        type: "preview_error",
        error: err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.",
      });
    }
  }

  async function handleCsvConfirm() {
    if (!csvState.batch || !csvState.idempotencyKey) return;
    dispatchCsv({ type: "confirm_start" });
    try {
      const result = await addProspectsFromCsv(campaignId, csvState.batch.import_batch_id, csvState.idempotencyKey, csvState.decisions);
      dispatchCsv({ type: "confirm_success", result });
      onSuccess(result);
    } catch (err) {
      dispatchCsv({
        type: "confirm_error",
        error: err instanceof ApiError ? formatApiErrorMessage(err.message) : "Couldn't reach the backend.",
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPopup className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add Prospects</DialogTitle>
          <DialogDescription>Add contacts to this campaign from an existing CRM List, or by uploading a CSV.</DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="crm-list">
          <TabsList>
            <TabsTab value="crm-list">CRM List</TabsTab>
            <TabsTab value="csv">Upload CSV</TabsTab>
          </TabsList>

          <TabsPanel value="crm-list">
            {crmListResult ? (
              <BatchResultPanel result={crmListResult} />
            ) : (
              <div className="space-y-4">
                {crmListError && (
                  <Alert variant="destructive">
                    <AlertDescription>{crmListError}</AlertDescription>
                  </Alert>
                )}
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-muted-foreground">CRM List</label>
                  <select
                    value={sourceListId}
                    onChange={(e) => setSourceListId(e.target.value)}
                    disabled={crmListBusy}
                    className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <option value="">-- choose a CRM List --</option>
                    {lists.map((l) => (
                      <option key={l.list_id} value={l.list_id}>
                        {l.name} ({l.contact_count} contact{l.contact_count === 1 ? "" : "s"})
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">
                    Adds the list&apos;s current members to this campaign. Contacts already in this campaign are skipped.
                  </p>
                </div>
                <Button onClick={handleAddFromCrmList} disabled={!sourceListId || crmListBusy}>
                  {crmListBusy ? "Adding..." : "Add Prospects"}
                </Button>
              </div>
            )}
          </TabsPanel>

          <TabsPanel value="csv">
            {csvState.step === "result" && csvState.result ? (
              <BatchResultPanel result={csvState.result} />
            ) : (
              <div className="space-y-4">
                {csvState.error && (
                  <Alert variant="destructive">
                    <AlertDescription>{csvState.error}</AlertDescription>
                  </Alert>
                )}

                {csvState.step === "upload" && <CsvUploadStep busy={csvState.busy} onUpload={handleCsvUpload} />}

                {csvState.step === "mapping" && csvState.batch && (
                  <div className="space-y-2">
                    <p className="text-sm text-muted-foreground">
                      {csvState.batch.row_count} row{csvState.batch.row_count === 1 ? "" : "s"} in {csvState.batch.filename}
                    </p>
                    <CsvColumnMappingStep
                      batch={csvState.batch}
                      mapping={csvState.mapping}
                      onMappingChange={(mapping) => dispatchCsv({ type: "set_mapping", mapping })}
                      customFields={customFields}
                      busy={csvState.busy}
                      onSubmit={handleCsvPreview}
                    />
                  </div>
                )}

                {csvState.step === "review" && csvState.batch && (
                  <CsvReviewStep
                    batch={csvState.batch}
                    decisions={csvState.decisions}
                    onDecisionChange={(rowIndex, decision) => dispatchCsv({ type: "set_decision", rowIndex, decision })}
                    busy={csvState.busy}
                    onConfirm={handleCsvConfirm}
                    confirmLabel="Add Prospects"
                    busyLabel="Adding..."
                  />
                )}
              </div>
            )}
          </TabsPanel>
        </Tabs>

        <DialogFooter>
          <DialogClose
            disabled={crmListBusy || csvState.busy}
            render={
              <Button type="button" variant="outline">
                {crmListResult || csvState.step === "result" ? "Done" : "Cancel"}
              </Button>
            }
          />
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

function BatchResultPanel({ result }: { result: MailEnrollmentBatch }) {
  const summary = summarizeBatchResult(result);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2 text-sm">
        <Badge>Usable contacts: {summary.usableContacts}</Badge>
        <Badge variant="outline">Newly added to this campaign: {summary.newlyAdded}</Badge>
        <Badge variant="outline">Already in this campaign: {summary.alreadyInCampaign}</Badge>
      </div>
      {summary.suppressedOfNewlyAdded > 0 && (
        <p className="text-xs text-muted-foreground">{suppressedSubsetNote(summary)}</p>
      )}
    </div>
  );
}
