"""
Pure CSV-building logic for BACKEND-generated CRM exports (Astro AI's
export_crm_contacts tool -- app/services/astro_crm_tools.py). This is the
first backend-generated downloadable file in this codebase; the existing
CRM UI's own export (frontend/lib/csv-export.ts) builds its CSV entirely
client-side and is deliberately left untouched by this module -- unifying
the two is a separate, explicitly deferred task.

Column source and formatting rules mirror frontend/lib/csv-export.ts
EXACTLY for parity with the CRM UI's own export: core/thesis fields via
get_contact_export_fields() (app/models/crm.py -- the same introspection
GET /crm/contacts/export-fields uses) plus every ACTIVE custom field,
title-cased headers (with the same id/url/crm acronym handling), list
values joined with "; ", booleans as "true"/"false", and RFC 4180
quote/comma/newline escaping with CRLF row separators.

Adds ONE thing the existing client-side export does not have: CSV
formula-injection protection. A cell value starting with =, +, -, or @ can
be interpreted as a formula by Excel/Google Sheets when the file is
opened -- _sanitize_formula_injection() prefixes such a value with a
leading `'` so it is always read back as literal text.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.models.crm import CrmContact, CrmCustomFieldDefinition, get_contact_export_fields

_ACRONYMS = {"id", "url", "crm"}

_SLUG_DISALLOWED = re.compile(r"[^a-z0-9]+")


def build_export_filename(label: str | None) -> str:
    """Always backend-controlled: a caller-supplied `label` is a
    descriptive hint only, never a path -- this strips it down to a
    lowercase-hyphen slug and forces a `.csv` extension, falling back to a
    generic name when the label is missing or has no usable characters at
    all (e.g. "all-crm-contacts.csv" for an unfiltered export)."""
    slug = _SLUG_DISALLOWED.sub("-", (label or "").strip().lower()).strip("-")
    return f"{slug or 'crm-contacts'}.csv"


def title_case(key: str) -> str:
    words = [w for w in key.split("_") if w]
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w[:1].upper() + w[1:] for w in words)


@dataclass(frozen=True)
class ExportColumn:
    key: str
    label: str
    kind: str  # "scalar" | "list" | "boolean"
    source: str  # "core" | "custom"


def build_export_columns(custom_fields: list[CrmCustomFieldDefinition]) -> list[ExportColumn]:
    """Core/thesis fields (get_contact_export_fields(), the exact same
    column list GET /crm/contacts/export-fields returns) followed by every
    ACTIVE custom field -- identical column *source* to the CRM UI's own
    export, never a hand-maintained/reduced schema."""
    core = [
        ExportColumn(key=f.key, label=title_case(f.key), kind=f.kind, source="core")
        for f in get_contact_export_fields()
    ]
    custom = [
        ExportColumn(
            key=f.field_key,
            label=f.label,
            kind="list" if f.field_type.value == "multi_select" else ("boolean" if f.field_type.value == "boolean" else "scalar"),
            source="custom",
        )
        for f in custom_fields
        if f.active
    ]
    return core + custom


def _cell_value(contact: CrmContact, column: ExportColumn) -> Any:
    if column.source == "custom":
        return contact.custom_fields.get(column.key)
    return getattr(contact, column.key, None)


def _format_cell_value(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "list":
        if not isinstance(value, list):
            return str(value)
        return "; ".join(str(v) for v in value if v is not None and str(v).strip() != "")
    return str(value)


_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def _sanitize_formula_injection(value: str) -> str:
    """Not part of RFC 4180 -- a spreadsheet-application convention.
    Applied only to this backend export path (see module docstring); the
    existing client-side CRM export is deliberately left unchanged."""
    if value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value


def _csv_escape(value: str) -> str:
    value = _sanitize_formula_injection(value)
    if any(c in value for c in ('"', ",", "\r", "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value


def build_csv(columns: list[ExportColumn], contacts: list[CrmContact]) -> str:
    header = [_csv_escape(c.label) for c in columns]
    rows = [
        [_csv_escape(_format_cell_value(_cell_value(c, col), col.kind)) for col in columns]
        for c in contacts
    ]
    return "\r\n".join(",".join(row) for row in [header, *rows])
