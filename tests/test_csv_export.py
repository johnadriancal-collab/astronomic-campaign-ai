"""
csv_export.py -- pure CSV-building logic for Astro AI's backend-generated
export_crm_contacts tool. Exercised directly against real CrmContact/
CrmCustomFieldDefinition models, no FastAPI/Claude involved.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.services.csv_export import (
    ExportColumn,
    build_csv,
    build_export_columns,
    build_export_filename,
    title_case,
)


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def make_custom_field(**overrides) -> CrmCustomFieldDefinition:
    defaults = dict(
        crm_custom_field_id=str(uuid.uuid4()),
        field_key="investor_type",
        label="Investor Type",
        field_type=CustomFieldType.MULTI_SELECT,
        options=["Angel Investor"],
        active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return CrmCustomFieldDefinition(**defaults)


# --- title_case ---------------------------------------------------------


def test_title_case_capitalizes_words():
    assert title_case("first_name") == "First Name"


@pytest.mark.parametrize("key,expected", [("crm_contact_id", "CRM Contact ID"), ("linkedin_url", "Linkedin URL")])
def test_title_case_uppercases_known_acronyms(key, expected):
    assert title_case(key) == expected


# --- build_export_columns ------------------------------------------------


def test_columns_include_core_fields_and_exclude_internal_ones():
    columns = build_export_columns([])
    keys = {c.key for c in columns}
    assert "first_name" in keys
    assert "source_snapshot" not in keys  # excluded exactly like get_contact_export_fields()
    assert "custom_fields" not in keys


def test_columns_include_only_active_custom_fields():
    active = make_custom_field(field_key="investor_type", label="Investor Type", active=True)
    inactive = make_custom_field(
        crm_custom_field_id=str(uuid.uuid4()), field_key="retired_field", label="Retired", active=False
    )
    columns = build_export_columns([active, inactive])
    keys = {c.key for c in columns}
    assert "investor_type" in keys
    assert "retired_field" not in keys


def test_custom_field_kind_derived_from_field_type():
    multi = make_custom_field(field_key="tags", field_type=CustomFieldType.MULTI_SELECT)
    boolean = make_custom_field(crm_custom_field_id=str(uuid.uuid4()), field_key="flag", field_type=CustomFieldType.BOOLEAN)
    text = make_custom_field(crm_custom_field_id=str(uuid.uuid4()), field_key="note", field_type=CustomFieldType.TEXT)
    columns = {c.key: c for c in build_export_columns([multi, boolean, text])}
    assert columns["tags"].kind == "list"
    assert columns["flag"].kind == "boolean"
    assert columns["note"].kind == "scalar"


# --- build_csv: formatting -----------------------------------------------


def test_build_csv_header_and_row_shape():
    columns = [ExportColumn(key="first_name", label="First Name", kind="scalar", source="core")]
    contact = make_contact(first_name="Alice")
    csv_text = build_csv(columns, [contact])
    assert csv_text == "First Name\r\nAlice"


def test_build_csv_joins_list_values_with_semicolon():
    columns = [ExportColumn(key="tags", label="Tags", kind="list", source="custom")]
    contact = make_contact(custom_fields={"tags": ["A", "B", ""]})
    csv_text = build_csv(columns, [contact])
    assert csv_text.split("\r\n")[1] == "A; B"


def test_build_csv_booleans_as_lowercase_strings():
    columns = [ExportColumn(key="flag", label="Flag", kind="boolean", source="custom")]
    contact = make_contact(custom_fields={"flag": True})
    csv_text = build_csv(columns, [contact])
    assert csv_text.split("\r\n")[1] == "true"


def test_build_csv_none_becomes_empty_string():
    columns = [ExportColumn(key="company", label="Company", kind="scalar", source="core")]
    contact = make_contact(company=None)
    csv_text = build_csv(columns, [contact])
    assert csv_text.split("\r\n")[1] == ""


def test_build_csv_multiple_contacts_one_row_each():
    columns = [ExportColumn(key="first_name", label="First Name", kind="scalar", source="core")]
    contacts = [make_contact(first_name="Alice"), make_contact(first_name="Bob")]
    csv_text = build_csv(columns, contacts)
    assert csv_text.split("\r\n") == ["First Name", "Alice", "Bob"]


# --- build_csv: RFC 4180 escaping ----------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Smith, Jr.", '"Smith, Jr."'),
        ('Say "hi"', '"Say ""hi"""'),
        ("Line1\nLine2", '"Line1\nLine2"'),
        ("plain value", "plain value"),
    ],
)
def test_build_csv_escapes_special_characters(raw, expected):
    columns = [ExportColumn(key="company", label="Company", kind="scalar", source="core")]
    contact = make_contact(company=raw)
    csv_text = build_csv(columns, [contact])
    assert csv_text.split("\r\n", 1)[1] == expected


# --- build_csv: formula-injection protection (new -- not in the existing
# client-side CRM export, only this backend path) --------------------------


@pytest.mark.parametrize("dangerous", ["=SUM(A1:A9)", "+1+1", "-1+1", "@SUM(1;2)"])
def test_build_csv_sanitizes_formula_injection_prefixes(dangerous):
    columns = [ExportColumn(key="company", label="Company", kind="scalar", source="core")]
    contact = make_contact(company=dangerous)
    csv_text = build_csv(columns, [contact])
    row = csv_text.split("\r\n", 1)[1]
    assert row == f"'{dangerous}"


def test_build_csv_does_not_sanitize_ordinary_values_starting_with_safe_characters():
    columns = [ExportColumn(key="company", label="Company", kind="scalar", source="core")]
    contact = make_contact(company="Acme Inc.")
    csv_text = build_csv(columns, [contact])
    assert csv_text.split("\r\n", 1)[1] == "Acme Inc."


# --- build_export_filename ------------------------------------------------


def test_filename_slugifies_a_label():
    assert build_export_filename("Austin Angel Investors") == "austin-angel-investors.csv"


def test_filename_falls_back_to_generic_name_when_no_label():
    assert build_export_filename(None) == "crm-contacts.csv"
    assert build_export_filename("") == "crm-contacts.csv"


def test_filename_strips_path_separators_and_unsafe_characters():
    assert build_export_filename("../../etc/passwd") == "etc-passwd.csv"
    assert build_export_filename("weird!!chars??") == "weird-chars.csv"
