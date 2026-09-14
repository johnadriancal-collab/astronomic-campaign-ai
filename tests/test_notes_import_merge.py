"""
Stage NP-2A: tests for merge_notes_import() (app/models/crm.py) -- the
canonical Contact `notes` import merge semantics. classify_notes
(crm_classification_rules.py) decides WHAT a CSV row contributes;
merge_notes_import decides how that contribution relates to whatever a
Contact already has. See both modules' own docstrings for the split.
"""

from app.models.crm import (
    NOTES_IMPORT_MERGE_HEADER,
    NOTES_PERSONAL_NOTES_MERGE_HEADER,
    merge_notes_import,
)


# --- existing blank ------------------------------------------------------------------


def test_existing_blank_notes_only_source_populates_verbatim():
    result = merge_notes_import(None, "General notes here", source_is_personal_notes_only=False)
    assert result == "General notes here"


def test_existing_blank_personal_notes_only_source_populates_verbatim_no_header():
    result = merge_notes_import(None, "Family office context", source_is_personal_notes_only=True)
    assert result == "Family office context"
    assert NOTES_PERSONAL_NOTES_MERGE_HEADER not in result


def test_existing_empty_string_treated_same_as_none():
    result = merge_notes_import("", "New content", source_is_personal_notes_only=False)
    assert result == "New content"


# --- existing populated, identical incoming -------------------------------------------


def test_existing_populated_identical_incoming_notes_is_a_no_op():
    result = merge_notes_import("Angel investor, Austin-based", "Angel investor, Austin-based", source_is_personal_notes_only=False)
    assert result == "Angel investor, Austin-based"


def test_existing_populated_identical_incoming_personal_notes_is_a_no_op():
    result = merge_notes_import("Friends of David Buttross", "friends of   David Buttross", source_is_personal_notes_only=True)
    assert result == "Friends of David Buttross"  # unchanged, original casing/whitespace preserved


def test_identical_comparison_is_whitespace_and_case_insensitive():
    result = merge_notes_import("Legit - Chris", "  legit -   CHRIS  ", source_is_personal_notes_only=False)
    assert result == "Legit - Chris"


# --- incoming already contained in existing --------------------------------------------


def test_incoming_already_contained_in_existing_is_a_no_op():
    existing = "LP in Liveoak Venture Partners here in Austin as well as Alumni Ventures."
    result = merge_notes_import(existing, "LP in Liveoak Venture Partners", source_is_personal_notes_only=True)
    assert result == existing


def test_containment_check_is_case_insensitive():
    existing = "She is Barbie Bowen's wife. Also illiquid."
    result = merge_notes_import(existing, "barbie bowen's wife", source_is_personal_notes_only=True)
    assert result == existing


# --- existing populated, genuinely distinct incoming -- append -------------------------


def test_distinct_personal_notes_source_appends_with_personal_notes_header():
    result = merge_notes_import("Prefers to invest in tech - Chris", "Friends of David Buttross", source_is_personal_notes_only=True)
    assert result == f"Prefers to invest in tech - Chris\n\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\nFriends of David Buttross"


def test_distinct_notes_source_appends_with_notes_import_header():
    result = merge_notes_import("Original note", "New note from later import", source_is_personal_notes_only=False)
    assert result == f"Original note\n\n{NOTES_IMPORT_MERGE_HEADER}\nNew note from later import"


def test_distinct_combined_notes_and_personal_notes_source_still_uses_notes_import_header():
    """When classify_notes has already combined a row's own distinct Notes+Personal
    Notes into one canonical value, that combined value is NOT personal-notes-only
    (the row's Notes column was populated too) -- appending it onto an existing
    Contact's notes uses the generic import header, not the personal-notes one."""
    already_canonicalized_incoming = f"New CSV notes\n\n{NOTES_PERSONAL_NOTES_MERGE_HEADER}\nNew CSV personal notes"
    result = merge_notes_import("Original note", already_canonicalized_incoming, source_is_personal_notes_only=False)
    assert result == f"Original note\n\n{NOTES_IMPORT_MERGE_HEADER}\n{already_canonicalized_incoming}"


def test_append_preserves_existing_notes_byte_for_byte_before_header():
    existing = "  Weirdly-spaced existing note  \nwith an internal newline"
    result = merge_notes_import(existing, "Something new", source_is_personal_notes_only=False)
    assert result.startswith(existing)
    assert result == f"{existing}\n\n{NOTES_IMPORT_MERGE_HEADER}\nSomething new"


# --- idempotency: re-merging the same incoming value is a no-op ------------------------


def test_repeated_merge_of_the_same_incoming_value_appends_only_once():
    existing = "Original note"
    once = merge_notes_import(existing, "New note", source_is_personal_notes_only=False)
    twice = merge_notes_import(once, "New note", source_is_personal_notes_only=False)
    assert once == twice
    assert once.count("New note") == 1


# --- blank incoming --------------------------------------------------------------------


def test_blank_incoming_leaves_existing_untouched():
    result = merge_notes_import("Original note", "   ", source_is_personal_notes_only=False)
    assert result == "Original note"
