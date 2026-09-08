"""
Pure-logic tests for app/services/luma_contact_enrichment.py -- the shared
Luma self-report Company/Job Title resolution/merge rules and the Company
Website resolver. No store/service wiring here -- see
tests/test_luma_sync_service.py for the live-webhook integration tests and
tests/test_luma_contact_enrichment_backfill.py for the historical driver.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaEvent, LumaRegistration, LumaRegistrationAnswer
from app.services.luma_contact_enrichment import (
    FIELD_PROVENANCE_KEY,
    RecencyTier,
    Tier1Result,
    apply_luma_self_report,
    canonical_website_url,
    compute_registration_recency,
    extract_company_question_answer,
    extract_email_domain,
    is_free_email_domain,
    normalize_company_name,
    normalize_website_domain,
    resolve_contact_luma_fields,
    resolve_field,
    resolve_tier1_website,
    resolve_tier2_email_domain_candidate,
    resolved_company_if_material_change,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def make_event(**overrides) -> LumaEvent:
    defaults = dict(luma_event_id=str(uuid.uuid4()), name="Test Event", synced_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return LumaEvent(**defaults)


def make_registration(**overrides) -> LumaRegistration:
    defaults = dict(
        luma_guest_id=str(uuid.uuid4()),
        luma_event_id="e1",
        approval_status=LumaApprovalStatus.APPROVED,
        synced_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


def company_answer(company: str | None = None, job_title: str | None = None, label: str = "Company") -> LumaRegistrationAnswer:
    return LumaRegistrationAnswer(question_id="q1", label=label, question_type="company", value={"company": company, "job_title": job_title})


# --- structural extraction, independent of label ----------------------------


def test_extraction_is_structural_not_label_based():
    """The organizer could name this question anything -- extraction only
    ever looks at question_type == "company"."""
    reg = make_registration(registration_answers=[company_answer("Acme", "CEO", label="Where do you work and what's your role?")])
    company, title = extract_company_question_answer(reg)
    assert company == "Acme"
    assert title == "CEO"


def test_extraction_ignores_a_non_company_question_type_even_with_a_similar_label():
    reg = make_registration(
        registration_answers=[
            LumaRegistrationAnswer(question_id="q1", label="Company", question_type="text", value="Acme")
        ]
    )
    company, title = extract_company_question_answer(reg)
    assert company is None
    assert title is None


def test_extraction_handles_missing_or_blank_sub_keys():
    reg = make_registration(registration_answers=[company_answer(company="Acme", job_title=None)])
    company, title = extract_company_question_answer(reg)
    assert company == "Acme"
    assert title is None

    reg2 = make_registration(registration_answers=[company_answer(company="  ", job_title="  ")])
    company2, title2 = extract_company_question_answer(reg2)
    assert company2 is None  # whitespace-only treated as blank
    assert title2 is None


def test_extraction_defensively_ignores_a_non_dict_value():
    reg = make_registration(
        registration_answers=[LumaRegistrationAnswer(question_id="q1", label="Company", question_type="company", value="not a dict")]
    )
    assert extract_company_question_answer(reg) == (None, None)


def test_extraction_takes_the_first_company_question_type_answer_when_multiple_exist():
    reg = make_registration(
        registration_answers=[
            company_answer(company="First", job_title="A"),
            company_answer(company="Second", job_title="B"),
        ]
    )
    assert extract_company_question_answer(reg) == ("First", "A")


# --- recency ------------------------------------------------------------------


def test_recency_prefers_registered_at():
    t1, t2 = _now(), _now() + timedelta(days=1)
    reg = make_registration(registered_at=t1, joined_at=t2)
    recency = compute_registration_recency(reg, event=make_event(start_at=t2))
    assert recency.tier == RecencyTier.REGISTERED_AT
    assert recency.recency_at == t1


def test_recency_falls_back_to_joined_at_when_registered_at_missing():
    t2 = _now()
    reg = make_registration(registered_at=None, joined_at=t2)
    recency = compute_registration_recency(reg, event=make_event(start_at=_now()))
    assert recency.tier == RecencyTier.JOINED_AT
    assert recency.recency_at == t2


def test_recency_falls_back_to_event_start_at_when_registration_timestamps_missing():
    start = _now()
    reg = make_registration(registered_at=None, joined_at=None)
    recency = compute_registration_recency(reg, event=make_event(start_at=start))
    assert recency.tier == RecencyTier.EVENT_START_AT
    assert recency.recency_at == start


def test_recency_is_unknown_when_nothing_real_world_is_available():
    reg = make_registration(registered_at=None, joined_at=None)
    recency = compute_registration_recency(reg, event=make_event(start_at=None))
    assert recency.tier == RecencyTier.UNKNOWN
    assert recency.recency_at is None

    recency_no_event = compute_registration_recency(reg, event=None)
    assert recency_no_event.tier == RecencyTier.UNKNOWN


def test_recency_never_uses_updated_at_or_synced_at_as_a_fallback():
    """registration.updated_at/synced_at are AstroHub's own persistence
    timestamps -- deliberately never consulted, even when they're the only
    thing set, since a backfill/reconciliation run stamps them to "now"."""
    reg = make_registration(registered_at=None, joined_at=None, updated_at=_now(), synced_at=_now())
    recency = compute_registration_recency(reg, event=make_event(start_at=None))
    assert recency.tier == RecencyTier.UNKNOWN


# --- field resolution: company/title independence, blank never erases ------


def test_company_and_title_resolve_independently():
    """A newer registration with a blank Job Title but filled Company must
    not block an OLDER registration's valid Job Title from winning."""
    older = make_registration(luma_guest_id="g-old", registered_at=_now() - timedelta(days=10), registration_answers=[company_answer("OldCo", "Analyst")])
    newer = make_registration(luma_guest_id="g-new", registered_at=_now(), registration_answers=[company_answer("NewCo", None)])
    resolutions = resolve_contact_luma_fields([older, newer], {})
    assert resolutions.company.resolved.value == "NewCo"  # newer wins for company
    assert resolutions.title.resolved.value == "Analyst"  # older still wins for title (only valid answer)


def test_resolve_field_returns_none_for_no_candidates():
    resolution = resolve_field([])
    assert resolution.resolved is None
    assert resolution.ambiguous_unknown is False
    assert resolution.candidate_count == 0


def test_blank_or_missing_answers_never_produce_a_candidate():
    reg = make_registration(registration_answers=[company_answer(company=None, job_title=None)])
    resolutions = resolve_contact_luma_fields([reg], {})
    assert resolutions.company.resolved is None
    assert resolutions.title.resolved is None


# --- recency-based resolution: newer known beats older known ---------------


def test_newer_known_recency_beats_older_known_recency():
    older = make_registration(luma_guest_id="g1", registered_at=_now() - timedelta(days=5), registration_answers=[company_answer("OldCo")])
    newer = make_registration(luma_guest_id="g2", registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([older, newer], {})
    assert resolutions.company.resolved.value == "NewCo"
    assert resolutions.company.resolved.luma_guest_id == "g2"


def test_older_late_arriving_registration_cannot_regress_a_newer_value():
    """Simulates the exact regression scenario: BOTH registrations are
    already stored (the "late webhook" has since arrived and been saved),
    and resolution is recomputed from the complete set -- order of
    construction/iteration must not matter."""
    newer = make_registration(luma_guest_id="g-newer", registered_at=_now(), registration_answers=[company_answer("NewCo")])
    older_late = make_registration(luma_guest_id="g-older", registered_at=_now() - timedelta(days=30), registration_answers=[company_answer("VeryOldCo")])
    resolutions_a = resolve_contact_luma_fields([newer, older_late], {})
    resolutions_b = resolve_contact_luma_fields([older_late, newer], {})  # reversed order
    assert resolutions_a.company.resolved.value == "NewCo"
    assert resolutions_b.company.resolved.value == "NewCo"


# --- unknown recency: single adopted, known always wins, ambiguous fails ----


def test_a_single_unknown_recency_candidate_is_adopted():
    reg = make_registration(registered_at=None, joined_at=None, registration_answers=[company_answer("OnlyCo")])
    resolutions = resolve_contact_luma_fields([reg], {})  # no event -> EVENT_START_AT unavailable too
    assert resolutions.company.resolved.value == "OnlyCo"
    assert resolutions.company.resolved.tier == RecencyTier.UNKNOWN
    assert resolutions.company.ambiguous_unknown is False


def test_known_recency_always_beats_unknown_recency_regardless_of_which_arrived():
    known = make_registration(luma_guest_id="g-known", registered_at=_now(), registration_answers=[company_answer("KnownCo")])
    unknown = make_registration(luma_guest_id="g-unknown", registered_at=None, joined_at=None, registration_answers=[company_answer("UnknownCo")])
    assert resolve_contact_luma_fields([known, unknown], {}).company.resolved.value == "KnownCo"
    assert resolve_contact_luma_fields([unknown, known], {}).company.resolved.value == "KnownCo"


def test_two_or_more_unknown_recency_candidates_fail_conservative():
    """No deterministic real-world ordering exists between two
    unknown-recency answers -- must not guess."""
    u1 = make_registration(luma_guest_id="g1", registered_at=None, joined_at=None, registration_answers=[company_answer("Co1")])
    u2 = make_registration(luma_guest_id="g2", registered_at=None, joined_at=None, registration_answers=[company_answer("Co2")])
    resolution = resolve_contact_luma_fields([u1, u2], {}).company
    assert resolution.resolved is None
    assert resolution.ambiguous_unknown is True
    assert resolution.candidate_count == 2


def test_ambiguous_unknown_history_only_affects_the_field_it_actually_conflicts_on():
    u1 = make_registration(luma_guest_id="g1", registered_at=None, joined_at=None, registration_answers=[company_answer("Co1", "Title1")])
    u2 = make_registration(luma_guest_id="g2", registered_at=None, joined_at=None, registration_answers=[company_answer("Co2", None)])
    resolutions = resolve_contact_luma_fields([u1, u2], {})
    assert resolutions.company.ambiguous_unknown is True  # two conflicting unknown companies
    assert resolutions.title.ambiguous_unknown is False  # only one valid title answer at all
    assert resolutions.title.resolved.value == "Title1"


# --- deterministic tie behavior for equal known timestamps ------------------


def test_equal_known_recency_is_broken_deterministically_by_smallest_guest_id():
    t = _now()
    a = make_registration(luma_guest_id="zzz-guest", registered_at=t, registration_answers=[company_answer("CoZ")])
    b = make_registration(luma_guest_id="aaa-guest", registered_at=t, registration_answers=[company_answer("CoA")])
    resolved_ab = resolve_contact_luma_fields([a, b], {}).company.resolved
    resolved_ba = resolve_contact_luma_fields([b, a], {}).company.resolved
    assert resolved_ab.value == "CoA"  # smallest guest_id wins, regardless of list order
    assert resolved_ba.value == "CoA"
    assert resolved_ab.luma_guest_id == "aaa-guest"


def test_tie_break_is_stable_and_reruns_produce_the_identical_result():
    t = _now()
    regs = [
        make_registration(luma_guest_id="m-guest", registered_at=t, registration_answers=[company_answer("CoM")]),
        make_registration(luma_guest_id="a-guest", registered_at=t, registration_answers=[company_answer("CoA")]),
        make_registration(luma_guest_id="z-guest", registered_at=t, registration_answers=[company_answer("CoZ")]),
    ]
    first = resolve_contact_luma_fields(regs, {}).company.resolved.value
    second = resolve_contact_luma_fields(list(reversed(regs)), {}).company.resolved.value
    assert first == second == "CoA"


# --- company-name normalization: comparison only, never rewrites storage ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Inc.", "acme"),
        ("Acme, LLC", "acme"),
        ("ACME INC", "acme"),
        ("Acme Consulting Co", "acme consulting"),
        ("Acme Ventures", "acme ventures"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_company_name(raw, expected):
    assert normalize_company_name(raw) == expected


def test_company_normalization_never_rewrites_the_stored_self_reported_value():
    """The comparison normalizer is used ONLY to decide "same company" for
    the website-review-needed signal/Tier-1 grouping -- .company itself
    always keeps the self-reported text verbatim, casing/punctuation
    included."""
    contact = make_contact(company="Acme Inc.", company_website=None)
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("ACME INC")])  # cosmetically different, same normalized company
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert outcome.contact.company == "ACME INC"  # stored verbatim, not normalized
    assert outcome.website_review_needed is False  # cosmetic difference only -- not a material change


# --- website domain normalization --------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://acme.com", "acme.com"),
        ("http://www.acme.com/", "acme.com"),
        ("acme.com", "acme.com"),
        ("www.acme.com", "acme.com"),
        ("https://acme.com/about", "acme.com"),
        ("https://ACME.com", "acme.com"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_website_domain(raw, expected):
    assert normalize_website_domain(raw) == expected


def test_canonical_website_url_is_scheme_and_path_normalized():
    assert canonical_website_url("http://www.acme.com/careers") == "https://acme.com"
    assert canonical_website_url(None) is None


# --- Tier 1: internal same-company knowledge --------------------------------


def test_tier1_unique_resolution():
    contacts = [
        make_contact(company="Acme Inc.", company_website="https://www.acme.com/"),
        make_contact(company="Acme", company_website="acme.com"),  # same normalized domain, different formatting
        make_contact(company="Totally Different Co", company_website="other.com"),
    ]
    result = resolve_tier1_website(normalize_company_name("Acme"), contacts)
    assert result.candidate == "https://acme.com"
    assert result.ambiguous is False


def test_tier1_ambiguous_when_matching_companies_disagree():
    contacts = [
        make_contact(company="Acme", company_website="acme.com"),
        make_contact(company="Acme Inc.", company_website="acme-other.io"),
    ]
    result = resolve_tier1_website(normalize_company_name("Acme"), contacts)
    assert result.candidate is None
    assert result.ambiguous is True


def test_tier1_no_match_is_not_ambiguous():
    contacts = [make_contact(company="Somewhere Else", company_website="else.com")]
    result = resolve_tier1_website(normalize_company_name("Acme"), contacts)
    assert result.candidate is None
    assert result.ambiguous is False
    assert result.matching_contact_count == 0


def test_tier1_excludes_archived_contacts_and_the_contact_itself():
    target = make_contact(company="Acme", company_website="acme.com")
    contacts = [
        target,
        make_contact(company="Acme", company_website="acme.com", archived=True),  # archived, excluded
    ]
    result = resolve_tier1_website(normalize_company_name("Acme"), contacts, exclude_crm_contact_id=target.crm_contact_id)
    assert result.candidate is None  # only match was excluded (self) or archived
    assert result.matching_contact_count == 0


# --- Tier 2: corporate email domain, free-provider exclusion ----------------


def test_tier2_corporate_domain_candidate():
    result = resolve_tier2_email_domain_candidate("jane@acme.com")
    assert result.candidate == "https://acme.com"
    assert result.excluded_free_domain is None


@pytest.mark.parametrize("domain", ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "protonmail.com"])
def test_tier2_excludes_free_email_domains(domain):
    result = resolve_tier2_email_domain_candidate(f"jane@{domain}")
    assert result.candidate is None
    assert result.excluded_free_domain == domain


def test_tier2_returns_nothing_for_a_blank_or_malformed_email():
    assert resolve_tier2_email_domain_candidate(None).candidate is None
    assert resolve_tier2_email_domain_candidate("not-an-email").candidate is None


def test_extract_email_domain_and_is_free_email_domain():
    assert extract_email_domain("Jane@Acme.COM") == "acme.com"
    assert extract_email_domain(None) is None
    assert is_free_email_domain("gmail.com") is True
    assert is_free_email_domain("acme.com") is False


# --- merge: blank never erases, nonblank always replaces --------------------


def test_blank_luma_answer_never_erases_an_existing_contact_value():
    contact = make_contact(company="Existing Co", title="Existing Title")
    reg = make_registration(registration_answers=[company_answer(company=None, job_title=None)])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert outcome.contact.company == "Existing Co"
    assert outcome.contact.title == "Existing Title"
    assert outcome.changed_field_keys == []


def test_nonblank_luma_company_replaces_an_existing_different_value():
    """The key behavioral difference from apply_import_mapping()'s
    fill-only rule -- this is the whole point of the feature."""
    contact = make_contact(company="OldCo", title="Old Title")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo", "New Title")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert outcome.contact.company == "NewCo"
    assert outcome.contact.title == "New Title"
    assert "company" in outcome.changed_field_keys
    assert "title" in outcome.changed_field_keys


def test_ambiguous_unknown_field_is_left_untouched_and_flagged():
    contact = make_contact(company="Existing Co")
    u1 = make_registration(luma_guest_id="g1", registered_at=None, joined_at=None, registration_answers=[company_answer("Co1")])
    u2 = make_registration(luma_guest_id="g2", registered_at=None, joined_at=None, registration_answers=[company_answer("Co2")])
    resolutions = resolve_contact_luma_fields([u1, u2], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert outcome.contact.company == "Existing Co"  # untouched, not guessed
    assert "company" not in outcome.changed_field_keys
    assert outcome.ambiguous_fields == ["company"]


def test_no_change_produces_zero_updates_and_zero_provenance_write():
    """We have no way to know the historical source of an already-
    identical value -- provenance must mean "this enrichment actually
    set/changed this field from this registration", never "Luma
    currently contains the same value the CRM already had"."""
    contact = make_contact(company="SameCo")
    reg = make_registration(registered_at=_now(), luma_guest_id="g1", registration_answers=[company_answer("SameCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert outcome.changed_field_keys == []
    assert outcome.contact is contact  # truly untouched, not even a fresh model_copy
    assert FIELD_PROVENANCE_KEY not in outcome.contact.custom_fields


def test_company_change_does_not_newly_acquire_title_provenance_when_title_is_already_identical():
    """Independent per field: Company changing must never cause Title to
    acquire provenance it wouldn't otherwise have earned on its own."""
    contact = make_contact(company="OldCo", title="SameTitle")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo", "SameTitle")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    assert "company" in outcome.changed_field_keys
    assert "title" not in outcome.changed_field_keys
    provenance = outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]
    assert "company" in provenance
    assert "title" not in provenance


def test_provenance_records_source_guest_id_and_recency():
    contact = make_contact()
    t = _now()
    reg = make_registration(luma_guest_id="gst-123", registered_at=t, registration_answers=[company_answer("Acme", "CEO")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions)
    provenance = outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]
    assert provenance["company"]["source"] == "luma_self_report"
    assert provenance["company"]["luma_guest_id"] == "gst-123"
    assert provenance["company"]["recency_tier"] == "registered_at"
    assert provenance["company"]["recency_at"] == t.isoformat()
    assert provenance["title"]["source"] == "luma_self_report"


def test_provenance_never_carries_raw_payload_or_email():
    contact = make_contact(email="jane@acme.com")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("Acme", "CEO")])
    outcome = apply_luma_self_report(contact, resolve_contact_luma_fields([reg], {}))
    provenance_json = str(outcome.contact.custom_fields[FIELD_PROVENANCE_KEY])
    assert "jane@acme.com" not in provenance_json
    assert "registration_answers" not in provenance_json


# --- V1: existing nonblank website is NEVER auto-cleared/overwritten -------


def test_material_company_change_preserves_the_existing_website_and_flags_for_review():
    """The corrected V1 behavior -- no destructive clearing. An existing
    website is left EXACTLY as it was; the outcome is flagged instead of
    acted on."""
    contact = make_contact(company="OldCo", company_website="oldco.com")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=Tier1Result(candidate="https://should-not-apply.com", ambiguous=False, matching_contact_count=1))
    assert outcome.contact.company == "NewCo"
    assert outcome.contact.company_website == "oldco.com"  # untouched, not cleared, not overwritten by Tier 1 either
    assert outcome.website_review_needed is True
    assert "company_website" not in outcome.changed_field_keys


def test_material_company_change_is_ignored_by_apply_when_tier1_result_is_not_even_provided():
    """A caller correctly following the "only fetch Tier 1 when website is
    blank" guidance never passes a tier1_result at all when the website
    is already set -- confirms nothing breaks/attempts to use it either
    way."""
    contact = make_contact(company="OldCo", company_website="oldco.com")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=None)
    assert outcome.contact.company_website == "oldco.com"
    assert outcome.website_review_needed is True


def test_no_website_review_flag_on_a_merely_cosmetic_company_difference():
    contact = make_contact(company="Acme Inc.", company_website="acme.com")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("ACME, LLC")])  # same normalized company
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=Tier1Result(candidate="https://should-not-be-used.com", ambiguous=False, matching_contact_count=1))
    assert outcome.contact.company == "ACME, LLC"  # self-report still applied verbatim
    assert outcome.contact.company_website == "acme.com"  # untouched -- no material change detected
    assert outcome.website_review_needed is False


# --- Tier 1 may ONLY populate a CURRENTLY BLANK website ---------------------


def test_tier1_populates_a_blank_website_regardless_of_whether_company_changed():
    contact = make_contact(company="OldCo", company_website=None)
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    tier1 = Tier1Result(candidate="https://newco.com", ambiguous=False, matching_contact_count=2)
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=tier1)
    assert outcome.contact.company_website == "https://newco.com"
    assert outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]["company_website"]["source"] == "internal_company_match"
    assert outcome.website_review_needed is False  # nothing to flag -- was blank, not overwritten


def test_tier1_can_populate_a_blank_website_on_a_first_time_company_fill_too():
    """Blank company -> filled is not a "change" that flags anything for
    review (nothing stale existed), but it's still a valid moment for
    Tier 1 to fill an ALSO-blank website -- this is a new, safe capability
    the corrected design enables (previously excluded entirely)."""
    contact = make_contact(company=None, company_website=None)
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    tier1 = Tier1Result(candidate="https://newco.com", ambiguous=False, matching_contact_count=1)
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=tier1)
    assert outcome.contact.company == "NewCo"
    assert outcome.contact.company_website == "https://newco.com"
    assert outcome.website_review_needed is False


def test_tier1_ambiguous_leaves_a_blank_website_honestly_blank():
    contact = make_contact(company="OldCo", company_website=None)
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    tier1 = Tier1Result(candidate=None, ambiguous=True, matching_contact_count=2)
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=tier1)
    assert outcome.contact.company_website is None
    assert outcome.website_tier1_ambiguous is True


def test_tier1_does_not_populate_an_already_blank_website_when_company_did_not_change_at_all():
    """Website stays blank when the resolver found nothing, even though
    Luma reconfirmed the same company (not a bug -- Tier 1 legitimately
    found no unambiguous match)."""
    contact = make_contact(company="SameCo", company_website=None)
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("SameCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=Tier1Result(candidate=None, ambiguous=False, matching_contact_count=0))
    assert outcome.contact.company_website is None
    assert outcome.changed_field_keys == []  # company unchanged (identical value) and website found nothing -- truly nothing to do


def test_resolved_company_if_material_change_helper():
    contact = make_contact(company="OldCo")
    reg = make_registration(registered_at=_now(), registration_answers=[company_answer("NewCo")])
    resolutions = resolve_contact_luma_fields([reg], {})
    assert resolved_company_if_material_change(contact, resolutions) == "NewCo"

    same_reg = make_registration(registered_at=_now(), registration_answers=[company_answer("OldCo")])
    assert resolved_company_if_material_change(contact, resolve_contact_luma_fields([same_reg], {})) is None

    blank_contact = make_contact(company=None)
    assert resolved_company_if_material_change(blank_contact, resolutions) is None


# --- title never triggers website logic -------------------------------------


def test_title_only_change_never_touches_an_existing_company_website():
    contact = make_contact(company="SameCo", company_website="sameco.com", title="Old Title")
    reg = make_registration(registered_at=_now(), luma_guest_id="g1", registration_answers=[company_answer("SameCo", "New Title")])
    resolutions = resolve_contact_luma_fields([reg], {})
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=Tier1Result(candidate="https://should-not-apply.com", ambiguous=False, matching_contact_count=1))
    assert outcome.contact.title == "New Title"
    assert outcome.contact.company_website == "sameco.com"  # untouched
    assert outcome.website_review_needed is False


def test_title_only_change_can_still_populate_a_blank_website_via_tier1():
    """Website handling is gated on "Luma resolved SOME company", not on
    "company's own value changed" -- Title-only changes still let a
    currently-blank website get filled if Tier 1 finds an unambiguous
    match for the (unchanged) company."""
    contact = make_contact(company="SameCo", company_website=None, title="Old Title")
    reg = make_registration(registered_at=_now(), luma_guest_id="g1", registration_answers=[company_answer("SameCo", "New Title")])
    resolutions = resolve_contact_luma_fields([reg], {})
    tier1 = Tier1Result(candidate="https://sameco.com", ambiguous=False, matching_contact_count=1)
    outcome = apply_luma_self_report(contact, resolutions, tier1_result=tier1)
    assert outcome.contact.title == "New Title"
    assert outcome.contact.company_website == "https://sameco.com"
    assert "company" not in outcome.changed_field_keys  # company itself never changed
