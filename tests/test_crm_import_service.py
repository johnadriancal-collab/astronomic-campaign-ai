"""
Tests for CrmImportService: CSV parsing/upload, deterministic mapping
suggestions, the preview step's dedup classification (including within-
file duplicate detection), and commit's create/update/skip decisions +
report.
"""

import pytest

from app.models.crm import CrmImportBatchStatus, CrmImportRowStatus
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.services.crm_import_service import CrmImportService, suggest_mapping
from app.services.crm_service import CrmService


@pytest.fixture
def import_service():
    crm = CrmService()
    return CrmImportService(crm_service=crm, batch_store=MemoryCrmImportBatchStore())


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
    batch = await import_service.upload(
        "p.csv", csv_bytes("Email,Company\nsame@example.com,Old Co\nsame@example.com,New Co\n")
    )
    await import_service.preview(batch.import_batch_id, {"Email": "email", "Company": "company"})
    report = await import_service.commit(batch.import_batch_id)

    assert report.created == 1
    assert report.updated == 1
    contacts = (await import_service.crm_service.list_contacts()).items
    assert len(contacts) == 1
    assert contacts[0].company == "New Co"  # second row's update applied to the first row's new contact


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
async def test_update_decision_overwrites_industry_but_never_overwrites_existing_investment_industry(import_service):
    """industry is an external field (overwrite-if-non-empty). investment_industry
    is a custom field (never auto-overwrite an existing value) -- an update
    import must respect both rules simultaneously."""
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
    assert updated.industry == "New Apollo Industry"  # external field: overwritten
    assert updated.custom_fields["investment_industry"] == ["Existing Value"]  # custom field: protected
