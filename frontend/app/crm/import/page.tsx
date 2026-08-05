"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Upload } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

const CORE_FIELD_OPTIONS = [
  "apollo_contact_id", "first_name", "last_name", "email", "email_status", "phone", "linkedin_url",
  "title", "company", "company_website", "city", "state", "country", "industry", "company_size",
  "revenue", "funding_stage", "funding_amount", "technologies", "seniority", "department", "job_function",
];

const THESIS_FIELD_OPTIONS = [
  "thesis_cities", "thesis_investor_mode", "thesis_dietary_preferences", "thesis_referral_emails",
  "thesis_private_asset_types", "thesis_private_business_models", "thesis_private_industries",
  "thesis_private_check_sizes", "thesis_private_deal_stages", "thesis_private_meeting_preferences",
  "thesis_private_demographic_preferences", "thesis_private_other_criteria",
  "thesis_also_invests_institutionally",
  "thesis_institutional_asset_types", "thesis_institutional_business_models", "thesis_institutional_industries",
  "thesis_institutional_check_sizes", "thesis_institutional_deal_stages", "thesis_institutional_meeting_preferences",
  "thesis_institutional_demographic_preferences", "thesis_institutional_other_criteria",
];

const STATUS_LABEL: Record<string, string> = {
  new: "New",
  existing: "Existing (will update)",
  possible_duplicate: "Possible duplicate",
  error: "Error",
};

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
                  if (file) handleUpload(file);
                }}
              />
            </label>
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
          <CardContent className="space-y-3">
            {batch.headers.map((header) => (
              <div key={header} className="flex items-center gap-3">
                <span className="w-48 shrink-0 truncate text-sm">{header}</span>
                <select
                  value={mapping[header] ?? ""}
                  onChange={(e) => setMapping((prev) => ({ ...prev, [header]: e.target.value }))}
                  className="h-9 flex-1 rounded-md border border-input bg-transparent px-3 text-sm"
                >
                  <option value="">-- ignore this column --</option>
                  <optgroup label="Core / source fields">
                    {CORE_FIELD_OPTIONS.map((f) => (
                      <option key={f} value={f}>{f}</option>
                    ))}
                  </optgroup>
                  <optgroup label="Investor Thesis fields">
                    {THESIS_FIELD_OPTIONS.map((f) => (
                      <option key={f} value={f}>{f}</option>
                    ))}
                  </optgroup>
                  {customFields.length > 0 && (
                    <optgroup label="Custom fields">
                      {customFields.map((f) => (
                        <option key={f.field_key} value={`custom:${f.field_key}`}>{f.label}</option>
                      ))}
                    </optgroup>
                  )}
                </select>
              </div>
            ))}
            <Button onClick={handlePreview} disabled={busy} className="mt-2">
              {busy ? "Checking for duplicates..." : "Preview import"}
            </Button>
          </CardContent>
        </Card>
      )}

      {batch && batch.preview && !report && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">3. Review &amp; commit</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge variant="outline">New: {batch.new_count}</Badge>
              <Badge variant="outline">Existing: {batch.existing_count}</Badge>
              <Badge variant="outline">Possible duplicates: {batch.possible_duplicate_count}</Badge>
              <Badge variant="outline">Errors: {batch.error_count}</Badge>
            </div>

            <div className="max-h-96 space-y-1 overflow-y-auto rounded-lg border border-border/60 p-2">
              {batch.preview.map((row) => (
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
                    onChange={(e) => setDecisions((prev) => ({ ...prev, [String(row.row_index)]: e.target.value }))}
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

            <Button onClick={handleCommit} disabled={busy}>
              {busy ? "Committing..." : "Commit import"}
            </Button>
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
