"""
Client CRM Stage 6B.1 -- structured Luma Event geo capture
(LumaEvent.location_city/location_region/location_country), additive
alongside the existing lossy location_summary. Pure-function/model tests
for the extraction and backward-compatibility guarantees themselves; see
tests/test_luma_sync_service.py's own Stage 6B.1 section for the live
end-to-end wiring (via process_guest_event + a real LumaEventStore).
"""

import json
from datetime import datetime, timezone

from app.models.luma import LumaEvent
from app.services.luma_sync_service import _derive_location_summary, _derive_structured_geo


def _now() -> datetime:
    return datetime(2026, 9, 11, tzinfo=timezone.utc)


def make_event_payload(**overrides) -> dict:
    base = {
        "id": "evt-1",
        "calendar_id": "cal-1",
        "name": "Austin Investor Dinner",
        "start_at": "2026-09-01T18:00:00Z",
        "end_at": "2026-09-01T21:00:00Z",
        "url": "https://lu.ma/evt-1",
    }
    base.update(overrides)
    return base


# --- structured extraction ----------------------------------------------------


def test_full_structured_geo_is_extracted_from_geo_address_json():
    payload = make_event_payload(geo_address_json={"address": "Austin", "city": "Austin", "region": "Texas", "country": "US"})
    city, region, country = _derive_structured_geo(payload)
    assert city == "Austin"
    assert region == "Texas"
    assert country == "US"  # raw, unnormalized -- see module docstring


def test_structured_geo_country_is_never_normalized_at_capture_time():
    """Normalization to AstroHub's full-name convention is an
    enrichment-time concern (Stage 6B.2), never a capture-time one."""
    payload = make_event_payload(geo_address_json={"city": "Tel Aviv", "region": "Tel Aviv District", "country": "IL"})
    _city, _region, country = _derive_structured_geo(payload)
    assert country == "IL"


def test_partial_structured_geo_extracts_only_whats_present():
    payload = make_event_payload(geo_address_json={"city": "Austin", "region": "Texas"})  # no country key at all
    city, region, country = _derive_structured_geo(payload)
    assert city == "Austin"
    assert region == "Texas"
    assert country is None


def test_city_falls_back_to_address_when_city_key_absent():
    payload = make_event_payload(geo_address_json={"address": "The Grove"})
    city, _region, _country = _derive_structured_geo(payload)
    assert city == "The Grove"


def test_no_geo_address_json_at_all_extracts_nothing():
    payload = make_event_payload()
    assert _derive_structured_geo(payload) == (None, None, None)


def test_non_dict_geo_address_json_never_raises():
    payload = make_event_payload(geo_address_json="not a dict")
    assert _derive_structured_geo(payload) == (None, None, None)


def test_blank_string_geo_values_are_treated_as_absent():
    payload = make_event_payload(geo_address_json={"city": "   ", "region": "", "country": None})
    assert _derive_structured_geo(payload) == (None, None, None)


# --- location_summary preserved unchanged --------------------------------------


def test_location_summary_behavior_is_unchanged_by_this_stage():
    payload = make_event_payload(geo_address_json={"address": "Austin", "city": "Austin", "region": "Texas", "country": "US"})
    assert _derive_location_summary(payload) == "Austin"

    online_payload = make_event_payload(geo_address_json=None, meeting_url="https://zoom.us/j/123")
    assert _derive_location_summary(online_payload) == "Online"


# --- backward compatibility: historical rows without the new fields -----------


def test_historical_luma_event_json_missing_new_fields_deserializes_safely():
    """Simulates a LumaEvent row persisted BEFORE Stage 6B.1 -- the stored
    JSON blob simply has no location_city/location_region/location_country
    keys at all (SQLiteLumaEventStore stores the full model as one JSON
    blob via model_dump_json(), so this is exactly what a pre-existing row
    looks like on disk). Must load with all three as None, never raise."""
    legacy_json = json.dumps({
        "luma_event_id": "evt-legacy",
        "calendar_id": "cal-1",
        "name": "Old Dinner",
        "start_at": None,
        "end_at": None,
        "status": None,
        "location_summary": "Austin",
        "url": None,
        "synced_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    })
    event = LumaEvent.model_validate_json(legacy_json)
    assert event.location_city is None
    assert event.location_region is None
    assert event.location_country is None
    assert event.location_summary == "Austin"  # untouched existing field
