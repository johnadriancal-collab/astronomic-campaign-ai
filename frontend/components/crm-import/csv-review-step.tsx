"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { CrmImportBatch } from "@/lib/api";

// Extracted from app/crm/import/page.tsx (Stage 4B, 2026-09-03) -- see
// csv-upload-step.tsx's own comment for the shared-component rationale.
// This is the ONE piece both callers most needed to share exactly: CRM
// matching/dedup semantics and POSSIBLE_DUPLICATE review/decision
// behavior must be byte-identical in Campaign Manager and the standalone
// CRM Import page -- see this file's own docstring on why no policy is
// duplicated here, only the review UI.

const STATUS_LABEL: Record<string, string> = {
  new: "New",
  existing: "Existing (will update)",
  possible_duplicate: "Possible duplicate",
  error: "Error",
};

export function CsvReviewStep({
  batch,
  decisions,
  onDecisionChange,
  busy,
  onConfirm,
  confirmLabel = "Commit import",
  busyLabel = "Committing...",
}: {
  batch: CrmImportBatch;
  decisions: Record<string, string>;
  onDecisionChange: (rowIndex: number, decision: string) => void;
  busy: boolean;
  onConfirm: () => void;
  confirmLabel?: string;
  busyLabel?: string;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2 text-sm">
        <Badge variant="outline">New: {batch.new_count}</Badge>
        <Badge variant="outline">Existing: {batch.existing_count}</Badge>
        <Badge variant="outline">Possible duplicates: {batch.possible_duplicate_count}</Badge>
        <Badge variant="outline">Errors: {batch.error_count}</Badge>
      </div>

      <div className="max-h-96 space-y-1 overflow-y-auto rounded-lg border border-border/60 p-2">
        {(batch.preview ?? []).map((row) => (
          <div key={row.row_index} className="flex items-center justify-between gap-3 rounded-md p-2 text-sm hover:bg-secondary/40">
            <div className="min-w-0">
              <p className="truncate">
                {String(row.mapped_fields.first_name ?? "")} {String(row.mapped_fields.last_name ?? "")}{" "}
                <span className="text-muted-foreground">{String(row.mapped_fields.email ?? "")}</span>
              </p>
              <p className="text-xs text-muted-foreground">
                {STATUS_LABEL[row.status]}
                {row.matched_on && !row.matched_on.startsWith("within_file") && ` -- matched by ${row.matched_on}`}
                {row.matched_on?.startsWith("within_file") && " -- duplicate of an earlier row in this file"}
                {row.error && ` -- ${row.error}`}
              </p>
            </div>
            <select
              value={decisions[String(row.row_index)] ?? ""}
              onChange={(e) => onDecisionChange(row.row_index, e.target.value)}
              className="h-8 shrink-0 rounded-md border border-input bg-transparent px-2 text-xs"
            >
              <option value="">
                Default ({row.status === "new" ? "create" : row.status === "existing" ? "update" : "skip"})
              </option>
              <option value="create">Create</option>
              <option value="update">Update</option>
              <option value="skip">Skip</option>
            </select>
          </div>
        ))}
      </div>

      <Button onClick={onConfirm} disabled={busy}>
        {busy ? busyLabel : confirmLabel}
      </Button>
    </div>
  );
}
