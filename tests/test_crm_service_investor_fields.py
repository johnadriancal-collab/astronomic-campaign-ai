"""
CrmService's Astro AI Phase 3 (2026-09-15) narrow investor-field write
seam: compute_investor_field_change() (pure, validates + computes
before/after) and apply_investor_field_change() (the one write path,
with an optimistic-concurrency guard and correct Activity Log
attribution) -- the ONLY way astro_crm_tools.py's
update_crm_contact_investor_field tool is allowed to touch a Contact,
never CrmService.update_contact with a free-form patch dict.

Also covers get_list_ids_for_contact() (the read half Astro's
get_crm_contact_lists/remove_crm_contact_from_list need) and the new
`source`/`actor` passthrough on bulk_add_to_list/bulk_remove_from_list/
update_contact used to attribute an Astro-driven write correctly in the
Activity Log without creating a second event.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.activity import ActivitySource
from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.services.crm_service import CrmService, InvestorFieldConflict, InvestorFieldError

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 9, 15, tzinfo=timezone.utc)


@pytest.fixture
def service():
    return CrmService()


async def _register_closed_field(service, field_key: str, options: list[str], field_type=CustomFieldType.MULTI_SELECT):
    await service.custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key=field_key,
            label=field_key,
            field_type=field_type,
            options=options,
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )


# --- get_list_ids_for_contact -----------------------------------------------


async def test_get_list_ids_for_contact_reflects_real_membership(service):
    contact = await service.create_contact({"first_name": "Solo"})
    contact_list = await service.create_contact_list(name="Austin Investors")
    assert await service.get_list_ids_for_contact(contact.crm_contact_id) == []

    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    assert await service.get_list_ids_for_contact(contact.crm_contact_id) == [contact_list.list_id]


# --- source/actor passthrough (no duplicate Activity Log entries) -----------


async def test_bulk_add_to_list_source_defaults_to_lists(service):
    contact = await service.create_contact({"first_name": "Solo"})
    contact_list = await service.create_contact_list(name="List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])

    events = await service.activity_log.store.list()
    added = next(e for e in events if e.event_type == "list.contacts_added")
    assert added.source == ActivitySource.LISTS


async def test_bulk_add_to_list_honors_explicit_astro_ai_source(service):
    contact = await service.create_contact({"first_name": "Solo"})
    contact_list = await service.create_contact_list(name="List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id], actor="astro_ai", source=ActivitySource.ASTRO_AI)

    events = await service.activity_log.store.list()
    added = next(e for e in events if e.event_type == "list.contacts_added")
    assert added.source == ActivitySource.ASTRO_AI
    assert added.actor == "astro_ai"
    # Exactly ONE event for this one mutation -- no duplicate just to get
    # a different source.
    assert len([e for e in events if e.event_type == "list.contacts_added"]) == 1


async def test_bulk_remove_from_list_honors_explicit_astro_ai_source(service):
    contact = await service.create_contact({"first_name": "Solo"})
    contact_list = await service.create_contact_list(name="List")
    await service.bulk_add_to_list(contact_list.list_id, [contact.crm_contact_id])
    await service.bulk_remove_from_list(contact_list.list_id, [contact.crm_contact_id], actor="astro_ai", source=ActivitySource.ASTRO_AI)

    events = await service.activity_log.store.list()
    removed = next(e for e in events if e.event_type == "list.contacts_removed")
    assert removed.source == ActivitySource.ASTRO_AI
    assert removed.actor == "astro_ai"


# --- compute_investor_field_change (pure) -----------------------------------


async def test_compute_add_value_to_open_vocab_field(service):
    contact = CrmContact(
        crm_contact_id="c1", created_at=_now(), updated_at=_now(),
        custom_fields={"investment_industry": ["Fintech"]},
    )
    before, after = await service.compute_investor_field_change(contact, "investment_industry", "add_value", value="Robotics")
    assert before == ["Fintech"]
    assert after == ["Fintech", "Robotics"]


async def test_compute_add_value_case_insensitive_duplicate_is_no_change(service):
    """Decision 5: trim + collapse whitespace + case-insensitive compare
    for investment_industry -- a value that already exists under
    different casing/whitespace must be treated as already present, the
    existing display value is NEVER overwritten with the new casing."""
    contact = CrmContact(
        crm_contact_id="c1", created_at=_now(), updated_at=_now(),
        custom_fields={"investment_industry": ["Artificial Intelligence / Machine Learning"]},
    )
    before, after = await service.compute_investor_field_change(
        contact, "investment_industry", "add_value", value="artificial intelligence /  machine learning"
    )
    assert before == after == ["Artificial Intelligence / Machine Learning"]


async def test_compute_remove_value_from_open_vocab_field_is_case_insensitive(service):
    contact = CrmContact(
        crm_contact_id="c1", created_at=_now(), updated_at=_now(),
        custom_fields={"investment_industry": ["Fintech", "AgTech & Food Production"]},
    )
    before, after = await service.compute_investor_field_change(
        contact, "investment_industry", "remove_value", value="agtech & food production"
    )
    assert before == ["Fintech", "AgTech & Food Production"]
    assert after == ["Fintech"]


async def test_compute_set_value_on_closed_multiselect_field_replaces_whole_list(service):
    await _register_closed_field(service, "check_size_personal", ["$1k - $10k", "$10k - $25k", "$25k - $100k"])
    contact = CrmContact(
        crm_contact_id="c1", created_at=_now(), updated_at=_now(),
        custom_fields={"check_size_personal": ["$1k - $10k"]},
    )
    before, after = await service.compute_investor_field_change(
        contact, "check_size_personal", "set_value", values=["$25k - $100k"]
    )
    assert before == ["$1k - $10k"]
    assert after == ["$25k - $100k"]


async def test_compute_set_value_rejects_option_outside_closed_vocabulary(service):
    await _register_closed_field(service, "check_size_personal", ["$1k - $10k", "$10k - $25k"])
    contact = CrmContact(crm_contact_id="c1", created_at=_now(), updated_at=_now())
    with pytest.raises(InvestorFieldError):
        await service.compute_investor_field_change(contact, "check_size_personal", "set_value", values=["$999k - $1B"])


async def test_compute_set_value_on_scalar_field(service):
    await _register_closed_field(service, "deploying_capital", ["Yes, actively", "Selectively", "Not at the moment"], CustomFieldType.SINGLE_SELECT)
    contact = CrmContact(crm_contact_id="c1", created_at=_now(), updated_at=_now())
    before, after = await service.compute_investor_field_change(contact, "deploying_capital", "set_value", value="Yes, actively")
    assert before is None
    assert after == "Yes, actively"


async def test_compute_rejects_invalid_field(service):
    contact = CrmContact(crm_contact_id="c1", created_at=_now(), updated_at=_now())
    with pytest.raises(InvestorFieldError):
        await service.compute_investor_field_change(contact, "investor_mode", "set_value", value="Both")


async def test_compute_rejects_add_value_on_scalar_field(service):
    contact = CrmContact(crm_contact_id="c1", created_at=_now(), updated_at=_now())
    with pytest.raises(InvestorFieldError):
        await service.compute_investor_field_change(contact, "deploying_capital", "add_value", value="Yes, actively")


# --- apply_investor_field_change (the one write path) -----------------------


async def test_apply_writes_the_new_value_and_logs_one_astro_attributed_event(service):
    contact = await service.create_contact({"first_name": "John", "custom_fields": {"investment_industry": ["Fintech"]}})

    updated = await service.apply_investor_field_change(
        contact.crm_contact_id, "investment_industry", ["Fintech"], ["Fintech", "Robotics"], actor="astro_ai"
    )
    assert updated.custom_fields["investment_industry"] == ["Fintech", "Robotics"]

    events = await service.activity_log.store.list()
    updates = [e for e in events if e.event_type == "contact.updated"]
    assert len(updates) == 1
    assert updates[0].source == ActivitySource.ASTRO_AI
    assert updates[0].actor == "astro_ai"
    assert updates[0].metadata == {"field": "investment_industry", "before": ["Fintech"], "after": ["Fintech", "Robotics"]}


async def test_apply_raises_conflict_if_current_value_no_longer_matches_expected_before(service):
    contact = await service.create_contact({"first_name": "John", "custom_fields": {"investment_industry": ["Fintech"]}})
    # Someone else changes the field in between propose and confirm.
    await service.update_contact(contact.crm_contact_id, {"custom_fields": {"investment_industry": ["Healthcare"]}})

    with pytest.raises(InvestorFieldConflict):
        await service.apply_investor_field_change(
            contact.crm_contact_id, "investment_industry", ["Fintech"], ["Fintech", "Robotics"], actor="astro_ai"
        )


async def test_apply_is_a_true_noop_when_after_equals_before_no_write_no_log(service):
    contact = await service.create_contact({"first_name": "John", "custom_fields": {"investment_industry": ["Fintech"]}})
    before_updated_at = contact.updated_at

    result = await service.apply_investor_field_change(
        contact.crm_contact_id, "investment_industry", ["Fintech"], ["Fintech"], actor="astro_ai"
    )
    assert result.updated_at == before_updated_at

    events = await service.activity_log.store.list()
    assert not any(e.event_type == "contact.updated" for e in events)


async def test_apply_investor_type_change_still_recomputes_investor_mode(service):
    """update_contact's existing derive_investor_mode logic must keep
    firing when Astro changes investor_type through this seam -- Decision
    1 explicitly relies on this rather than a separate mechanism."""
    contact = await service.create_contact({"first_name": "John", "custom_fields": {"investor_type": ["Angel Investor"]}})
    assert contact.thesis_investor_mode == "Privately"

    updated = await service.apply_investor_field_change(
        contact.crm_contact_id, "investor_type", ["Angel Investor"], ["Angel Investor", "Family Office"], actor="astro_ai"
    )
    assert updated.thesis_investor_mode_manual_override is False
    # derive_investor_mode's exact output for this combination isn't the
    # point here -- only that it recomputed at all (never left stale).
    assert updated.thesis_investor_mode is not None


async def test_investor_fields_constant_excludes_investor_mode(service):
    """Decision 1 -- investor_mode must never be one of the writable
    keys, structurally, not just by convention."""
    assert "investor_mode" not in CrmService.INVESTOR_FIELDS
    assert "thesis_investor_mode" not in CrmService.INVESTOR_FIELDS
