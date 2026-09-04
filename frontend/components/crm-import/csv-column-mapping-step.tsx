"use client";

import { Button } from "@/components/ui/button";
import type { CrmCustomFieldDefinition, CrmImportBatch } from "@/lib/api";

// Extracted from app/crm/import/page.tsx (Stage 4B, 2026-09-03) -- see
// csv-upload-step.tsx's own comment for the shared-component rationale.
// Same "owns interactive content only, not chrome" boundary: the caller
// supplies its own Card/heading around this.

export const CORE_FIELD_OPTIONS = [
  "apollo_contact_id", "first_name", "last_name", "email", "email_status", "phone", "linkedin_url",
  "title", "company", "company_website", "city", "state", "country", "industry", "company_size",
  "revenue", "funding_stage", "funding_amount", "technologies", "seniority", "department", "job_function",
];

// thesis_private_check_sizes / thesis_institutional_check_sizes deliberately excluded --
// deprecated as of the 2026-08-06 Check Size consolidation. check_size_personal/
// check_size_institutional (custom fields, already selectable via the custom-field
// section of this same mapping UI) are now the sole canonical Check Size destinations.
export const THESIS_FIELD_OPTIONS = [
  "thesis_cities", "thesis_investor_mode", "thesis_dietary_preferences", "thesis_referral_emails",
  "thesis_private_asset_types", "thesis_private_business_models", "thesis_private_industries",
  "thesis_private_deal_stages", "thesis_private_meeting_preferences",
  "thesis_private_demographic_preferences", "thesis_private_other_criteria",
  "thesis_also_invests_institutionally",
  "thesis_institutional_asset_types", "thesis_institutional_business_models", "thesis_institutional_industries",
  "thesis_institutional_deal_stages", "thesis_institutional_meeting_preferences",
  "thesis_institutional_demographic_preferences", "thesis_institutional_other_criteria",
];

export function CsvColumnMappingStep({
  batch,
  mapping,
  onMappingChange,
  customFields,
  busy,
  onSubmit,
  submitLabel = "Preview import",
  busyLabel = "Checking for duplicates...",
}: {
  batch: CrmImportBatch;
  mapping: Record<string, string>;
  onMappingChange: (mapping: Record<string, string>) => void;
  customFields: CrmCustomFieldDefinition[];
  busy: boolean;
  onSubmit: () => void;
  submitLabel?: string;
  busyLabel?: string;
}) {
  return (
    <div className="space-y-3">
      {batch.headers.map((header) => (
        <div key={header} className="flex items-center gap-3">
          <span className="w-48 shrink-0 truncate text-sm">{header}</span>
          <select
            value={mapping[header] ?? ""}
            onChange={(e) => onMappingChange({ ...mapping, [header]: e.target.value })}
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
      <Button onClick={onSubmit} disabled={busy} className="mt-2">
        {busy ? busyLabel : submitLabel}
      </Button>
    </div>
  );
}
