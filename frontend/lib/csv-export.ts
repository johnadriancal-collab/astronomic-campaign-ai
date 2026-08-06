// Pure CSV-building logic for the CRM "export selected contacts" feature,
// kept separate from the page component so escaping/formatting rules are
// unit-testable without rendering React or touching the DOM. The one
// DOM-touching piece (downloadCsv) lives at the bottom, deliberately
// excluded from the pure functions above it.
//
// Column list = core/thesis fields (from GET /crm/contacts/export-fields,
// computed server-side straight off the CrmContact model) + active custom
// fields (from GET /crm/custom-fields) -- both dynamic, so a field added to
// either later shows up with no changes here. Multi-select values are
// joined with "; ", matching the exact delimiter the CSV *import* pipeline
// already splits on (see LIST_FIELD_NAMES in crm_import_service.py), so an
// export round-trips cleanly back through import.

export type CrmContactExportFieldKind = "scalar" | "list" | "boolean";

export interface ExportColumn {
  key: string;
  label: string;
  kind: CrmContactExportFieldKind;
  source: "core" | "custom";
}

interface ContactLike {
  custom_fields?: Record<string, unknown> | null;
}

const ACRONYMS = new Set(["id", "url", "crm"]);

export function titleCase(key: string): string {
  return key
    .split("_")
    .filter((word) => word.length > 0)
    .map((word) => (ACRONYMS.has(word.toLowerCase()) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
}

export function formatCellValue(value: unknown, kind: CrmContactExportFieldKind): string {
  if (value === null || value === undefined) return "";
  if (kind === "boolean") return value ? "true" : "false";
  if (kind === "list") {
    if (!Array.isArray(value)) return String(value);
    return value
      .filter((v) => v !== null && v !== undefined && String(v).trim() !== "")
      .map((v) => String(v))
      .join("; ");
  }
  return String(value);
}

// RFC 4180: quote a field if it contains a comma, quote, or line break; double any
// internal quotes. Plain values pass through untouched.
export function csvEscape(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function buildExportColumns(
  coreFields: { key: string; kind: CrmContactExportFieldKind }[],
  customFields: { field_key: string; label: string; field_type: string }[]
): ExportColumn[] {
  const core: ExportColumn[] = coreFields.map((f) => ({
    key: f.key,
    label: titleCase(f.key),
    kind: f.kind,
    source: "core",
  }));
  const custom: ExportColumn[] = customFields.map((f) => ({
    key: f.field_key,
    label: f.label,
    kind: f.field_type === "multi_select" ? "list" : f.field_type === "boolean" ? "boolean" : "scalar",
    source: "custom",
  }));
  return [...core, ...custom];
}

export function getContactCellValue<T extends ContactLike>(contact: T, column: ExportColumn): unknown {
  if (column.source === "custom") return contact.custom_fields ? contact.custom_fields[column.key] : undefined;
  return (contact as Record<string, unknown>)[column.key];
}

// Every column always gets a cell -- including columns where every contact in `contacts`
// happens to be empty for that field -- since the header row alone is what guarantees
// the full CRM schema is represented, independent of which contacts were selected.
// Generic (rather than a fixed ContactLike[] param) so a real, fully-typed CrmContact[] --
// which has no index signature of its own -- can be passed straight through.
export function buildCsv<T extends ContactLike>(columns: ExportColumn[], contacts: T[]): string {
  const header = columns.map((c) => csvEscape(c.label));
  const rows = contacts.map((contact) =>
    columns.map((c) => csvEscape(formatCellValue(getContactCellValue(contact, c), c.kind)))
  );
  return [header, ...rows].map((row) => row.join(",")).join("\r\n");
}

export function exportFilename(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `crm_contacts_${y}-${m}-${d}.csv`;
}

export function downloadCsv(csv: string, filename: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
