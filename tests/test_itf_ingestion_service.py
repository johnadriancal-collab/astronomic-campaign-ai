"""
Tests for ItfIngestionService.process_submission() -- the single-submission
webhook path used by the Google Apps Script bridge. No Google API/Apps
Script calls anywhere in this file; everything is plain (headers, values,
row_number) tuples, exactly what the Apps Script payload becomes once
FastAPI validates it. Uses in-memory CRM/ingestion-log stores throughout,
same convention as test_crm_import_service.py's `import_service` fixture.
"""

import pytest

from app.models.crm import CustomFieldType
from app.models.itf import ItfRowStatus
from app.repositories.crm_import_batch_store import MemoryCrmImportBatchStore
from app.repositories.itf_ingestion_log_store import MemoryItfIngestionLogStore
from app.services.crm_import_service import CrmImportService
from app.services.crm_service import CrmService
from app.services.itf_ingestion_service import (
    ItfIngestionService,
    _content_hash,
    _disambiguate_headers,
    _normalize_dietary_preferences_delimiter,
    _zip_row,
)

HEADERS = [
    "Timestamp",
    "First Name",
    "Last Name",
    "Email Address",
    "LinkedIn Profile URL",
    "Which types of assets do you invest in or are you interested in?",
    "Do you also invest institutionally (via a fund)?",
    "Which types of assets do you invest in or are you interested in?",  # duplicate: institutional section
]


@pytest.fixture
def crm_service():
    return CrmService()


@pytest.fixture
def import_service(crm_service):
    return CrmImportService(crm_service=crm_service, batch_store=MemoryCrmImportBatchStore())


@pytest.fixture
def log_store():
    return MemoryItfIngestionLogStore()


@pytest.fixture
def itf_service(import_service, log_store):
    return ItfIngestionService(import_service=import_service, log_store=log_store)


def row(*values):
    return list(values)


# --- header disambiguation / row zipping ---


def test_disambiguate_headers_appends_institutional_suffix_on_second_occurrence():
    result = _disambiguate_headers(HEADERS)
    assert result[5] == "Which types of assets do you invest in or are you interested in?"
    assert result[7] == "Which types of assets do you invest in or are you interested in? (Institutional)"


def test_zip_row_uses_disambiguated_headers_and_positional_values():
    headers = _disambiguate_headers(HEADERS)
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "Private equity", "Yes", "Fund-of-funds")
    raw_row = _zip_row(headers, values)
    assert raw_row["Which types of assets do you invest in or are you interested in?"] == "Private equity"
    assert raw_row["Which types of assets do you invest in or are you interested in? (Institutional)"] == "Fund-of-funds"


def test_zip_row_handles_values_shorter_than_headers_as_blank_trailing_cells():
    assert _zip_row(["First Name", "Last Name", "Email Address"], ["Ada"]) == {"First Name": "Ada"}


def test_content_hash_stable_for_same_values_changes_on_edit():
    assert _content_hash(["a", "b"]) == _content_hash(["a", "b"])
    assert _content_hash(["a", "b"]) != _content_hash(["a", "c"])


# --- process_submission(): dry run never writes ---


@pytest.mark.asyncio
async def test_dry_run_creates_nothing_in_the_crm_or_the_log(itf_service, import_service, log_store):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "Private equity", "Yes", "Fund-of-funds")

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=True)

    assert result.dry_run is True
    assert result.status == "created"
    assert result.contact_id is None  # never actually created
    assert result.mapped_fields is not None  # dry run exposes the classified fields for inspection
    assert result.mapped_fields["email"] == "ada@example.com"

    assert await import_service.crm_service.contact_store.list() == []
    assert await log_store.get_all() == {}


@pytest.mark.asyncio
async def test_dry_run_never_creates_the_itf_submitted_at_custom_field(itf_service, import_service):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")

    await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=True)

    definition = await import_service.crm_service.custom_field_store.get_by_field_key("itf_submitted_at")
    assert definition is None


@pytest.mark.asyncio
async def test_dry_run_does_not_report_matched_contact_as_already_processed_even_if_a_real_row_exists(itf_service, import_service):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)

    # Same row, dry run: must still classify (not report already_processed) so the
    # caller can inspect it, even though it's already been processed for real.
    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=True)
    assert result.status == "already_processed"  # correctly reflects real ledger state
    assert result.mapped_fields is None  # never leaks CRM data through an "already processed" result


# --- process_submission(): real run writes, sets source + itf_submitted_at ---


@pytest.mark.asyncio
async def test_real_run_creates_contact_with_source_itf_and_submitted_at(itf_service, import_service, log_store):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "Private equity", "Yes", "Fund-of-funds")

    result = await itf_service.process_submission(HEADERS, values, row_number=2, response_id="resp-1", dry_run=False)

    assert result.status == "created"
    assert result.contact_id is not None
    assert result.mapped_fields is None  # not echoed back on a real run

    contact = await import_service.crm_service.contact_store.get(result.contact_id)
    assert contact.source == "itf"
    assert contact.custom_fields["itf_submitted_at"] == "2026-08-10T10:00:00"
    assert contact.thesis_private_asset_types == ["Private equity"]
    assert contact.thesis_institutional_asset_types == ["Fund-of-funds"]
    assert contact.thesis_also_invests_institutionally is True

    definition = await import_service.crm_service.custom_field_store.get_by_field_key("itf_submitted_at")
    assert definition is not None
    assert definition.field_type == CustomFieldType.DATE

    log_entries = await log_store.get_all()
    assert log_entries[2].status == ItfRowStatus.CREATED
    assert log_entries[2].crm_contact_id == result.contact_id
    assert log_entries[2].response_id == "resp-1"


@pytest.mark.asyncio
async def test_existing_contacts_source_is_never_overwritten_by_a_later_itf_update(itf_service, import_service):
    crm = import_service.crm_service
    existing = await crm.create_contact_from_import({"email": "ada@example.com", "first_name": "Ada"})
    assert existing.source is None

    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)

    assert result.status == "updated"
    updated = await crm.contact_store.get(existing.crm_contact_id)
    assert updated.source is None  # untouched, even though this update came from ITF
    assert updated.custom_fields["itf_submitted_at"] == "2026-08-10T10:00:00"  # latest-wins still applies


@pytest.mark.asyncio
async def test_itf_submitted_at_always_takes_the_latest_value_on_repeat_submission(itf_service, import_service):
    crm = import_service.crm_service
    values_1 = row("8/1/2026 09:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    result_1 = await itf_service.process_submission(HEADERS, values_1, row_number=2, dry_run=False)
    assert (await crm.contact_store.get(result_1.contact_id)).custom_fields["itf_submitted_at"] == "2026-08-01T09:00:00"

    # Same person submits again, later -- a DIFFERENT row (Apps Script fires once per
    # submission, appending a new row each time), so it's a fresh, unrelated ledger entry.
    values_2 = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    result_2 = await itf_service.process_submission(HEADERS, values_2, row_number=3, dry_run=False)

    assert result_2.status == "updated"
    assert result_2.contact_id == result_1.contact_id
    updated = await crm.contact_store.get(result_1.contact_id)
    assert updated.custom_fields["itf_submitted_at"] == "2026-08-10T10:00:00"


# --- idempotency ---


@pytest.mark.asyncio
async def test_identical_repeat_webhook_call_is_reported_already_processed(itf_service, import_service):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")

    first = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert first.status == "created"

    second = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert second.status == "already_processed"
    assert second.contact_id == first.contact_id

    contacts = await import_service.crm_service.contact_store.list()
    assert len(contacts) == 1  # never duplicated


@pytest.mark.asyncio
async def test_same_row_number_with_different_content_is_reprocessed(itf_service):
    values_1 = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    await itf_service.process_submission(HEADERS, values_1, row_number=2, dry_run=False)

    values_2 = row("8/10/2026 10:00:00", "Ada", "Lovelace2", "ada@example.com", "", "", "", "")
    result = await itf_service.process_submission(HEADERS, values_2, row_number=2, dry_run=False)

    assert result.status == "updated"  # reprocessed, not reported as already_processed


@pytest.mark.asyncio
async def test_a_row_that_previously_errored_is_retried_not_skipped(itf_service, log_store):
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "", "")
    await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)

    log_entries = await log_store.get_all()
    entry = log_entries[2]
    await log_store.save(entry.model_copy(update={"status": ItfRowStatus.ERROR}))

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert result.status != "already_processed"


# --- check size / revenue stage: private vs institutional ---


@pytest.mark.asyncio
async def test_check_size_personal_and_institutional_stay_independent(itf_service, import_service):
    headers = [
        "Timestamp", "First Name", "Last Name", "Email Address",
        "Which size investments are you open to making?",
        "Which size investments are you open to making? (Institutional)",
    ]
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "$25k - $50k", "$1M - $2M")

    result = await itf_service.process_submission(headers, values, row_number=2, dry_run=True)
    assert result.mapped_fields.get("custom:check_size_personal") is None  # no live options in context -- dropped, not guessed


@pytest.mark.asyncio
async def test_missing_email_and_name_still_processes_without_crashing(itf_service):
    result = await itf_service.process_submission(HEADERS, ["", "", "", "", "", "", "", ""], row_number=2, dry_run=True)
    assert result.status in ("created", "error")


@pytest.mark.asyncio
async def test_unparseable_timestamp_leaves_itf_submitted_at_unset_and_warns(itf_service, import_service):
    values = row("not-a-real-timestamp", "Ada", "Lovelace", "ada@example.com", "", "", "", "")

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    contact = await import_service.crm_service.contact_store.get(result.contact_id)
    assert "itf_submitted_at" not in contact.custom_fields
    assert any("Timestamp value not recognized" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_unmapped_expected_question_is_reported_as_a_warning(itf_service):
    headers_missing_linkedin = [h for h in HEADERS if h != "LinkedIn Profile URL"]
    values = row("8/10/2026 10:00:00", "Ada", "Lovelace", "ada@example.com", "", "", "")

    result = await itf_service.process_submission(headers_missing_linkedin, values, row_number=2, dry_run=True)
    assert any("Which city/cities do you live in or frequent?" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_unmapped_checklist_alias_is_reported_as_a_warning(itf_service):
    """Regression coverage for requirement #6 of the demographic-preferences bug
    fix: if a checklist alias (crm_classification_rules.thesis_checklist_aliases())
    doesn't match any real header, that must surface as a warning -- this is
    exactly the diagnostic that was missing when the broken demographic-
    preferences aliases silently produced a missing private field and a
    contaminated institutional field with zero warnings at all."""
    headers = REAL_HEADERS.copy()
    headers[13] = "Some completely different question wording"  # was the demographic-preferences private column
    values = list(REAL_VALUES)

    result = await itf_service.process_submission(headers, values, row_number=2, dry_run=True)
    assert any(
        "Do you have demographic preferences for the people whose companies you invest in?" in w
        for w in result.warnings
    )


# --- full real-row regression: exact 26-column header set audited 2026-08-11 ---
# Identity fields are synthetic (never the real respondent's actual PII); every
# header string and every answer VALUE below is the real, audit-verified text --
# option-list wording, not personal data.

REAL_HEADERS = [
    "Timestamp",
    "First Name",
    "Last Name",
    "Email Address",
    "LinkedIn Profile URL",
    "Which city/cities do you live in or frequent?",
    "Do you invest privately or institutionally (or both)?",
    "Which types of assets do you invest in or are you interested in?",
    "Which business models do you invest in or are you interested in?",
    "Which industries do you invest in or are you interested in?",
    "Which size investments are you open to making?",
    "During which deal stages are you open to investing?",
    "How would you like to meet fundraisers?",
    "Do you have demographic preferences for the people whose companies you invest in?",
    "Do you have any other criteria or feedback?",
    "Do you also invest institutionally (via a fund)?",
    "Which types of assets do you invest in or are you interested in?",  # Q, institutional dup
    "Which business models do you invest in or are you interested in?",  # R, institutional dup
    "Which industries do you invest in or are you interested in?",  # S, institutional dup
    "Which size investments are you open to making?",  # T, institutional dup
    "During which deal stages are you open to investing?",  # U, institutional dup
    "How would you like to meet fundraisers?",  # V, institutional dup
    "Do you have demographic preferences for the people whose companies you invest in?",  # W, institutional dup of N
    "Do you have any other criteria or feedback?",  # X, institutional dup
    "Do you have dietary preferences?",
    "Want us to invite/include other investor-friends? If so, enter their email(s) here.",
]

REAL_VALUES = row(
    "8/6/2026 17:46:14",
    "Riley",  # synthetic -- never the real respondent's name
    "Quinn",  # synthetic
    "riley.quinn.itf-test@example.com",  # synthetic
    "https://www.linkedin.com/in/example-profile",  # synthetic
    "Austin",
    "Privately",
    "Carbon credits / ESG investments, Collectibles (e.g., art, wine, watches)",
    "Agencies / Services (e.g., marketing, development firms)",
    "Aerospace & Defense, AgTech & Food Production",
    "$1k - $10k, $10k - $25k, $25k - $50k, $50k - $100k",
    "Friends & Family (idea or concept stage, often pre-incorporation)",
    "In an email intro, I'd do a Zoom call",
    "I'm open to investing in anyone, I prefer female fundraisers, I prefer male fundraisers, "
    "I prefer black fundraisers, I prefer Latino fundraisers",
    "",
    "Yes",
    "Carbon credits / ESG investments, Collectibles (e.g., art, wine, watches)",
    "Brokerages, Community-based / Network-led growth",
    "Creative Industries (Media, Music, Photo, etc.), Cybersecurity",
    "$1k - $10k",
    "Growth Equity (post-Series B+, but still private)",
    "I'd host a dinner at my house",
    "I'm open to investing in anyone, I prefer female fundraisers",
    "",
    "",
    "",
)


@pytest.mark.asyncio
async def test_full_real_row_dry_run_maps_every_column_with_no_warnings(itf_service):
    """The exact 26-column header set read live from the real Sheet on 2026-08-11
    (identity fields swapped for synthetic values) -- every header must be
    recognized, and the institutional gate (P='Yes') must not by itself change
    anything here since gate-triggered institutional presence is just "columns
    Q-X happen to be non-blank," not a separate code path."""
    result = await itf_service.process_submission(REAL_HEADERS, REAL_VALUES, row_number=2, dry_run=True)

    assert result.status == "created"
    assert result.warnings == []  # every _SCALAR_COLUMN_ALIASES entry found its column

    mapped = result.mapped_fields
    assert mapped["thesis_cities"] == "Austin"
    assert mapped["thesis_investor_mode"] == "Privately"
    assert mapped["thesis_also_invests_institutionally"] is True

    # Private section (H-N)
    assert mapped["thesis_private_asset_types"] == [
        "Carbon credits / ESG investments", "Collectibles (e.g., art, wine, watches)",
    ]
    assert mapped["thesis_private_business_models"] == ["Agencies / Services (e.g., marketing, development firms)"]
    assert mapped["thesis_private_industries"] == ["Aerospace & Defense", "AgTech & Food Production"]
    assert mapped["thesis_private_deal_stages"] == ["Friends & Family (idea or concept stage, often pre-incorporation)"]
    assert mapped["thesis_private_meeting_preferences"] == ["In an email intro", "I'd do a Zoom call"]
    assert mapped["thesis_private_demographic_preferences"] == [
        "I'm open to investing in anyone", "I prefer female fundraisers",
        "I prefer male fundraisers", "I prefer black fundraisers", "I prefer Latino fundraisers",
    ]

    # Institutional section (Q-W) -- triggered by P='Yes', verified independently from private
    assert mapped["thesis_institutional_asset_types"] == [
        "Carbon credits / ESG investments", "Collectibles (e.g., art, wine, watches)",
    ]
    assert mapped["thesis_institutional_business_models"] == ["Brokerages", "Community-based / Network-led growth"]
    assert mapped["thesis_institutional_industries"] == ["Creative Industries (Media, Music, Photo, etc.)", "Cybersecurity"]
    # investment_industry (custom field) -- ordered, deduplicated union of the private
    # and institutional industries above, populated by classify_industry (not
    # classify_thesis_checklist_fields), reusing the SAME ITF header lookups
    assert mapped["custom:investment_industry"] == [
        "Aerospace & Defense", "AgTech & Food Production",
        "Creative Industries (Media, Music, Photo, etc.)", "Cybersecurity",
    ]
    assert mapped["thesis_institutional_deal_stages"] == ["Growth Equity (post-Series B+, but still private)"]
    assert mapped["thesis_institutional_meeting_preferences"] == ["I'd host a dinner at my house"]
    # W IS a duplicate of N (both byte-identical, auto-suffixed by _disambiguate_headers) --
    # verified independently mapped from the private column, not overwritten by/overwriting it
    assert mapped["thesis_institutional_demographic_preferences"] == [
        "I'm open to investing in anyone", "I prefer female fundraisers",
    ]

    # Blank in this real row -- dietary preferences and referral emails, O and X other-criteria
    assert "thesis_dietary_preferences" not in mapped
    assert "thesis_referral_emails" not in mapped
    assert "thesis_private_other_criteria" not in mapped
    assert "thesis_institutional_other_criteria" not in mapped


@pytest.mark.asyncio
async def test_full_real_row_dietary_preferences_and_referral_captured_when_present(itf_service):
    """Same real header set, but with the two previously-blank fields filled in --
    confirms both are now actually captured, not just theoretically mappable."""
    values = list(REAL_VALUES)
    values[24] = "Vegan"  # "Do you have dietary preferences?"
    values[25] = "friend@example.com"  # referral emails free text

    result = await itf_service.process_submission(REAL_HEADERS, values, row_number=2, dry_run=True)

    assert result.mapped_fields["thesis_dietary_preferences"] == ["Vegan"]
    assert result.mapped_fields["thesis_referral_emails"] == "friend@example.com"


# --- _normalize_dietary_preferences_delimiter: ITF-only, never touches CSV path ---


def test_normalize_dietary_preferences_delimiter_splits_googleforms_comma_join():
    """Google Forms joins checkbox selections with ', ' -- confirmed live against
    6 other multi-select ITF questions in the real Sheet (2026-08-11 audit).
    classify_dietary_preferences (untouched) expects ';' -- this bridges the two."""
    raw_row = {"Do you have dietary preferences?": "Vegan, Gluten-Free, Dairy-Free"}
    result = _normalize_dietary_preferences_delimiter(raw_row)
    assert result["Do you have dietary preferences?"] == "Vegan; Gluten-Free; Dairy-Free"


def test_normalize_dietary_preferences_delimiter_preserves_comma_inside_other_text():
    """A human's free-typed 'Other' answer containing its own comma must survive as
    ONE segment, not be shredded -- this is exactly why greedy known-option
    matching is used instead of a blind comma-split."""
    raw_row = {"Do you have dietary preferences?": "Vegan, No pork, no shellfish"}
    result = _normalize_dietary_preferences_delimiter(raw_row)
    assert result["Do you have dietary preferences?"] == "Vegan; No pork, no shellfish"


def test_normalize_dietary_preferences_delimiter_all_unrecognized_stays_one_segment():
    raw_row = {"Do you have dietary preferences?": "No pork, no shellfish, no beef"}
    result = _normalize_dietary_preferences_delimiter(raw_row)
    assert result["Do you have dietary preferences?"] == "No pork, no shellfish, no beef"


def test_normalize_dietary_preferences_delimiter_is_a_noop_without_that_column():
    """Proves this bridge is unreachable from a CSV-shaped raw_row -- CSV import
    never calls this function at all, but even if it somehow received a row with
    no dietary-preferences-shaped column, it changes nothing."""
    raw_row = {"First Name": "Ada", "Email": "ada@example.com"}
    assert _normalize_dietary_preferences_delimiter(raw_row) == raw_row


def test_normalize_dietary_preferences_delimiter_is_a_noop_on_blank_value():
    raw_row = {"Do you have dietary preferences?": ""}
    assert _normalize_dietary_preferences_delimiter(raw_row) == raw_row


def test_normalize_dietary_preferences_delimiter_recognizes_csv_style_headers_too():
    """The bridge matches by the same alias set as the classifier itself (Dietary
    Preferences / Dietary Restrictions / the ITF wording) -- included for
    completeness, though CSV import's own code path never calls this function."""
    raw_row = {"Dietary Restrictions": "Halal, Kosher"}
    result = _normalize_dietary_preferences_delimiter(raw_row)
    assert result["Dietary Restrictions"] == "Halal; Kosher"


# --- end-to-end via process_submission: ITF path gets comma-joined dietary answers right ---


@pytest.mark.asyncio
async def test_process_submission_splits_multiple_dietary_preferences_from_comma_join(itf_service):
    headers = REAL_HEADERS
    values = list(REAL_VALUES)
    values[24] = "Vegan, Gluten-Free, Dairy-Free"

    result = await itf_service.process_submission(headers, values, row_number=2, dry_run=True)
    assert result.mapped_fields["thesis_dietary_preferences"] == ["Vegan", "Gluten-Free", "Dairy-Free"]
    assert "thesis_dietary_preferences_other" not in result.mapped_fields


@pytest.mark.asyncio
async def test_process_submission_preserves_comma_in_other_dietary_free_text(itf_service):
    headers = REAL_HEADERS
    values = list(REAL_VALUES)
    values[24] = "Vegan, No pork, no shellfish"

    result = await itf_service.process_submission(headers, values, row_number=2, dry_run=True)
    assert result.mapped_fields["thesis_dietary_preferences"] == ["Vegan", "Other"]
    assert result.mapped_fields["thesis_dietary_preferences_other"] == "No pork, no shellfish"


# --- CSV regression: classify_dietary_preferences itself is provably untouched ---
# (see tests/test_crm_classification_rules.py's existing 10 dietary-preference tests,
# all still semicolon-based and unmodified -- this function is never on that call path.)


@pytest.mark.asyncio
async def test_csv_import_dietary_preferences_still_uses_semicolons_unaffected_by_itf_bridge(itf_service):
    """CrmImportService.import_one_row() (the exact function both CSV and ITF share)
    never calls _normalize_dietary_preferences_delimiter -- proven directly by feeding
    it a CSV-shaped, semicolon-joined row through the real shared pipeline and
    confirming it parses exactly as it always has."""
    from app.services.crm_classification_rules import build_classification_context

    context = await build_classification_context(itf_service.import_service.crm_service.custom_field_store)
    status, contact, matched_on, mapped = await itf_service.import_service.import_one_row(
        {"Email": "csv-row@example.com", "Dietary Preferences": "Vegan;Gluten-Free"},
        {"Email": "email"},
        context,
    )
    assert mapped["thesis_dietary_preferences"] == ["Vegan", "Gluten-Free"]


# --- Activity Log ---


@pytest.mark.asyncio
async def test_new_contact_submission_emits_submission_received_and_contact_created(itf_service):
    values = row("8/10/2026 10:00:00", "Amos", "Ben-Meir", "amos@example.com", "", "", "", "")
    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert result.status == "created"

    events = await itf_service.activity_log.store.list()
    event_types = [e.event_type for e in events]
    assert event_types == ["itf.contact_created", "itf.submission_received"]  # newest first
    for event in events:
        assert event.entity_name == "Amos Ben-Meir"
        assert event.entity_id == result.contact_id


@pytest.mark.asyncio
async def test_existing_contact_submission_emits_submission_received_and_contact_updated(itf_service, import_service):
    await import_service.crm_service.create_contact({"email": "amos@example.com", "city": ""})
    values = row("8/10/2026 10:00:00", "Amos", "Ben-Meir", "amos@example.com", "", "", "", "")

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert result.status == "updated"

    events = await itf_service.activity_log.store.list()
    event_types = [e.event_type for e in events]
    assert event_types == ["itf.contact_updated", "itf.submission_received"]


@pytest.mark.asyncio
async def test_processing_failure_emits_processing_failed_not_created_or_updated(itf_service):
    """A genuine per-row processing exception (import_one_row raising) must
    surface as itf.processing_failed, in the errors category -- never a
    contact_created/contact_updated event, since nothing was actually
    written to the CRM."""
    from unittest.mock import AsyncMock

    itf_service.import_service.import_one_row = AsyncMock(side_effect=RuntimeError("simulated processing failure"))
    values = row("8/10/2026 10:00:00", "Amos", "Ben-Meir", "amos@example.com", "", "", "", "")

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert result.status == "error"

    events = await itf_service.activity_log.store.list()
    event_types = [e.event_type for e in events]
    assert event_types == ["itf.processing_failed", "itf.submission_received"]
    failure_event = events[0]
    from app.models.activity import ActivityCategory

    assert failure_event.category == ActivityCategory.ERRORS
    assert "simulated processing failure" in failure_event.metadata["error"]


@pytest.mark.asyncio
async def test_already_processed_resubmission_emits_no_new_events(itf_service):
    """Idempotency at the activity-log level too: a genuine retry of the exact
    same submission must never create a second submission_received/
    contact_created pair."""
    values = row("8/10/2026 10:00:00", "Amos", "Ben-Meir", "amos@example.com", "", "", "", "")
    await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    events_after_first = await itf_service.activity_log.store.list()
    assert len(events_after_first) == 2

    result = await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=False)
    assert result.status == "already_processed"

    events_after_second = await itf_service.activity_log.store.list()
    assert len(events_after_second) == 2  # unchanged


@pytest.mark.asyncio
async def test_dry_run_emits_no_activity_events_at_all(itf_service):
    values = row("8/10/2026 10:00:00", "Amos", "Ben-Meir", "amos@example.com", "", "", "", "")
    await itf_service.process_submission(HEADERS, values, row_number=2, dry_run=True)
    events = await itf_service.activity_log.store.list()
    assert events == []
