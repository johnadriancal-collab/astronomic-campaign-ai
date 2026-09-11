"""
Pure-logic tests for app/services/luma_contact_location_enrichment.py --
Client CRM Stage 6B.2's fill-only Luma Event structured geo -> CrmContact
city/state/country enrichment. No store/service wiring here -- see
tests/test_luma_sync_service.py for the live end-to-end integration tests
(feature flag, Activity Log, idempotency, engagement_id provenance
resolution).
"""

import uuid
from datetime import datetime, timezone

from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaEvent, LumaRegistration
from app.services.luma_contact_enrichment import FIELD_PROVENANCE_KEY
from app.services.luma_contact_location_enrichment import (
    LUMA_EVENT_LOCATION_SOURCE,
    apply_luma_event_location_enrichment,
    is_eligible_for_location_enrichment,
    normalize_country_code,
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
        registered_at=_now(),
        synced_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


# --- country normalization ------------------------------------------------------


def test_known_country_code_normalizes_to_full_name_matching_crm_convention():
    assert normalize_country_code("US") == "United States"
    assert normalize_country_code("us") == "United States"  # case-insensitive
    assert normalize_country_code(" US ") == "United States"  # trimmed


def test_unknown_country_code_normalizes_to_none_rather_than_writing_the_raw_code():
    assert normalize_country_code("ZZ") == None  # noqa: E711 -- explicit None, not falsy-check


def test_blank_country_code_normalizes_to_none():
    assert normalize_country_code(None) is None
    assert normalize_country_code("") is None


def test_an_already_conventional_full_name_passes_through_unchanged():
    """Defensive case: no evidence Luma ever sends a full name (the one
    confirmed live fixture is a 2-letter code), but if it someday did,
    and that value already exactly matches AstroHub's own convention, it
    must be preserved -- never wrongly rejected as an unrecognized code."""
    assert normalize_country_code("United States") == "United States"
    assert normalize_country_code("Israel") == "Israel"


def test_an_unrecognized_non_code_string_still_fails_safe_to_none():
    """A string that is neither a known code nor an already-conventional
    full name (e.g. a typo, or a full name AstroHub doesn't itself use
    anywhere) must never be written -- same conservative, fail-closed
    behavior as an unknown code."""
    assert normalize_country_code("USA") is None
    assert normalize_country_code("United State") is None  # the real, confirmed production typo -- never propagated


# --- eligibility ------------------------------------------------------------------


def test_eligible_when_registered_at_is_set_regardless_of_approval_status():
    reg = make_registration(registered_at=_now(), approval_status=LumaApprovalStatus.DECLINED)
    assert is_eligible_for_location_enrichment(reg) is True


def test_not_eligible_when_registered_at_is_none():
    reg = make_registration(registered_at=None, approval_status=LumaApprovalStatus.INVITED)
    assert is_eligible_for_location_enrichment(reg) is False


def test_not_eligible_when_invited_then_declined_without_ever_registering():
    """The exact 'invited-only, self-declined' scenario -- registered_at
    stays null throughout, regardless of the final approval_status."""
    reg = make_registration(registered_at=None, invited_at=_now(), approval_status=LumaApprovalStatus.DECLINED)
    assert is_eligible_for_location_enrichment(reg) is False


def test_eligible_when_registered_then_host_declined():
    """Final RSVP status is never the deciding factor -- registered_at
    being set is what matters, even though this person ended up declined."""
    reg = make_registration(registered_at=_now(), approval_status=LumaApprovalStatus.DECLINED)
    assert is_eligible_for_location_enrichment(reg) is True


# --- fill-only merge behavior -------------------------------------------------------


def test_all_blank_fills_all_available_fields():
    contact = make_contact()
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact.city == "Austin"
    assert outcome.contact.state == "Texas"
    assert outcome.contact.country == "United States"
    assert set(outcome.changed_field_keys) == {"city", "state", "country", "custom:field_provenance"}


def test_city_already_populated_is_preserved_state_and_country_still_fill():
    contact = make_contact(city="San Francisco")
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact.city == "San Francisco"  # preserved
    assert outcome.contact.state == "Texas"  # filled
    assert outcome.contact.country == "United States"  # filled
    assert "city" not in outcome.changed_field_keys


def test_all_fields_already_populated_is_a_complete_no_op():
    contact = make_contact(city="San Francisco", state="California", country="United States")
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact is contact  # same object -- never even copied
    assert outcome.changed_field_keys == []


def test_not_eligible_is_a_no_op_even_with_full_event_geo_and_all_blank():
    contact = make_contact()
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration(registered_at=None, approval_status=LumaApprovalStatus.INVITED)
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.changed_field_keys == []
    assert outcome.contact.city is None


def test_partial_event_geo_fills_only_whats_available():
    contact = make_contact()
    event = make_event(location_city="Austin", location_region="Texas", location_country=None)
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact.city == "Austin"
    assert outcome.contact.state == "Texas"
    assert outcome.contact.country is None  # never inferred
    assert "country" not in outcome.changed_field_keys


def test_event_with_no_structured_geo_at_all_is_a_clean_no_op():
    contact = make_contact()
    event = make_event()  # location_city/region/country all None
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.changed_field_keys == []
    assert outcome.contact is contact


def test_unnormalizable_country_code_leaves_country_blank_not_a_raw_code():
    contact = make_contact()
    event = make_event(location_city="Somewhere", location_region=None, location_country="ZZ")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact.city == "Somewhere"
    assert outcome.contact.country is None  # never "ZZ"
    assert "country" not in outcome.changed_field_keys


# --- provenance ---------------------------------------------------------------------


def test_provenance_is_written_only_for_fields_actually_filled():
    contact = make_contact(city="San Francisco")  # city already set -- won't change
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration(luma_guest_id="gst-42")
    outcome = apply_luma_event_location_enrichment(contact, event, reg, engagement_id="eng-1")

    provenance = outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]
    assert "city" not in provenance  # untouched field gets no provenance entry
    assert provenance["state"]["source"] == LUMA_EVENT_LOCATION_SOURCE
    assert provenance["state"]["luma_event_id"] == event.luma_event_id
    assert provenance["state"]["luma_guest_id"] == "gst-42"
    assert provenance["state"]["engagement_id"] == "eng-1"
    assert "inferred_at" in provenance["state"]
    assert provenance["country"]["source"] == LUMA_EVENT_LOCATION_SOURCE


def test_engagement_id_omitted_from_provenance_when_not_given():
    contact = make_contact()
    event = make_event(location_city="Austin")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)  # no engagement_id passed

    assert "engagement_id" not in outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]["city"]


def test_sibling_custom_fields_and_other_provenance_entries_are_preserved():
    contact = make_contact(
        custom_fields={
            "role": ["Investor"],
            FIELD_PROVENANCE_KEY: {"company": {"source": "luma_self_report", "luma_guest_id": "gst-other"}},
        }
    )
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact.custom_fields["role"] == ["Investor"]  # untouched sibling custom field
    provenance = outcome.contact.custom_fields[FIELD_PROVENANCE_KEY]
    assert provenance["company"] == {"source": "luma_self_report", "luma_guest_id": "gst-other"}  # untouched
    assert provenance["city"]["source"] == LUMA_EVENT_LOCATION_SOURCE  # new entry added alongside


def test_no_update_means_no_custom_fields_mutation_at_all():
    """A complete no-op (ineligible, no geo, or everything already filled)
    must never even touch custom_fields -- not merely leave its content
    equal, but never construct a new dict/model_copy at all."""
    contact = make_contact(city="X", state="Y", country="Z")
    event = make_event(location_city="Austin", location_region="Texas", location_country="US")
    reg = make_registration()
    outcome = apply_luma_event_location_enrichment(contact, event, reg)

    assert outcome.contact is contact
    assert FIELD_PROVENANCE_KEY not in outcome.contact.custom_fields
