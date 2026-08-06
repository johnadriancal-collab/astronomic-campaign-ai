import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildCsv,
  buildExportColumns,
  csvEscape,
  exportFilename,
  formatCellValue,
  getContactCellValue,
  titleCase,
} from "./csv-export.ts";

// A loose stand-in for CrmContact in these tests -- real contact objects have far more
// fields than any single test needs, and an index signature here (unlike on the real
// CrmContact type) lets each test literal declare just the handful of fields it cares about.
type TestContact = { [key: string]: unknown };

// --- titleCase ---

test("titleCase converts snake_case to Title Case", () => {
  assert.equal(titleCase("first_name"), "First Name");
  assert.equal(titleCase("thesis_private_check_sizes"), "Thesis Private Check Sizes");
});

test("titleCase uppercases known acronyms", () => {
  assert.equal(titleCase("crm_contact_id"), "CRM Contact ID");
  assert.equal(titleCase("linkedin_url"), "Linkedin URL");
});

// --- csvEscape ---

test("csvEscape leaves a plain value untouched", () => {
  assert.equal(csvEscape("Austin"), "Austin");
});

test("csvEscape quotes a value containing a comma", () => {
  assert.equal(csvEscape("Collectibles (e.g., art, wine, watches)"), '"Collectibles (e.g., art, wine, watches)"');
});

test("csvEscape quotes and doubles internal quotes", () => {
  assert.equal(csvEscape('She said "hi"'), '"She said ""hi"""');
});

test("csvEscape quotes a value containing a newline", () => {
  assert.equal(csvEscape("line one\nline two"), '"line one\nline two"');
});

test("csvEscape quotes a value containing a carriage return", () => {
  assert.equal(csvEscape("line one\r\nline two"), '"line one\r\nline two"');
});

test("csvEscape does not alter an empty string", () => {
  assert.equal(csvEscape(""), "");
});

// --- formatCellValue ---

test("formatCellValue renders null/undefined scalars as an empty string", () => {
  assert.equal(formatCellValue(null, "scalar"), "");
  assert.equal(formatCellValue(undefined, "scalar"), "");
});

test("formatCellValue stringifies a plain scalar", () => {
  assert.equal(formatCellValue("Austin", "scalar"), "Austin");
  assert.equal(formatCellValue(42, "scalar"), "42");
});

test("formatCellValue renders booleans as literal true/false, not empty", () => {
  assert.equal(formatCellValue(true, "boolean"), "true");
  assert.equal(formatCellValue(false, "boolean"), "false");
});

test("formatCellValue renders a null boolean as empty, distinct from false", () => {
  assert.equal(formatCellValue(null, "boolean"), "");
});

test("formatCellValue joins a multi-select list with semicolons", () => {
  assert.equal(formatCellValue(["Angel Investor", "Family Office"], "list"), "Angel Investor; Family Office");
});

test("formatCellValue preserves every selected value, not just the first", () => {
  const values = ["A", "B", "C", "D", "E"];
  assert.equal(formatCellValue(values, "list"), "A; B; C; D; E");
});

test("formatCellValue renders an empty list as an empty string, not omitted", () => {
  assert.equal(formatCellValue([], "list"), "");
});

test("formatCellValue renders a null list as an empty string", () => {
  assert.equal(formatCellValue(null, "list"), "");
});

test("formatCellValue filters out blank/null entries within a list", () => {
  assert.equal(formatCellValue(["A", "", null, "B"], "list"), "A; B");
});

// --- buildExportColumns ---

test("buildExportColumns merges core and custom fields, core first", () => {
  const columns = buildExportColumns(
    [{ key: "first_name", kind: "scalar" }, { key: "technologies", kind: "list" }],
    [{ field_key: "investor_type", label: "Investor Type", field_type: "multi_select" }]
  );
  assert.deepEqual(columns.map((c) => c.key), ["first_name", "technologies", "investor_type"]);
  assert.deepEqual(columns.map((c) => c.source), ["core", "core", "custom"]);
});

test("buildExportColumns maps multi_select custom fields to kind=list", () => {
  const columns = buildExportColumns([], [{ field_key: "investor_type", label: "Investor Type", field_type: "multi_select" }]);
  assert.equal(columns[0].kind, "list");
});

test("buildExportColumns maps boolean custom fields to kind=boolean", () => {
  const columns = buildExportColumns([], [{ field_key: "accredited", label: "Accredited", field_type: "boolean" }]);
  assert.equal(columns[0].kind, "boolean");
});

test("buildExportColumns maps every other custom field type to kind=scalar", () => {
  for (const field_type of ["text", "long_text", "number", "date", "single_select"]) {
    const columns = buildExportColumns([], [{ field_key: "k", label: "K", field_type }]);
    assert.equal(columns[0].kind, "scalar", `expected scalar for ${field_type}`);
  }
});

test("buildExportColumns uses the definition's own label for custom fields, not titleCase", () => {
  const columns = buildExportColumns([], [{ field_key: "investor_type", label: "Investor Type (Legacy)", field_type: "text" }]);
  assert.equal(columns[0].label, "Investor Type (Legacy)");
});

// --- getContactCellValue ---

test("getContactCellValue reads a core field directly off the contact", () => {
  const contact = { first_name: "Ada", custom_fields: {} };
  const column = { key: "first_name", label: "First Name", kind: "scalar" as const, source: "core" as const };
  assert.equal(getContactCellValue(contact, column), "Ada");
});

test("getContactCellValue reads a custom field out of custom_fields", () => {
  const contact = { first_name: "Ada", custom_fields: { investor_type: ["Angel Investor"] } };
  const column = { key: "investor_type", label: "Investor Type", kind: "list" as const, source: "custom" as const };
  assert.deepEqual(getContactCellValue(contact, column), ["Angel Investor"]);
});

test("getContactCellValue returns undefined for a custom field the contact never set", () => {
  const contact = { first_name: "Ada", custom_fields: {} };
  const column = { key: "never_set", label: "Never Set", kind: "scalar" as const, source: "custom" as const };
  assert.equal(getContactCellValue(contact, column), undefined);
});

test("getContactCellValue handles a missing custom_fields object entirely", () => {
  const contact: TestContact = { first_name: "Ada" };
  const column = { key: "investor_type", label: "Investor Type", kind: "list" as const, source: "custom" as const };
  assert.equal(getContactCellValue(contact, column), undefined);
});

// --- buildCsv (end-to-end) ---

const CORE_FIELDS = [
  { key: "first_name", kind: "scalar" as const },
  { key: "archived", kind: "boolean" as const },
  { key: "technologies", kind: "list" as const },
];
const CUSTOM_FIELDS = [{ field_key: "investor_type", label: "Investor Type", field_type: "multi_select" }];

test("buildCsv includes every column as a header, even when no selected contact has a value for it", () => {
  const columns = buildExportColumns(CORE_FIELDS, CUSTOM_FIELDS);
  const csv = buildCsv(columns, [{ first_name: "Ada", archived: false, custom_fields: {} }]);
  const [header] = csv.split("\r\n");
  assert.equal(header, "First Name,Archived,Technologies,Investor Type");
});

test("buildCsv renders a full row with scalar, boolean, list, and custom values", () => {
  const columns = buildExportColumns(CORE_FIELDS, CUSTOM_FIELDS);
  const csv = buildCsv(columns, [
    { first_name: "Ada", archived: true, technologies: ["Python", "Rust"], custom_fields: { investor_type: ["Angel Investor", "Family Office"] } },
  ]);
  const [, row] = csv.split("\r\n");
  assert.equal(row, "Ada,true,Python; Rust,Angel Investor; Family Office");
});

test("buildCsv renders empty/null fields as empty cells without dropping the column", () => {
  const columns = buildExportColumns(CORE_FIELDS, CUSTOM_FIELDS);
  const csv = buildCsv(columns, [{ first_name: null, archived: false, technologies: [], custom_fields: {} }]);
  const [, row] = csv.split("\r\n");
  assert.equal(row, ",false,,");
});

test("buildCsv only ever includes the contacts explicitly passed in", () => {
  const columns = buildExportColumns(CORE_FIELDS, []);
  const csv = buildCsv(columns, [{ first_name: "Selected", archived: false, technologies: [] } as TestContact]);
  assert.ok(csv.includes("Selected"));
  assert.equal(csv.split("\r\n").length, 2); // header + exactly one contact row
});

test("buildCsv correctly escapes a value containing a comma inside a real row", () => {
  const columns = buildExportColumns([{ key: "company", kind: "scalar" }], []);
  const csv = buildCsv(columns, [{ company: "Acme, Inc." } as TestContact]);
  const [, row] = csv.split("\r\n");
  assert.equal(row, '"Acme, Inc."');
});

// --- exportFilename ---

test("exportFilename formats as crm_contacts_YYYY-MM-DD.csv", () => {
  assert.equal(exportFilename(new Date(2026, 7, 6)), "crm_contacts_2026-08-06.csv"); // month is 0-indexed
});

test("exportFilename zero-pads single-digit months and days", () => {
  assert.equal(exportFilename(new Date(2026, 0, 3)), "crm_contacts_2026-01-03.csv");
});
