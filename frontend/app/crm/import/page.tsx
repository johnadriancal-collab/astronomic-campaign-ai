"use client";

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CsvColumnMappingStep } from "@/components/crm-import/csv-column-mapping-step";
import { CsvReviewStep } from "@/components/crm-import/csv-review-step";
import { CsvUploadStep } from "@/components/crm-import/csv-upload-step";
import {
  ApiError,
  commitCrmImport,
  listCrmCustomFields,
  previewCrmImport,
  uploadCrmImport,
  type CrmCustomFieldDefinition,
  type CrmImportBatch,
  type CrmImportReport,
} from "@/lib/api";

export default function CrmImportPage() {
  const [customFields, setCustomFields] = useState<CrmCustomFieldDefinition[]>([]);
  const [batch, setBatch] = useState<CrmImportBatch | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [report, setReport] = useState<CrmImportReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listCrmCustomFields(false).then(setCustomFields).catch(() => setCustomFields([]));
  }, []);

  async function handleUpload(file: File) {
    setBusy(true);
    setError(null);
    try {
      const uploaded = await uploadCrmImport(file);
      setBatch(uploaded);
      setMapping(uploaded.suggested_mapping);
    } catch (err) {
      setError(err instanceof ApiError ? `Upload failed (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handlePreview() {
    if (!batch) return;
    setBusy(true);
    setError(null);
    try {
      const previewed = await previewCrmImport(batch.import_batch_id, mapping);
      setBatch(previewed);
    } catch (err) {
      setError(err instanceof ApiError ? `Preview failed (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  async function handleCommit() {
    if (!batch) return;
    setBusy(true);
    setError(null);
    try {
      const result = await commitCrmImport(batch.import_batch_id, decisions);
      setReport(result);
    } catch (err) {
      setError(err instanceof ApiError ? `Commit failed (${err.status}): ${err.message}` : "Couldn't reach the backend.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <h1 className="mb-2 font-serif text-2xl font-medium tracking-tight">Import CSV</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Upload &rarr; review mappings &amp; duplicates &rarr; commit. Nothing is written to the CRM until you commit.
      </p>

      {error && (
        <Alert variant="destructive" className="mb-4">
          <AlertTriangle />
          <AlertTitle>Something went wrong</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {!batch && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">1. Upload</CardTitle>
          </CardHeader>
          <CardContent>
            <CsvUploadStep busy={busy} onUpload={handleUpload} />
          </CardContent>
        </Card>
      )}

      {batch && !batch.preview && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              2. Map columns -- {batch.row_count} row{batch.row_count === 1 ? "" : "s"} in {batch.filename}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <CsvColumnMappingStep
              batch={batch}
              mapping={mapping}
              onMappingChange={setMapping}
              customFields={customFields}
              busy={busy}
              onSubmit={handlePreview}
            />
          </CardContent>
        </Card>
      )}

      {batch && batch.preview && !report && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">3. Review &amp; commit</CardTitle>
          </CardHeader>
          <CardContent>
            <CsvReviewStep
              batch={batch}
              decisions={decisions}
              onDecisionChange={(rowIndex, decision) => setDecisions((prev) => ({ ...prev, [String(rowIndex)]: decision }))}
              busy={busy}
              onConfirm={handleCommit}
            />
          </CardContent>
        </Card>
      )}

      {report && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Import complete</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2 text-sm">
            <Badge>Created: {report.created}</Badge>
            <Badge variant="outline">Updated: {report.updated}</Badge>
            <Badge variant="outline">Skipped: {report.skipped}</Badge>
            {report.errors > 0 && <Badge variant="destructive">Errors: {report.errors}</Badge>}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
