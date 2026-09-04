"use client";

import { Upload } from "lucide-react";

// Extracted from app/crm/import/page.tsx (Stage 4B, 2026-09-03) so
// Campaign Manager's Add Prospects -> Upload CSV flow can reuse the exact
// same dropzone/upload interaction instead of a second implementation --
// see that page's own history for why. Deliberately owns only the
// interactive dropzone content, not a Card/heading wrapper -- each caller
// (the standalone page, AddProspectsModal) supplies its own chrome around
// this, so extracting it changes nothing about either caller's own layout.
export function CsvUploadStep({ busy, onUpload }: { busy: boolean; onUpload: (file: File) => void }) {
  return (
    <label className="flex cursor-pointer flex-col items-center gap-2 rounded-xl border border-dashed border-border/60 py-12 text-center text-sm text-muted-foreground hover:bg-secondary/40">
      <Upload className="h-5 w-5" />
      {busy ? "Uploading..." : "Choose a CSV file"}
      <input
        type="file"
        accept=".csv"
        className="hidden"
        disabled={busy}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onUpload(file);
        }}
      />
    </label>
  );
}
