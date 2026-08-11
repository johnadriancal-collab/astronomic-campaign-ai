"""
Tests for CrmImportService: CSV parsing/upload, deterministic mapping
suggestions, the preview step's dedup classification (including within-
file duplicate detection), and commit's create/update/skip decisions +
report.
"""

import pytest

from app.models.crm import CrmImportBatchStatus, CrmImportRowStatus, CustomFieldType
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.services.crm_classification_rules import build_classification_context
from app.services.crm_import_service import CrmImportService, suggest_mapping
from app.services.crm_service import CrmService


@pytest.fixture
def import_service():
    crm = CrmService()
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


async def _seed_validated_single_selects(import_service):
    """Creates the three validated single-select custom fields with their real
    production options -- mirrors production state, where these fields already
    exist before any CSV is ever uploaded. Called directly (not a fixture) since
    this suite has no pytest-asyncio config for async fixtures."""
    crm = import_service.crm_service
    await crm.create_custom_field(
        "chris_degree_connection", "Chris Degree Connection", CustomFieldType.SINGLE_SELECT,
        options=["1st degree", "2nd degree", "3rd degree", "N/A"],
    )
    await crm.create_custom_field(
        "age_range", "Age Range", CustomFieldType.SINGLE_SELECT,
        options=["18-22", "23-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81+", "Retired", "Deceased"],
    )
    await crm.create_custom_field(
        "gender", "Gender", CustomFieldType.SINGLE_SELECT, options=["Male", "Female"],
    )
    await crm.create_custom_field(
        "engagement_stage", "Engagement Stage", CustomFieldType.SINGLE_SELECT,
        options=["Cold", "Interested", "Unresponsive", "Replied"],
    )
    check_size_options = [
        "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
        "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M", "$10M+", "Other:",
    ]
    await crm.create_custom_field(
        "check_size_personal", "Check Size (Personal)", CustomFieldType.MULTI_SELECT, options=check_size_options,
    )
    await crm.create_custom_field(
        "check_size_institutional", "Check Size (Institutional)", CustomFieldType.MULTI_SELECT,
        options=check_size_options,
    )
    await crm.create_custom_field(
        "accredited_status", "Accredited Status", CustomFieldType.SINGLE_SELECT, options=["Yes", "No"],
    )
    await crm.create_custom_field(
        "how_early_do_you_invest", "How Early Do You Invest?", CustomFieldType.MULTI_SELECT,
        options=["Great team, no revenue", "Great team, some revenue", "$10k-$50k MRR / GMV",
                 "$50k-$100k MRR / GMV", "$100k-$1M MRR / GMV", "$1M+ MRR / GMV"],
    )
    await crm.create_custom_field(
        "revenue_stage", "Revenue Stage", CustomFieldType.SINGLE_SELECT,
        options=["$250K - $500K", "$500k - $1M", "$1M - $10M", "$10M - $100M"],
    )
    # 2026-08-06 broader-audit Phase 1 -- the ten plain scalar fields, mirroring
    # production state where these definitions already exist before any CSV upload.
    await crm.create_custom_field("work_direct_phone", "Work Direct Phone", CustomFieldType.TEXT)
    await crm.create_custom_field("do_not_call", "Do Not Call", CustomFieldType.BOOLEAN)
    await crm.create_custom_field("last_raised_at", "Last Raised At", CustomFieldType.DATE)
    await crm.create_custom_field("how_often_do_you_invest", "How Often Do You Invest?", CustomFieldType.TEXT)
    await crm.create_custom_field("personal_notes", "Personal Notes", CustomFieldType.LONG_TEXT)
    await crm.create_custom_field("notes", "Notes", CustomFieldType.LONG_TEXT)
    await crm.create_custom_field(
        "referred_to_constellation_dinners_by", "Who were you referred to Constellation Dinners by?",
        CustomFieldType.TEXT,
    )
    await crm.create_custom_field(
        "investment_geography_preference", "Investment Geography Preference", CustomFieldType.TEXT,
    )
    await crm.create_custom_field("chris_knows_personally", "Chris Knows Personally", CustomFieldType.BOOLEAN)


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


# --- Upload / parsing ---


@pytest.mark.asyncio
async def test_upload_parses_rows_and_reports_row_count(import_service):
    content = csv_bytes("First Name,Last Name,Email\nAda,Lovelace,ada@example.com\nGrace,Hopper,grace@example.com\n")
    batch = await import_service.upload("prospects.csv", content)

    assert batch.row_count == 2
    assert batch.headers == ["First Name", "Last Name", "Email"]
    assert batch.rows[0]["Email"] == "ada@example.com"
    assert batch.status == CrmImportBatchStatus.UPLOADED


@pytest.mark.asyncio
async def test_upload_strips_bom(import_service):
    content = "﻿First Name,Email\nAda,ada@example.com\n".encode("utf-8")
    batch = await import_service.upload("prospects.csv", content)
    assert batch.headers[0] == "First Name"  # not "﻿First Name"


# --- Deterministic mapping suggestions ---


def test_suggest_mapping_matches_common_header_variants():
    mapping = suggest_mapping(["First Name", "Last Name", "E-Mail", "LinkedIn Profile URL", "Company Name"])
    assert mapping["First Name"] == "first_name"
    assert mapping["Last Name"] == "last_name"
    assert mapping["E-Mail"] == "email"
    assert mapping["LinkedIn Profile URL"] == "linkedin_url"
    assert mapping["Company Name"] == "company"


def test_suggest_mapping_leaves_unknown_headers_unmapped():
    mapping = suggest_mapping(["Some Totally Unknown Column"])
    assert "Some Totally Unknown Column" not in mapping


def test_suggest_mapping_leaves_company_size_unmapped_but_employee_headers_still_map():
    """2026-08-06 Contacts 3 (Investors) audit -- 'Company Size' (the Investor Thesis
    form's own free-text bucket answer, e.g. '51-200 employees') is a DIFFERENT concept
    from '# Employees' (Apollo's real headcount number) and must never alias to
    company_size, even though every populated production company_size value has always
    come from an employee-count header. All employee-count header variants must keep
    mapping to company_size exactly as before."""
    mapping = suggest_mapping(
        ["Company Size", "# Employees", "Employees", "Employee Count", "Number of Employees"]
    )
    assert "Company Size" not in mapping
    assert mapping["# Employees"] == "company_size"
    assert mapping["Employees"] == "company_size"
    assert mapping["Employee Count"] == "company_size"
    assert mapping["Number of Employees"] == "company_size"


@pytest.mark.asyncio
async def test_company_size_never_wins_over_number_of_employees_end_to_end(import_service):
    """Real Contacts 3 (Investors) shape: a row with BOTH '# Employees' (Apollo's real
    headcount) and 'Company Size' (the thesis form's own bucketed text) populated --
    company_size must come from '# Employees' only; the raw 'Company Size' text is still
    preserved in source_snapshot, just never structured into the core field."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes('Email,# Employees,Company Size\nnova@example.com,84,"51-200 employees"\n'),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.company_size == "84"
    assert contact.source_snapshot["Company Size"] == "51-200 employees"


@pytest.mark.asyncio
async def test_upload_populates_suggested_mapping(import_service):
    batch = await import_service.upload("p.csv", csv_bytes("First Name,Email\nAda,ada@example.com\n"))
    assert batch.suggested_mapping == {"First Name": "first_name", "Email": "email"}


# --- Preview: dedup classification ---


@pytest.mark.asyncio
async def test_preview_classifies_new_row(import_service):
    batch = await import_service.upload("p.csv", csv_bytes("Email\nnew@example.com\n"))
    previewed = await import_service.preview(batch.import_batch_id, {"Email": "email"})
    assert previewed.preview[0].status == CrmImportRowStatus.NEW
    assert previewed.new_count == 1


@pytest.mark.asyncio
async def test_preview_classifies_existing_row_via_email(import_service):
    await import_service.crm_service.create_contact({"email": "known@example.com"})
    batch = await import_service.upload("p.csv", csv_bytes("Email\nknown@example.com\n"))
    previewed = await import_service.preview(batch.import_batch_id, {"Email": "email"})
    assert previewed.preview[0].status == CrmImportRowStatus.EXISTING
    assert previewed.preview[0].matched_on == "email"
    assert previewed.existing_count == 1


@pytest.mark.asyncio
async def test_preview_classifies_possible_duplicate_via_name_and_company(import_service):
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await import_service.upload("p.csv", csv_bytes("First Name,Last Name,Company\nAda,Lovelace,Acme\n"))
    previewed = await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Company": "company"}
    )
    assert previewed.preview[0].status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert previewed.possible_duplicate_count == 1


@pytest.mark.asyncio
async def test_preview_deduplicates_within_the_file_itself(import_service):
    """Two rows in the SAME file sharing an email must not both become new contacts."""
    batch = await import_service.upload(
        "p.csv", csv_bytes("Email\nsame@example.com\nsame@example.com\n")
    )
    previewed = await import_service.preview(batch.import_batch_id, {"Email": "email"})

    assert previewed.preview[0].status == CrmImportRowStatus.NEW
    assert previewed.preview[1].status == CrmImportRowStatus.EXISTING
    assert previewed.preview[1].matched_on == "within_file_row_0"
    assert previewed.new_count == 1
    assert previewed.existing_count == 1


@pytest.mark.asyncio
async def test_preview_within_file_fallback_tier_is_possible_duplicate(import_service):
    batch = await import_service.upload(
        "p.csv", csv_bytes("First Name,Last Name,Company\nAda,Lovelace,Acme\nAda,Lovelace,Acme\n")
    )
    previewed = await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Company": "company"}
    )
    assert previewed.preview[0].status == CrmImportRowStatus.NEW
    assert previewed.preview[1].status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert previewed.preview[1].matched_on == "within_file_row_0"


@pytest.mark.asyncio
async def test_preview_within_file_same_email_different_name_is_flagged_not_merged(import_service):
    """Real 2026-08-06 two-CSV audit case: two rows share one email but are two
    different real people (a source-data error -- one row's Email column holds
    someone else's address). Must NOT be auto-merged into a single contact --
    downgraded to POSSIBLE_DUPLICATE (always human-reviewed, defaults to skip)."""
    batch = await import_service.upload(
        "p.csv",
        csv_bytes(
            "First Name,Last Name,Email\n"
            "James,Feldkamp,jfeldkamp@alarycapital.com\n"
            "Shawn,Riely,jfeldkamp@alarycapital.com\n"
        ),
    )
    previewed = await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Email": "email"}
    )
    assert previewed.preview[0].status == CrmImportRowStatus.NEW
    assert previewed.preview[1].status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert previewed.preview[1].matched_on == "within_file_row_0_conflicting_identity"
    assert previewed.new_count == 1
    assert previewed.possible_duplicate_count == 1


@pytest.mark.asyncio
async def test_commit_same_email_different_name_conflict_defaults_to_skip_preserving_first(import_service):
    batch = await import_service.upload(
        "p.csv",
        csv_bytes(
            "First Name,Last Name,Email\n"
            "James,Feldkamp,jfeldkamp@alarycapital.com\n"
            "Shawn,Riely,jfeldkamp@alarycapital.com\n"
        ),
    )
    await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Email": "email"}
    )
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    assert report.skipped == 1
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].first_name == "James"  # the self-consistent identity survives; Shawn's row is skipped, not merged in
    assert contacts[0].last_name == "Feldkamp"


@pytest.mark.asyncio
async def test_repeat_upload_of_same_conflicting_email_never_merges_into_existing_contact(import_service):
    """The gap a first pass at this fix missed: within-file dedup alone only
    catches the conflict the FIRST time two rows collide in one batch. Once
    James Feldkamp exists in the CRM, a LATER upload (the same CSV again, or
    a fresh one) containing Shawn Riely's row under James's email must still
    be caught -- via classify_match's identity check, not just the within-file
    one -- and never merge Shawn's data into James's contact."""
    batch1 = await import_service.upload(
        "batch1.csv",
        csv_bytes("First Name,Last Name,Email,Company\nJames,Feldkamp,jfeldkamp@alarycapital.com,Alary Capital\n"),
    )
    await import_service.preview(
        batch1.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Email": "email", "Company": "company"}
    )
    await import_service.commit(batch1.import_batch_id)

    batch2 = await import_service.upload(
        "batch2.csv",
        csv_bytes("First Name,Last Name,Email,Company\nShawn,Riely,jfeldkamp@alarycapital.com,Westmount Realty Capital\n"),
    )
    previewed2 = await import_service.preview(
        batch2.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Email": "email", "Company": "company"}
    )
    assert previewed2.preview[0].status == CrmImportRowStatus.POSSIBLE_DUPLICATE
    assert previewed2.preview[0].matched_on == "email_conflicting_identity"
    report2 = await import_service.commit(batch2.import_batch_id)

    assert report2.skipped == 1
    assert report2.created == 0
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].first_name == "James"
    assert contacts[0].company == "Alary Capital"  # never touched by Shawn's row


@pytest.mark.asyncio
async def test_within_file_dedup_still_merges_when_names_genuinely_match(import_service):
    """Sanity check: the new identity check must not break the ordinary case --
    same person, same name, split across two rows in one file, still merges."""
    batch = await import_service.upload(
        "p.csv",
        csv_bytes(
            "First Name,Last Name,Email,Company\n"
            "Ada,Lovelace,ada@example.com,\n"
            "Ada,Lovelace,ada@example.com,Analytical Engines Ltd\n"
        ),
    )
    previewed = await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Email": "email", "Company": "company"}
    )
    assert previewed.preview[1].status == CrmImportRowStatus.EXISTING
    assert previewed.preview[1].matched_on == "within_file_row_0"
    report = await import_service.commit(batch.import_batch_id)
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].company == "Analytical Engines Ltd"


@pytest.mark.asyncio
async def test_preview_splits_multi_value_thesis_fields_on_semicolon_not_comma(import_service):
    """Canonical option text contains literal commas -- splitting on comma would shred it."""
    batch = await import_service.upload(
        "p.csv",
        csv_bytes(
            'Industries\n"SaaS / Software Infrastructure;Collectibles (e.g., art, wine, watches)"\n'
        ),
    )
    previewed = await import_service.preview(batch.import_batch_id, {"Industries": "thesis_private_industries"})
    values = previewed.preview[0].mapped_fields["thesis_private_industries"]
    assert values == ["SaaS / Software Infrastructure", "Collectibles (e.g., art, wine, watches)"]


# --- Commit ---


@pytest.mark.asyncio
async def test_commit_creates_new_rows_by_default(import_service):
    batch = await import_service.upload("p.csv", csv_bytes("Email,First Name\nnew@example.com,Ada\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email", "First Name": "first_name"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    assert report.updated == 0
    contacts = (await import_service.crm_service.list_contacts()).items
    assert contacts[0].email == "new@example.com"


@pytest.mark.asyncio
async def test_commit_updates_existing_rows_by_default(import_service):
    existing = await import_service.crm_service.create_contact({"email": "known@example.com", "company": ""})
    batch = await import_service.upload("p.csv", csv_bytes("Email,Company\nknown@example.com,Acme\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email", "Company": "company"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.company == "Acme"


@pytest.mark.asyncio
async def test_commit_defaults_possible_duplicate_to_skip(import_service):
    """Never silently create duplicates -- an unreviewed possible_duplicate must be skipped."""
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await import_service.upload("p.csv", csv_bytes("First Name,Last Name,Company\nAda,Lovelace,Acme\n"))
    await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Company": "company"}
    )
    report = await import_service.commit(batch.import_batch_id)

    assert report.skipped == 1
    assert report.created == 0
    assert (await import_service.crm_service.list_contacts()).total == 1  # no duplicate created


@pytest.mark.asyncio
async def test_commit_respects_explicit_decision_to_create_a_possible_duplicate_anyway(import_service):
    await import_service.crm_service.create_contact({"first_name": "Ada", "last_name": "Lovelace", "company": "Acme"})
    batch = await import_service.upload("p.csv", csv_bytes("First Name,Last Name,Company\nAda,Lovelace,Acme\n"))
    await import_service.preview(
        batch.import_batch_id, {"First Name": "first_name", "Last Name": "last_name", "Company": "company"}
    )
    report = await import_service.commit(batch.import_batch_id, decisions={0: "create"})

    assert report.created == 1
    assert (await import_service.crm_service.list_contacts()).total == 2


@pytest.mark.asyncio
async def test_commit_within_file_duplicate_updates_the_row_created_earlier_in_the_same_commit(import_service):
    """The second row's update still applies to the first row's new contact --
    but company is already populated by the first row, so the (same person,
    conflicting) second value is preserved-not-overwritten, same as any other
    populated field. See test_commit_within_file_duplicate_fills_an_empty_field
    below for the case where the second row's value actually lands."""
    batch = await import_service.upload(
        "p.csv", csv_bytes("Email,Company\nsame@example.com,Old Co\nsame@example.com,New Co\n")
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email", "Company": "company"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    assert report.updated == 1
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].company == "Old Co"  # already populated by row 1 -- row 2's conflicting value is preserved-not-overwritten


@pytest.mark.asyncio
async def test_commit_within_file_duplicate_fills_an_empty_field(import_service):
    """Same shape as above, but the field is empty on row 1 -- row 2's value
    for the SAME within-file contact correctly fills it in (fill-empty is
    always allowed; only a conflicting non-empty value is protected)."""
    batch = await import_service.upload(
        "p.csv", csv_bytes("Email,Company\nsame@example.com,\nsame@example.com,New Co\n")
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email", "Company": "company"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    assert report.updated == 1
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].company == "New Co"


@pytest.mark.asyncio
async def test_commit_skip_decision_creates_nothing(import_service):
    batch = await import_service.upload("p.csv", csv_bytes("Email\nnew@example.com\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id, decisions={0: "skip"})

    assert report.skipped == 1
    assert (await import_service.crm_service.list_contacts()).items == []


@pytest.mark.asyncio
async def test_commit_marks_batch_committed(import_service):
    batch = await import_service.upload("p.csv", csv_bytes("Email\nnew@example.com\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    final = await import_service.get_batch(batch.import_batch_id)
    assert final.status == CrmImportBatchStatus.COMMITTED


# --- Classification rules (Industry / Investment Industry) ---
#
# These simulate a FUTURE CSV upload -- the classification rule in
# crm_classification_rules.py must fire automatically with no special
# configuration, exactly as it will for every upload from now on.


@pytest.mark.asyncio
async def test_future_upload_derives_industry_and_investment_industry_with_new_unseen_values(import_service):
    """A brand-new industry value never seen before must be accepted as-is,
    with no predefined option list to reject it."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Industry,Main Industry,Sub-industry\n"
            "nova@example.com,Artisanal Cheese Production,Space Tourism,\"Zero-G Hospitality, Lunar Retail\"\n"
        ),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})  # Industry columns deliberately left unmapped
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.industry == "Artisanal Cheese Production"
    assert contact.custom_fields["investment_industry"] == ["Space Tourism", "Zero-G Hospitality", "Lunar Retail"]


@pytest.mark.asyncio
async def test_classification_rule_wins_even_if_column_mapping_wrongly_maps_main_industry_to_industry(import_service):
    """Regression guard: the original bug was Main Industry's values landing in
    the core `industry` field. Even if a human's column_mapping repeats that
    mistake, the classification rule must overwrite it with the real Industry
    column value."""
    batch = await import_service.upload(
        "p.csv",
        csv_bytes("Email,Industry,Main Industry\nada@example.com,Consumer Electronics,Healthcare\n"),
    )
    await import_service.preview(
        batch.import_batch_id, {"Email": "email", "Main Industry": "industry"}  # the mistake
    )
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.industry == "Consumer Electronics"  # not "Healthcare"


@pytest.mark.asyncio
async def test_future_csv_upload_normalizes_dinner_subscriptions_automatically(import_service):
    """The exact scenario the user described: a future CSV containing legacy
    wording must be normalized on import with zero manual configuration --
    Dinner Subscriptions is deliberately left unmapped to prove the
    classification rule fires independent of column_mapping."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Dinner Subscriptions\n"
            "nova@example.com,\"Investor Dinners, Sigma Librae Dinners, Retreats\"\n"
        ),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})  # column deliberately left unmapped
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Founder Dinners"]


@pytest.mark.asyncio
async def test_dinner_subscriptions_small_group_dinners_end_to_end(import_service):
    """User's explicit decision for the one unmapped value found in the
    2026-08-06 two-CSV audit: map to Investor Dinners, don't add a new option."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Dinner Subscriptions\nnicole@example.com,\"Small group dinners, Fireside Dinners\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners"]


@pytest.mark.asyncio
async def test_future_csv_upload_populates_dinners_attended_automatically(import_service):
    """Dinners Attended is deliberately left unmapped -- proves the classification
    rule fires independent of column_mapping, and dated entries survive verbatim."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Dinners Attended\n"
            "nova@example.com,\"Investor Dinners, Savvy [2.25.2025] Austin, Fireside Dinners\"\n"
        ),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["dinners_attended"] == [
        "Investor Dinners", "Savvy [2.25.2025] Austin", "Fireside Dinners",
    ]


@pytest.mark.asyncio
async def test_future_csv_upload_populates_chris_degree_connection_automatically(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Chris Degree Connection\nnova@example.com,1st degree\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["chris_degree_connection"] == "1st degree"


@pytest.mark.asyncio
async def test_future_csv_upload_populates_age_range_automatically(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Age Range\nnova@example.com,61-70\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["age_range"] == "61-70"


@pytest.mark.asyncio
async def test_future_csv_upload_populates_check_size_personal_automatically(import_service):
    """Check Size is deliberately left unmapped -- proves the classification rule
    fires independent of column_mapping. Comma-space-separated multi-value cell,
    the exact real Alex Pepe shape from the 2026-08-06 audit."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Check Size\nnova@example.com,\"$25k - $50k, $50k - $100k\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


@pytest.mark.asyncio
async def test_check_size_institutional_populates_independently_from_personal(import_service):
    """Real shape confirmed in the audit: personal and institutional check size are
    genuinely distinct columns with different values -- institutional must never be
    derived from or copied from personal, and vice versa."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Check Size,Check Size (Institutional)\n"
            "nova@example.com,\"$1k - $10k, $10k - $25k\",\"$500k - $1M, $1M - $2M\"\n"
        ),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["check_size_personal"] == ["$1k - $10k", "$10k - $25k"]
    assert contact.custom_fields["check_size_institutional"] == ["$500k - $1M", "$1M - $2M"]


@pytest.mark.asyncio
async def test_check_size_dollar_amount_with_thousands_comma_never_shredded_end_to_end(import_service):
    """The critical parsing edge case, run through the real pipeline end to end:
    "$5,000-$10,000" must survive as ONE unrecognized value (dropped, since it
    matches no canonical bucket), never shredded by a naive split(",") into "$5"
    and "000-$10,000". A genuine bucket in the same cell must still be captured."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Check Size\nnova@example.com,\"$5,000-$10,000, $25k - $50k\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["check_size_personal"] == ["$25k - $50k"]
    assert contact.source_snapshot["Check Size"] == "$5,000-$10,000, $25k - $50k"  # original text never lost


@pytest.mark.asyncio
async def test_check_size_free_text_never_populates_the_field_but_survives_in_source_snapshot(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Check Size\nnova@example.com,Depends on Asset Allocation\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert "check_size_personal" not in contact.custom_fields
    assert contact.source_snapshot["Check Size"] == "Depends on Asset Allocation"


@pytest.mark.asyncio
async def test_check_size_merges_with_existing_never_overwrites_populated_value(import_service):
    """Same merge policy as Dinner Subscriptions/Dinners Attended: existing selections
    are never replaced, only added to; formatting-only differences between the existing
    canonical value and a re-normalized incoming value must not create a duplicate."""
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact({
        "email": "known@example.com",
        "custom_fields": {"check_size_personal": ["$25k - $50k"]},
    })
    batch = await import_service.upload(
        "newer.csv",
        # "$25k-$50k" (no spaces) is the SAME bucket as the existing "$25k - $50k" --
        # must not duplicate it -- while "$50k - $100k" is genuinely new and merges in.
        csv_bytes("Email,Check Size\nknown@example.com,\"$25k-$50k, $50k - $100k\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


@pytest.mark.asyncio
async def test_check_size_blank_csv_cell_never_erases_existing_value(import_service):
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"email": "known@example.com", "custom_fields": {"check_size_personal": ["$25k - $50k"]}}
    )
    batch = await import_service.upload("future.csv", csv_bytes("Email,Check Size\nknown@example.com,\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["check_size_personal"] == ["$25k - $50k"]


@pytest.mark.asyncio
async def test_future_csv_upload_populates_core_field_aliases_with_zero_manual_mapping(import_service):
    """The exact scenario the user described for Person Linkedin Url/Company Name
    for Emails/Secondary Email/Corporate Phone/Gender: a human just accepts
    suggest_mapping()'s default suggestion (no manual column mapping at all), and
    every one of these fields still lands correctly."""
    await _seed_validated_single_selects(import_service)
    headers_row = (
        "Email,Person Linkedin Url,Company Name for Emails,Secondary Email,Corporate Phone,Gender\n"
        "nova@example.com,http://linkedin.com/in/nova,Nova Inc,nova2@example.com,555-0100,Female\n"
    )
    batch = await import_service.upload("future.csv", csv_bytes(headers_row))
    mapping = suggest_mapping(batch.headers)  # zero manual mapping -- exactly what suggest_mapping() gives by default
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.linkedin_url == "http://linkedin.com/in/nova"
    assert contact.company == "Nova Inc"
    assert contact.custom_fields["secondary_email"] == "nova2@example.com"
    assert contact.custom_fields["corporate_phone"] == "555-0100"
    assert contact.custom_fields["gender"] == "Female"


@pytest.mark.asyncio
async def test_stage_column_populates_engagement_stage_not_funding_stage(import_service):
    """The exact bug found in the 2026-08-06 two-CSV audit: a CSV `Stage` column
    (outreach/engagement value) must land in engagement_stage, never funding_stage --
    with zero manual mapping, using the same default suggest_mapping() a human gets."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload("future.csv", csv_bytes("Email,Stage\nnova@example.com,Interested\n"))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["engagement_stage"] == "Interested"
    assert contact.funding_stage is None  # never populated by Stage


@pytest.mark.asyncio
async def test_funding_stage_column_still_populates_funding_stage(import_service):
    """Preserves the legitimate Funding Stage -> funding_stage mapping -- a real
    funding-round value from a genuine "Funding Stage" column must still land
    in the core field, unaffected by the Stage/engagement_stage fix."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload("future.csv", csv_bytes("Email,Funding Stage\nnova@example.com,Series A\n"))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.funding_stage == "Series A"
    assert "engagement_stage" not in contact.custom_fields  # never populated by Funding Stage


@pytest.mark.asyncio
async def test_stage_and_funding_stage_columns_coexist_without_colliding(import_service):
    """Both columns present in the same row (as could happen in a future export
    combining both concepts) -- each must land in its own distinct field, neither
    overwriting or blending with the other."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Stage,Funding Stage\nnova@example.com,Cold,Seed\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["engagement_stage"] == "Cold"
    assert contact.funding_stage == "Seed"


@pytest.mark.asyncio
async def test_stage_replied_populates_engagement_stage_automatically(import_service):
    """"Replied" is a real, live engagement_stage option (17 genuine occurrences in the
    audit) -- a future upload must capture it, not drop it just because it wasn't in the
    field's original guessed option list."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload("future.csv", csv_bytes("Email,Stage\nnova@example.com,Replied\n"))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["engagement_stage"] == "Replied"
    assert contact.funding_stage is None


@pytest.mark.asyncio
async def test_stage_no_stage_placeholder_never_populates_engagement_stage():
    """"(No Stage)" is a null/unset placeholder, not a real stage -- a future upload must
    never fabricate a literal "(No Stage)" engagement_stage value from it."""
    crm = CrmService()
    await crm.create_custom_field(
        "engagement_stage", "Engagement Stage", CustomFieldType.SINGLE_SELECT,
        options=["Cold", "Interested", "Unresponsive", "Replied"],
    )
    import_service = CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())
    batch = await import_service.upload("future.csv", csv_bytes("Email,Stage\nnova@example.com,(No Stage)\n"))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await crm.list_contacts()).items[0]
    assert "engagement_stage" not in contact.custom_fields
    assert contact.funding_stage is None


@pytest.mark.asyncio
async def test_blank_csv_cell_never_erases_existing_chris_degree_connection(import_service):
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"email": "known@example.com", "custom_fields": {"chris_degree_connection": "1st degree"}}
    )
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Chris Degree Connection\nknown@example.com,\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["chris_degree_connection"] == "1st degree"  # blank cell never erases it


@pytest.mark.asyncio
async def test_blank_csv_cell_never_erases_existing_age_range(import_service):
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"email": "known@example.com", "custom_fields": {"age_range": "31-40"}}
    )
    batch = await import_service.upload("future.csv", csv_bytes("Email,Age Range\nknown@example.com,\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["age_range"] == "31-40"


@pytest.mark.asyncio
async def test_carlos_oviedo_merge_scenario(import_service):
    """User's specified merge test: existing Dinner Subscriptions
    [Investor, Founder, Biz Dev] + newer CSV [Investor, Founder, Not actively
    Investing] -> [Investor, Founder, Biz Dev, Not actively Investing]. Dinners
    Attended must likewise merge (add-only), never replace."""
    existing = await import_service.crm_service.create_contact({
        "email": "carlos@carloscardenas.com",
        "custom_fields": {
            "dinner_subscriptions": ["Investor Dinners", "Founder Dinners", "Biz Dev Dinners"],
            "dinners_attended": ["Investor Dinners", "Alpha Rose [08.13.2025] Austin", "Biz Dev Dinners", "Ensitech [11.13.2025] Austin"],
        },
    })

    batch = await import_service.upload(
        "newer.csv",
        csv_bytes(
            "Email,Dinner Subscriptions,Dinners Attended\n"
            "carlos@carloscardenas.com,"
            "\"Investor Dinners, Founder Dinners, Not actively Investing\","
            "\"Investor Dinners, Metropolitan Development Co [11.13.2025] Austin, Founder Dinners\"\n"
        ),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["dinner_subscriptions"] == [
        "Investor Dinners", "Founder Dinners", "Biz Dev Dinners", "Not actively Investing",
    ]
    assert updated.custom_fields["dinners_attended"] == [
        "Investor Dinners", "Alpha Rose [08.13.2025] Austin", "Biz Dev Dinners", "Ensitech [11.13.2025] Austin",
        "Metropolitan Development Co [11.13.2025] Austin", "Founder Dinners",
    ]


@pytest.mark.asyncio
async def test_alex_pepe_end_to_end(import_service):
    """User's specified final verification case -- values taken verbatim from
    Alex Pepe's real row in Contacts 2 (ITF).csv (2026-08-06 audit), not
    hardcoded/guessed. A brand-new contact, all 4 fields unmapped, zero manual
    configuration, using the same suggested mapping a human would just accept."""
    await _seed_validated_single_selects(import_service)
    row = (
        "Email,Dinner Subscriptions,Dinners Attended,Chris Degree Connection,Age Range,Check Size,"
        "Do Not Call,Chris knows personally,Accredited Status,Deal Stage,"
        "Investing in these types of assets,Investing in these business models:,"
        "Would like to meet founders by\n"
        "alexjpepe@gmail.com,"
        "\"Investor Dinners, Fireside Dinners, Biz Dev Dinners\","
        "\"Investor Dinners, Fireside Dinners, Savvy [2.25.2025] Austin, VacayMyWay [08.12.2025] Austin, "
        "Alpha Rose [08.13.2025] Austin, Biz Dev Dinners, Ensitech [11.13.2025] Austin, "
        "SharpsAI [12.04.2025] Austin, Civilization Fund [01.19.2026] Austin, Predict RX [03.10.2026] Austin, "
        "Submersive [04.30.2026] Austin\","
        "1st degree,61-70,\"$25k - $50k, $50k - $100k\","
        "false,Yes,Yes,"
        "\"Pre-seed, Seed, Series A, Series B, Series C,D,E, Fund LP, Secondary\","
        "\"Hedge funds, Infrastructure, Private credit, Private equity, Public equities, Real estate, "
        "Royalty financing, Venture capital\","
        "\"Hardware / Physical products, Licensing / IP-based, Manufacturing, Marketplaces, "
        "Software as a Service (SaaS)\","
        "\"Email intro, Zoom Call\"\n"
    )
    batch = await import_service.upload("future.csv", csv_bytes(row))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    alex = (await import_service.crm_service.list_contacts()).items[0]
    assert alex.custom_fields["dinner_subscriptions"] == ["Investor Dinners", "Fireside Dinners", "Biz Dev Dinners"]
    assert alex.custom_fields["dinners_attended"] == [
        "Investor Dinners", "Fireside Dinners", "Savvy [2.25.2025] Austin", "VacayMyWay [08.12.2025] Austin",
        "Alpha Rose [08.13.2025] Austin", "Biz Dev Dinners", "Ensitech [11.13.2025] Austin",
        "SharpsAI [12.04.2025] Austin", "Civilization Fund [01.19.2026] Austin", "Predict RX [03.10.2026] Austin",
        "Submersive [04.30.2026] Austin",
    ]
    assert alex.custom_fields["chris_degree_connection"] == "1st degree"
    assert alex.custom_fields["age_range"] == "61-70"
    assert alex.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]
    # Phase 1 (2026-08-06 broader-audit) fields, real Alex Pepe values
    assert alex.custom_fields["do_not_call"] is False
    assert alex.custom_fields["chris_knows_personally"] is True
    assert alex.custom_fields["accredited_status"] == "Yes"
    # Phase 2 (legacy thesis translation) fields, real Alex Pepe values -- including
    # the "Series C,D,E" shorthand collapsing to the same canonical bucket as B/C.
    assert alex.thesis_private_deal_stages == [
        "Pre-Seed (early development, pre-revenue or minimal traction)",
        "Seed (product in market, early customers or pilots)",
        "Series A (scaling phase, revenue traction, team expansion)",
        "Series B or later (growth or expansion stage, institutional rounds)",
        "Fund LP (investor in venture/private equity funds)",
        "Secondary (buying equity from early investors or founders)",
    ]
    assert alex.thesis_private_asset_types == [
        "Hedge funds (multi-asset strategies)",
        "Infrastructure (e.g., toll roads, utilities, airports)",
        "Private credit (e.g., private loans, direct lending)",
        "Private equity",
        "Public equities (stocks, ETFs)",
        "Real estate (direct ownership, syndications, REITs)",
        "Royalty financing",
        "Venture capital (e.g., angel checks, early-stage startups, high-growth tech)",
    ]
    assert alex.thesis_private_business_models == [
        "Hardware / Physical products", "Licensing / IP-based", "Manufacturing",
        "Marketplaces (e.g., Airbnb, Uber-style platforms)", "Software as a Service (SaaS)",
    ]
    assert alex.thesis_private_meeting_preferences == ["In an email intro", "I'd do a Zoom call"]


@pytest.mark.asyncio
async def test_classification_rule_preserves_source_snapshot_unchanged(import_service):
    batch = await import_service.upload(
        "p.csv",
        csv_bytes("Email,Industry,Main Industry,Sub-industry\nada@example.com,Fintech,Healthcare,Biotech\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.source_snapshot == {
        "Email": "ada@example.com", "Industry": "Fintech", "Main Industry": "Healthcare", "Sub-industry": "Biotech",
    }


@pytest.mark.asyncio
async def test_update_decision_preserves_industry_and_merges_investment_industry(import_service):
    """industry (external field) is now protected the same as every other field --
    a conflicting incoming value never overwrites an existing one. investment_industry
    (multi-select custom field) still union-merges its existing selections with the
    incoming ones rather than replacing or freezing the list."""
    existing = await import_service.crm_service.create_contact({"email": "known@example.com", "industry": "Old Value"})
    existing = await import_service.crm_service.update_contact(
        existing.crm_contact_id, {"custom_fields": {"investment_industry": ["Existing Value"]}}
    )

    batch = await import_service.upload(
        "p.csv",
        csv_bytes("Email,Industry,Main Industry\nknown@example.com,New Apollo Industry,New Investment Interest\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.industry == "Old Value"  # external field: preserved, not overwritten by a conflicting value
    assert updated.custom_fields["investment_industry"] == ["Existing Value", "New Investment Interest"]  # merged, not replaced


# --- Classification rules (Investor Type -> mode, Role) ---
#
# These confirm the rules run through the REAL preview()/commit() pipeline,
# not just as standalone function calls -- the actual permanent behavior
# for every future upload.


@pytest.mark.asyncio
async def test_future_upload_derives_investor_mode_from_investor_type(import_service):
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Investor type\nnova@example.com,\"Angel Investor, Venture Capital\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})  # Investor type deliberately left unmapped
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["investor_type"] == ["Angel Investor", "Venture Capital"]
    assert contact.thesis_investor_mode == "Both"
    assert contact.thesis_investor_mode_manual_override is False


@pytest.mark.asyncio
async def test_future_upload_leaves_investor_mode_unset_when_no_signal(import_service):
    batch = await import_service.upload("future.csv", csv_bytes("Email,Investor type\nnova@example.com,Hedge Fund\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["investor_type"] == ["Hedge Fund"]  # never reinterpreted
    assert contact.thesis_investor_mode is None  # never guessed


@pytest.mark.asyncio
async def test_manual_override_blocks_investor_mode_from_a_future_import(import_service):
    """An existing contact with manual_override=True must keep their manually
    chosen mode even when a later CSV upload carries Investor Type data."""
    existing = await import_service.crm_service.create_contact({"email": "known@example.com"})
    existing = await import_service.crm_service.update_contact(
        existing.crm_contact_id,
        {"thesis_investor_mode": "Institutionally", "thesis_investor_mode_manual_override": True},
    )

    batch = await import_service.upload("future.csv", csv_bytes("Email,Investor type\nknown@example.com,Angel Investor\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.thesis_investor_mode == "Institutionally"  # untouched -- manual override respected


@pytest.mark.asyncio
async def test_future_upload_filters_role_to_the_live_approved_taxonomy(import_service):
    await import_service.crm_service.create_custom_field(
        field_key="role", label="Role", field_type=CustomFieldType.MULTI_SELECT,
        options=["Investor", "Founder", "CEO"],
    )
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Role\nnova@example.com,\"Investor, Founder, VP, President\"\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["role"] == ["Investor", "Founder"]  # VP/President dropped, nothing invented


@pytest.mark.asyncio
async def test_role_with_nothing_approved_leaves_the_field_unset(import_service):
    await import_service.crm_service.create_custom_field(
        field_key="role", label="Role", field_type=CustomFieldType.MULTI_SELECT,
        options=["Investor", "Founder", "CEO"],
    )
    batch = await import_service.upload("future.csv", csv_bytes("Email,Role\nnova@example.com,\"VP, Director\"\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert "role" not in contact.custom_fields


@pytest.mark.asyncio
async def test_role_rule_works_even_when_role_field_does_not_exist_yet(import_service):
    """No role custom field defined at all -- must not error, must not
    invent the field, just leaves it unset."""
    batch = await import_service.upload("future.csv", csv_bytes("Email,Role\nnova@example.com,Investor\n"))
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert "role" not in contact.custom_fields


# --- 2026-08-06 broader-audit Phase 1: the ten plain scalar mappings ---


@pytest.mark.asyncio
async def test_future_upload_populates_all_ten_phase1_fields_with_zero_manual_mapping(import_service):
    """All ten Phase 1 fields, both CSV headers, via suggest_mapping()'s default --
    exactly what a human gets by accepting the suggested mapping with no changes."""
    await _seed_validated_single_selects(import_service)
    headers_row = (
        "Email,Work Direct Phone,Do Not Call,Last Raised At,How often do you invest?,"
        "Personal Notes,Notes,Who were you referred to Constellation Dinners by?,"
        "Geographic preference,Chris knows personally,Accredited Status,"
        "Qualify Contact,DO NOT INVEST IN\n"
        "nova@example.com,555-0100,false,2025-07-01T00:00:00+00:00,4 per year,"
        "Family office context,General notes here,Chris Beaman,"
        "\"Austin, Texas\",Yes,Yes,Warm intro only,\"Wearables, Digital Health\"\n"
    )
    batch = await import_service.upload("future.csv", csv_bytes(headers_row))
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    cf = contact.custom_fields
    assert cf["work_direct_phone"] == "555-0100"
    assert cf["do_not_call"] is False
    assert cf["last_raised_at"] == "2025-07-01T00:00:00+00:00"
    assert cf["how_often_do_you_invest"] == "4 per year"
    assert cf["personal_notes"] == "Family office context"
    assert cf["notes"] == "General notes here"
    assert cf["referred_to_constellation_dinners_by"] == "Chris Beaman"
    assert cf["investment_geography_preference"] == "Austin, Texas"
    assert cf["chris_knows_personally"] is True
    assert cf["accredited_status"] == "Yes"
    assert cf["qualify_contact"] == "Warm intro only"
    assert cf["do_not_invest_in"] == "Wearables, Digital Health"


@pytest.mark.asyncio
async def test_chris_knows_personally_boolean_coercion_yes_and_false_strings(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Chris knows personally,Do Not Call\nnova@example.com,Yes,false\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["chris_knows_personally"] is True
    assert contact.custom_fields["do_not_call"] is False


@pytest.mark.asyncio
async def test_accredited_status_single_select_validation_drops_unrecognized(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Accredited Status\nnova@example.com,Maybe\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email"})
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert "accredited_status" not in contact.custom_fields


# --- 2026-08-06 Contacts 3 (Investors) audit: Revenue Stage, Qualify Contact, DO NOT INVEST IN ---


@pytest.mark.asyncio
async def test_revenue_stage_single_select_validation_recognizes_live_option(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Revenue Stage\nnova@example.com,$1M - $10M\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["revenue_stage"] == "$1M - $10M"


@pytest.mark.asyncio
async def test_revenue_stage_single_select_validation_drops_unrecognized(import_service):
    """Real Contacts 3 (Investors) values like '$100K - $250K' (not a live option),
    '$0-$50k', and 'Just getting started' must never be guessed into the nearest bucket."""
    await _seed_validated_single_selects(import_service)
    for raw in ["$100K - $250K", "$0-$50k", "Just getting started"]:
        batch = await import_service.upload(
            "future.csv", csv_bytes(f"Email,Revenue Stage\nnova+{raw!r}@example.com,{raw}\n"),
        )
        await import_service.preview(batch.import_batch_id, {"Email": "email"})
        await import_service.commit(batch.import_batch_id)

    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 3
    for contact in contacts:
        assert "revenue_stage" not in contact.custom_fields


@pytest.mark.asyncio
async def test_revenue_stage_never_touches_deal_stage_or_deprecated_check_size_fields(import_service):
    """Revenue Stage (the contact's own company) is a different concept from Deal Stage
    (their investing preference) and from Check Size -- confirm no cross-contamination."""
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Revenue Stage,Deal Stage\nnova@example.com,$1M - $10M,Seed\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["revenue_stage"] == "$1M - $10M"
    assert contact.thesis_private_deal_stages == ["Seed (product in market, early customers or pilots)"]
    assert contact.thesis_private_check_sizes == []
    assert contact.thesis_institutional_check_sizes == []
    assert "check_size_personal" not in contact.custom_fields
    assert "check_size_institutional" not in contact.custom_fields


@pytest.mark.asyncio
async def test_qualify_contact_and_do_not_invest_in_plain_scalar_mapping(import_service):
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Qualify Contact,DO NOT INVEST IN\n"
            "nova@example.com,Warm intro only,\"Wearables, Digital Health\"\n"
        ),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["qualify_contact"] == "Warm intro only"
    assert contact.custom_fields["do_not_invest_in"] == "Wearables, Digital Health"


@pytest.mark.asyncio
async def test_qualify_contact_rule_works_even_when_field_does_not_exist_yet(import_service):
    """No qualify_contact custom field defined at all -- must not error; the raw value
    is stored as-is (same fallback as every other custom-field alias when the
    definition is missing -- see _coerce_value)."""
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Qualify Contact\nnova@example.com,Warm intro only\n"),
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email", "Qualify Contact": "custom:qualify_contact"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["qualify_contact"] == "Warm intro only"


@pytest.mark.asyncio
async def test_new_mappings_never_overwrite_existing_populated_values(import_service):
    """Same fill-only-if-empty policy as every other custom field -- an existing
    populated value must survive a re-import with a different incoming value."""
    await _seed_validated_single_selects(import_service)
    await import_service.crm_service.create_contact({
        "email": "known@example.com",
        "custom_fields": {
            "revenue_stage": "$10M - $100M", "qualify_contact": "Already qualified",
            "do_not_invest_in": "Crypto",
        },
    })
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Revenue Stage,Qualify Contact,DO NOT INVEST IN\n"
            "known@example.com,$1M - $10M,New value,Real Estate\n"
        ),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    updated = (await import_service.crm_service.list_contacts()).items[0]
    assert updated.custom_fields["revenue_stage"] == "$10M - $100M"
    assert updated.custom_fields["qualify_contact"] == "Already qualified"
    assert updated.custom_fields["do_not_invest_in"] == "Crypto"


@pytest.mark.asyncio
async def test_phase1_scalar_fields_never_overwrite_existing_populated_value(import_service):
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact({
        "email": "known@example.com",
        "custom_fields": {
            "notes": "Original note", "accredited_status": "No", "chris_knows_personally": False,
        },
    })
    batch = await import_service.upload(
        "newer.csv",
        csv_bytes("Email,Notes,Accredited Status,Chris knows personally\nknown@example.com,New note,Yes,Yes\n"),
    )
    mapping = suggest_mapping(batch.headers)  # Notes/Chris knows personally are alias-based, not
    # classification-rule-driven -- must use the real suggested mapping, not a narrowed one, or
    # they'd never even be read and this test would pass for the wrong reason.
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["notes"] == "Original note"
    assert updated.custom_fields["accredited_status"] == "No"
    assert updated.custom_fields["chris_knows_personally"] is False


@pytest.mark.asyncio
async def test_phase1_scalar_fields_blank_csv_cell_never_erases_existing_value(import_service):
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"email": "known@example.com", "custom_fields": {"work_direct_phone": "555-0100", "personal_notes": "x"}}
    )
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Work Direct Phone,Personal Notes\nknown@example.com,,\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["work_direct_phone"] == "555-0100"
    assert updated.custom_fields["personal_notes"] == "x"


@pytest.mark.asyncio
async def test_phase1_fields_matched_via_apollo_contact_id(import_service):
    """Apollo ID/email/LinkedIn matching is unchanged by Phase 1 -- proves a Phase 1
    field lands on the correct contact even when matched purely by Apollo ID."""
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"apollo_contact_id": "apollo-123", "email": "old@example.com", "first_name": "Nova"}
    )
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Apollo Contact Id,Email,First Name,Notes\napollo-123,new@example.com,Nova,Matched by Apollo ID\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.custom_fields["notes"] == "Matched by Apollo ID"
    assert updated.email == "old@example.com"  # populated external field never overwritten


@pytest.mark.asyncio
async def test_phase1_field_identity_conflict_still_blocked(import_service):
    """Same identity-conflict guard as every other field: a row sharing an email with
    an existing contact but a fully mismatched name must never write into it."""
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact(
        {"email": "shared@example.com", "first_name": "James", "last_name": "Feldkamp"}
    )
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,First Name,Last Name,Notes\nshared@example.com,Shawn,Riely,Should not merge\n"),
    )
    mapping = suggest_mapping(batch.headers)  # Notes is alias-based -- must be really mapped so
    # this test proves the identity-conflict guard is what blocks it, not a missing mapping.
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 0  # defaults to skip, never silently merged
    unchanged = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert "notes" not in unchanged.custom_fields


# --- 2026-08-06 broader-audit Phase 2: legacy thesis-column translation, full pipeline ---


@pytest.mark.asyncio
async def test_future_upload_populates_deal_stage_with_legacy_translation(import_service):
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Deal Stage\nnova@example.com,\"Pre-seed, Seed, Series A\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_private_deal_stages == [
        "Pre-Seed (early development, pre-revenue or minimal traction)",
        "Seed (product in market, early customers or pilots)",
        "Series A (scaling phase, revenue traction, team expansion)",
    ]


@pytest.mark.asyncio
async def test_future_upload_populates_asset_types_with_internal_comma_canonical_values(import_service):
    """The critical parsing edge case, run through the real pipeline: 'Collectibles'
    translates to a canonical value that itself contains commas -- must survive whole."""
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Investing in these types of assets\nnova@example.com,\"Collectibles, Real estate\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_private_asset_types == [
        "Collectibles (e.g., art, wine, watches)",
        "Real estate (direct ownership, syndications, REITs)",
    ]


@pytest.mark.asyncio
async def test_future_upload_populates_business_models_with_legacy_translation(import_service):
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Investing in these business models:\nnova@example.com,\"Marketplaces\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_private_business_models == ["Marketplaces (e.g., Airbnb, Uber-style platforms)"]


@pytest.mark.asyncio
async def test_future_upload_populates_meeting_preferences_with_legacy_translation(import_service):
    batch = await import_service.upload(
        "future.csv",
        csv_bytes("Email,Would like to meet founders by\nnova@example.com,\"Email intro, Zoom Call\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_private_meeting_preferences == ["In an email intro", "I'd do a Zoom call"]


@pytest.mark.asyncio
async def test_future_upload_never_populates_founder_diversity_preference(import_service):
    """Deliberately excluded from Phase 2 -- still an open duplicate-destination
    question against the gender-specific dining column (2026-08-06 broader audit)."""
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Founder Diversity Preference\nnova@example.com,Open to investing in anyone\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_private_demographic_preferences == []


@pytest.mark.asyncio
async def test_legacy_thesis_fields_never_overwrite_existing_populated_value(import_service):
    existing = await import_service.crm_service.create_contact({
        "email": "known@example.com",
        "thesis_private_deal_stages": ["Series B or later (growth or expansion stage, institutional rounds)"],
    })
    batch = await import_service.upload(
        "newer.csv", csv_bytes("Email,Deal Stage\nknown@example.com,\"Pre-seed, Seed\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    # Core thesis fields fill-only-if-empty (never merge, never overwrite) -- same
    # policy as every other core/external field, established before this round.
    assert updated.thesis_private_deal_stages == [
        "Series B or later (growth or expansion stage, institutional rounds)",
    ]


@pytest.mark.asyncio
async def test_future_upload_populates_how_early_do_you_invest_with_internal_commas(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,How early do you invest?\n"
            "nova@example.com,\"Great team, no revenue, Great team, some revenue, $1M+ MRR / GMV\"\n"
        ),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["how_early_do_you_invest"] == [
        "Great team, no revenue", "Great team, some revenue", "$1M+ MRR / GMV",
    ]


# --- Idempotency (repeat upload of the same rows) ---


@pytest.mark.asyncio
async def test_repeat_upload_of_phase1_and_phase2_fields_is_idempotent(import_service):
    await _seed_validated_single_selects(import_service)
    content = csv_bytes(
        "Email,Notes,Accredited Status,Deal Stage,How early do you invest?\n"
        "nova@example.com,A note,Yes,\"Pre-seed, Seed\",\"Great team, no revenue\"\n"
    )

    batch1 = await import_service.upload("run1.csv", content)
    mapping = suggest_mapping(batch1.headers)
    await import_service.preview(batch1.import_batch_id, mapping)
    report1 = await import_service.commit(batch1.import_batch_id)
    assert report1.created == 1

    batch2 = await import_service.upload("run2.csv", content)
    await import_service.preview(batch2.import_batch_id, mapping)
    report2 = await import_service.commit(batch2.import_batch_id)
    assert report2.created == 0
    assert report2.updated == 1  # matched the same contact, not duplicated

    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact.custom_fields["notes"] == "A note"
    assert contact.custom_fields["accredited_status"] == "Yes"
    assert contact.thesis_private_deal_stages == [
        "Pre-Seed (early development, pre-revenue or minimal traction)",
        "Seed (product in market, early customers or pilots)",
    ]
    assert contact.custom_fields["how_early_do_you_invest"] == ["Great team, no revenue"]


# --- Check Size regression (must remain fully untouched by Phase 1/2) ---


@pytest.mark.asyncio
async def test_check_size_still_works_unchanged_alongside_new_phase1_phase2_fields(import_service):
    await _seed_validated_single_selects(import_service)
    batch = await import_service.upload(
        "future.csv",
        csv_bytes(
            "Email,Check Size,Deal Stage,Notes\n"
            "nova@example.com,\"$25k - $50k, $50k - $100k\",\"Seed\",A note\n"
        ),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]
    assert contact.thesis_private_deal_stages == ["Seed (product in market, early customers or pilots)"]
    assert contact.custom_fields["notes"] == "A note"
    # 2026-08-06 Check Size consolidation: the deprecated thesis check-size fields
    # must never be populated by CSV import, regardless of what got mapped/classified.
    assert contact.thesis_private_check_sizes == []
    assert contact.thesis_institutional_check_sizes == []


@pytest.mark.asyncio
async def test_check_size_import_never_touches_deprecated_thesis_fields_even_when_pre_populated(import_service):
    """A contact that already has legacy data sitting in the deprecated thesis
    check-size fields (from before the consolidation) must have that data left
    exactly as-is by a fresh CSV import -- never deleted, never added to."""
    await _seed_validated_single_selects(import_service)
    existing = await import_service.crm_service.create_contact({
        "email": "known@example.com",
        "thesis_private_check_sizes": ["$1M - $2M"],
        "thesis_institutional_check_sizes": ["$10M+"],
    })
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Check Size\nknown@example.com,\"$25k - $50k\"\n"),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.updated == 1
    updated = await import_service.crm_service.get_contact(existing.crm_contact_id)
    assert updated.thesis_private_check_sizes == ["$1M - $2M"]  # untouched, not deleted
    assert updated.thesis_institutional_check_sizes == ["$10M+"]  # untouched, not deleted
    assert updated.custom_fields["check_size_personal"] == ["$25k - $50k"]  # new data lands only here


# --- 2026-08-07 Dietary Preferences: TEXT -> validated MULTI_SELECT via a real import ---


@pytest.mark.asyncio
async def test_dietary_preferences_semicolon_split_end_to_end(import_service):
    batch = await import_service.upload(
        "future.csv", csv_bytes('Email,Dietary Preferences\nnova@example.com,"Vegetarian;Gluten-Free"\n'),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_dietary_preferences == ["Vegetarian", "Gluten-Free"]
    assert contact.thesis_dietary_preferences_other is None


@pytest.mark.asyncio
async def test_dietary_preferences_unrecognized_value_lands_in_other_end_to_end(import_service):
    batch = await import_service.upload(
        "future.csv", csv_bytes('Email,Dietary Preferences\nnova@example.com,"Vegetarian;Cayenne-Free"\n'),
    )
    mapping = suggest_mapping(batch.headers)
    await import_service.preview(batch.import_batch_id, mapping)
    await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_dietary_preferences == ["Vegetarian", "Other"]
    assert contact.thesis_dietary_preferences_other == "Cayenne-Free"


@pytest.mark.asyncio
async def test_dietary_preferences_union_merges_across_two_separate_imports(import_service):
    """The exact spec example: existing Vegetarian + a later CSV's Gluten-Free = both,
    never one replacing the other, across two genuinely separate upload/preview/commit
    cycles (not the same batch)."""
    batch1 = await import_service.upload(
        "first.csv", csv_bytes("Email,Dietary Preferences\nnova@example.com,Vegetarian\n"),
    )
    await import_service.preview(batch1.import_batch_id, suggest_mapping(batch1.headers))
    await import_service.commit(batch1.import_batch_id)

    batch2 = await import_service.upload(
        "second.csv", csv_bytes("Email,Dietary Preferences\nnova@example.com,Gluten-Free\n"),
    )
    await import_service.preview(batch2.import_batch_id, suggest_mapping(batch2.headers))
    report2 = await import_service.commit(batch2.import_batch_id)

    assert report2.updated == 1
    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_dietary_preferences == ["Vegetarian", "Gluten-Free"]


@pytest.mark.asyncio
async def test_dietary_preferences_reimport_does_not_duplicate_or_lose_values(import_service):
    """Idempotency at the field level: re-importing the exact same row a second time
    must not duplicate an already-present value or drop anything."""
    for _ in range(2):
        batch = await import_service.upload(
            "future.csv", csv_bytes("Email,Dietary Preferences\nnova@example.com,Vegetarian;Gluten-Free\n"),
        )
        await import_service.preview(batch.import_batch_id, suggest_mapping(batch.headers))
        await import_service.commit(batch.import_batch_id)

    contact = (await import_service.crm_service.list_contacts()).items[0]
    assert contact.thesis_dietary_preferences == ["Vegetarian", "Gluten-Free"]  # unchanged, not doubled


@pytest.mark.asyncio
async def test_dietary_preferences_other_field_appears_in_export_fields():
    from app.models.crm import get_contact_export_fields

    fields_by_key = {f.key: f.kind for f in get_contact_export_fields()}
    assert fields_by_key["thesis_dietary_preferences"] == "list"
    assert fields_by_key["thesis_dietary_preferences_other"] == "scalar"


@pytest.mark.asyncio
async def test_dietary_preferences_never_overwrites_a_prior_contacts_value_with_a_new_contacts_row(import_service):
    """Sanity check that the new union-merge behavior is scoped per-contact --
    a brand-new contact's row must never affect an unrelated existing contact."""
    await import_service.crm_service.create_contact({
        "email": "existing@example.com", "thesis_dietary_preferences": ["Halal"],
    })
    batch = await import_service.upload(
        "future.csv", csv_bytes("Email,Dietary Preferences\nnova@example.com,Vegan\n"),
    )
    await import_service.preview(batch.import_batch_id, suggest_mapping(batch.headers))
    await import_service.commit(batch.import_batch_id)

    contacts = {c.email: c for c in (await import_service.crm_service.list_contacts()).items}
    assert contacts["existing@example.com"].thesis_dietary_preferences == ["Halal"]
    assert contacts["nova@example.com"].thesis_dietary_preferences == ["Vegan"]


# --- import_one_row (2026-08-10 ITF intake design) ---


@pytest.mark.asyncio
async def test_import_one_row_creates_a_new_contact(import_service):
    context = await build_classification_context(import_service.crm_service.custom_field_store)
    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "new@example.com", "First Name": "Ada"},
        {"Email": "email", "First Name": "first_name"},
        context,
    )
    assert status == CrmImportRowStatus.NEW
    assert contact is not None
    assert contact.email == "new@example.com"
    assert mapped["email"] == "new@example.com"

    stored = await import_service.crm_service.contact_store.get(contact.crm_contact_id)
    assert stored is not None


@pytest.mark.asyncio
async def test_import_one_row_updates_an_existing_contact(import_service):
    existing = await import_service.crm_service.create_contact_from_import(
        {"email": "existing@example.com", "first_name": "Ada"}
    )
    context = await build_classification_context(import_service.crm_service.custom_field_store)

    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "existing@example.com", "First Name": "Ada", "Last Name": "Lovelace"},
        {"Email": "email", "First Name": "first_name", "Last Name": "last_name"},
        context,
    )
    assert status == CrmImportRowStatus.EXISTING
    assert matched_on == "email"
    assert contact.crm_contact_id == existing.crm_contact_id
    assert contact.last_name == "Lovelace"  # filled in, since it was empty

    stored = await import_service.crm_service.contact_store.get(existing.crm_contact_id)
    assert stored.last_name == "Lovelace"


@pytest.mark.asyncio
async def test_import_one_row_dry_run_never_writes(import_service):
    context = await build_classification_context(import_service.crm_service.custom_field_store)
    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "new@example.com"}, {"Email": "email"}, context, dry_run=True,
    )
    assert status == CrmImportRowStatus.NEW
    assert contact is None  # never created
    assert (await import_service.crm_service.list_contacts()).items == []


@pytest.mark.asyncio
async def test_import_one_row_dry_run_reports_the_matched_existing_contact_without_updating(import_service):
    existing = await import_service.crm_service.create_contact_from_import(
        {"email": "existing@example.com", "last_name": "Original"}
    )
    context = await build_classification_context(import_service.crm_service.custom_field_store)

    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "existing@example.com", "Last Name": "Changed"},
        {"Email": "email", "Last Name": "last_name"},
        context,
        dry_run=True,
    )
    assert status == CrmImportRowStatus.EXISTING
    assert contact.crm_contact_id == existing.crm_contact_id
    assert contact.last_name == "Original"  # unmerged -- dry run never writes

    stored = await import_service.crm_service.contact_store.get(existing.crm_contact_id)
    assert stored.last_name == "Original"


@pytest.mark.asyncio
async def test_import_one_row_extra_fields_merge_in_after_classification(import_service):
    context = await build_classification_context(import_service.crm_service.custom_field_store)
    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "new@example.com"},
        {"Email": "email"},
        context,
        extra_fields={"source": "itf"},
    )
    assert mapped["source"] == "itf"
    assert contact.source == "itf"


@pytest.mark.asyncio
async def test_import_one_row_extra_fields_source_never_overwrites_existing_contacts_source(import_service):
    existing = await import_service.crm_service.create_contact_from_import(
        {"email": "existing@example.com", "source": "manual"}
    )
    context = await build_classification_context(import_service.crm_service.custom_field_store)

    status, contact, matched_on, mapped = await import_service.import_one_row(
        {"Email": "existing@example.com"},
        {"Email": "email"},
        context,
        extra_fields={"source": "itf"},
    )
    assert status == CrmImportRowStatus.EXISTING
    assert contact.source == "manual"  # fill-only-if-empty -- untouched
