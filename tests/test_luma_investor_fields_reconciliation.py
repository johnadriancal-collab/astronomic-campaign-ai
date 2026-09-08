"""
Tests for app/services/luma_investor_fields_reconciliation.py -- the
historical reconciliation driver for the four investor-related Luma
custom fields affected by the stale question_type mapping bug (see
LumaSyncService's own "Deploying Capital" investigation).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaQuestionMapping, LumaRegistration, LumaRegistrationAnswer
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_investor_fields_reconciliation import TARGET_FIELD_KEYS, run_investor_fields_reconciliation
from app.services.luma_sync_service import LumaSyncService

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def make_answer(label: str, question_type: str, value, question_id: str = "q-1") -> LumaRegistrationAnswer:
    return LumaRegistrationAnswer(question_id=question_id, label=label, question_type=question_type, value=value)


def make_registration(**overrides) -> LumaRegistration:
    defaults = dict(
        luma_guest_id=str(uuid.uuid4()),
        luma_event_id="e1",
        match_status=LumaMatchStatus.MATCHED,
        approval_status=LumaApprovalStatus.APPROVED,
        synced_at=_now(),
        updated_at=_now(),
        registration_answers=[],
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


def make_mapping(**overrides) -> LumaQuestionMapping:
    defaults = dict(
        luma_question_mapping_id=str(uuid.uuid4()),
        question_label="Deploying Capital",
        question_type=None,
        target_field_key="custom:deploying_capital",
        extract_key=None,
        normalizer=None,
        active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaQuestionMapping(**defaults)


@pytest.fixture
def custom_field_store():
    return MemoryCrmCustomFieldStore()


@pytest.fixture
def mapping_store():
    return MemoryLumaQuestionMappingStore()


@pytest.fixture
def registration_store():
    return MemoryLumaRegistrationStore()


@pytest.fixture
def event_store():
    return MemoryLumaEventStore()


@pytest_asyncio.fixture
async def crm_service(custom_field_store):
    async def _define(key, field_type, options=None):
        await custom_field_store.create(
            CrmCustomFieldDefinition(
                crm_custom_field_id=str(uuid.uuid4()),
                field_key=key,
                label=key,
                field_type=field_type,
                options=options or [],
                active=True,
                created_at=_now(),
                updated_at=_now(),
            )
        )

    await _define("investor_type", CustomFieldType.MULTI_SELECT, ["Angel Investor", "Family Office", "Venture Capital"])
    await _define("check_size_personal", CustomFieldType.MULTI_SELECT, ["$25k - $50k", "$50k - $100k", "$100k - $250k"])
    await _define("deploying_capital", CustomFieldType.SINGLE_SELECT, ["Yes, actively", "Selectively", "Not at the moment"])
    await _define("investment_industry", CustomFieldType.MULTI_SELECT, [])  # open-ended, matches production
    await _define("role", CustomFieldType.MULTI_SELECT, ["Investor", "Founder"])
    return CrmService(custom_field_store=custom_field_store)


@pytest.fixture
def luma_service(crm_service, event_store, registration_store, mapping_store):
    return LumaSyncService(
        crm_service=crm_service,
        event_store=event_store,
        registration_store=registration_store,
        mapping_store=mapping_store,
        activity_log=crm_service.activity_log,
    )


async def _seed_four_mappings(mapping_store, *, question_type=None):
    await mapping_store.create(make_mapping(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="Investor Type", question_type=question_type,
        target_field_key="custom:investor_type",
    ))
    await mapping_store.create(make_mapping(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="Check Size", question_type=question_type,
        target_field_key="custom:check_size_personal",
    ))
    await mapping_store.create(make_mapping(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="Deploying Capital", question_type=question_type,
        target_field_key="custom:deploying_capital",
    ))
    await mapping_store.create(make_mapping(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="Investment Industry", question_type=question_type,
        target_field_key="custom:investment_industry",
    ))


# --- core recovery scenario ---------------------------------------------------


async def test_reconciliation_recovers_a_previously_dropped_select_typed_answer(luma_service, crm_service, registration_store, mapping_store):
    """The exact Wesley/Gustavo-shaped bug: a "select"-typed historical
    answer, silently dropped before the mapping fix, is recovered once
    question_type=None."""
    await _seed_four_mappings(mapping_store, question_type=None)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id,
            registered_at=_now(),
            registration_answers=[make_answer("Deploying Capital", "select", "Yes, actively")],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.contacts_would_change == 1
    example = report.examples[0]
    assert example.crm_contact_id == contact.crm_contact_id
    change = next(c for c in example.changes if c.field_key == "deploying_capital")
    assert change.old_value is None
    assert change.new_value == "Yes, actively"  # exact string, never a boolean


async def test_dry_run_makes_zero_contact_saves(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Deploying Capital", "select", "Selectively")],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.contacts_saved == 0
    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert "deploying_capital" not in persisted.custom_fields


async def test_write_mode_applies_and_is_idempotent_on_rerun(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Deploying Capital", "select", "Selectively")],
        )
    )

    first = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)
    assert first.counts.contacts_saved == 1
    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.custom_fields["deploying_capital"] == "Selectively"

    second = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)
    assert second.counts.contacts_saved == 0
    assert second.counts.contacts_would_change == 0
    assert second.counts.contacts_unchanged == 1


async def test_no_luma_registration_mutation(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    reg = make_registration(
        crm_contact_id=contact.crm_contact_id, registered_at=_now(),
        registration_answers=[make_answer("Deploying Capital", "select", "Yes, actively")],
    )
    await registration_store.save(reg)
    before = await registration_store.get(reg.luma_guest_id)

    await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    after = await registration_store.get(reg.luma_guest_id)
    assert after == before


def test_reconciliation_module_makes_no_luma_api_calls():
    """Structural guard -- this module must never import a Luma HTTP
    client; it only ever reads already-persisted registration_answers."""
    import inspect

    from app.services import luma_investor_fields_reconciliation as module

    source = inspect.getsource(module)
    assert "LumaClient" not in source
    assert "luma_client" not in source


# --- scope: only the four target fields, nothing else ----------------------


async def test_reconciliation_never_touches_company_title_or_role(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    await mapping_store.create(make_mapping(
        luma_question_mapping_id=str(uuid.uuid4()), question_label="Company", question_type="company",
        target_field_key="company", extract_key="company",
    ))
    contact = make_contact(company=None)
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id,
            registered_at=_now(),
            registration_answers=[
                make_answer("Deploying Capital", "select", "Yes, actively", question_id="q-1"),
                make_answer("Company", "company", {"company": "Acme"}, question_id="q-2"),
                make_answer("Investor Type", "select", ["Angel Investor"], question_id="q-3"),  # investor signal, would normally auto-tag role
            ],
        )
    )

    await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.company is None  # never touched -- company/title reprocessing is a separate concern
    assert "role" not in persisted.custom_fields  # investor-role auto-tagging never runs here


# --- multi-select union-merge / single-select fill-only, preserved ---------


async def test_union_merges_multi_select_preserving_existing_selections(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact(custom_fields={"investor_type": ["Venture Capital"]})
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investor Type", "select", ["Family Office"])],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert set(persisted.custom_fields["investor_type"]) == {"Venture Capital", "Family Office"}
    assert report.counts.changes_by_field.get("investor_type") == 1


async def test_deploying_capital_is_fill_only_not_overwritten(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact(custom_fields={"deploying_capital": "Not at the moment"})
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Deploying Capital", "select", "Yes, actively")],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.contacts_would_change == 0
    assert report.counts.contacts_unchanged == 1


async def test_a_bare_scalar_select_answer_is_still_wrapped_for_a_multi_select_field(luma_service, crm_service, registration_store, mapping_store):
    """The destination field's own definition, not Luma's question_type,
    decides list-vs-scalar shape -- reused unmodified from
    _wrap_scalar_for_multi_select via apply_import_mapping()."""
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investor Type", "select", "Angel Investor")],  # bare scalar, not a list
        )
    )

    await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.custom_fields["investor_type"] == ["Angel Investor"]


# --- multiple registrations per contact, chronological fold -----------------


async def test_multiple_registrations_fold_chronologically_and_union_merge(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    older = make_registration(
        luma_guest_id="g-older", crm_contact_id=contact.crm_contact_id, registered_at=_now() - timedelta(days=10),
        registration_answers=[make_answer("Investor Type", "multi-select", ["Venture Capital"])],
    )
    newer = make_registration(
        luma_guest_id="g-newer", crm_contact_id=contact.crm_contact_id, registered_at=_now(),
        registration_answers=[make_answer("Investor Type", "select", ["Family Office"])],
    )
    await registration_store.save(newer)  # saved out of order -- fold must not depend on save order
    await registration_store.save(older)

    await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert set(persisted.custom_fields["investor_type"]) == {"Venture Capital", "Family Office"}


# --- ambiguous / invalid classification (report-only) -----------------------


async def test_invalid_value_rejected_by_destination_field_is_counted_and_not_written(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investor Type", "select", ["Not A Real Option"])],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.invalid_values_rejected == 1
    assert report.counts.contacts_would_change == 0
    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert "investor_type" not in persisted.custom_fields


async def test_ambiguous_answer_is_counted_and_skipped(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Deploying Capital", "select", {"unexpected": "shape"})],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.ambiguous_unmappable_answers == 1
    assert report.counts.contacts_would_change == 0


# --- unresolved registrations, unrelated questions --------------------------


async def test_unresolved_registrations_are_skipped_and_counted(luma_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    await registration_store.save(
        make_registration(crm_contact_id=None, match_status=LumaMatchStatus.NEEDS_REVIEW, registration_answers=[make_answer("Deploying Capital", "select", "Selectively")])
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.registrations_unresolved_skipped == 1
    assert report.counts.contacts_examined == 0


async def test_an_unrelated_question_never_counts_toward_ambiguous_or_invalid(luma_service, crm_service, registration_store, mapping_store):
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Do you currently support an organization or cause? If yes, which?", "text", "Yes, several")],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    assert report.counts.ambiguous_unmappable_answers == 0
    assert report.counts.invalid_values_rejected == 0
    assert report.counts.contacts_would_change == 0


def test_target_field_keys_are_exactly_the_four_approved_fields():
    assert TARGET_FIELD_KEYS == {
        "custom:investor_type",
        "custom:check_size_personal",
        "custom:deploying_capital",
        "custom:investment_industry",
    }


# --- schema-alignment fix (2026-09-08): Corporate Venture, Fund Manager /
# General Partner, and Other (investor_type + investment_industry) added as
# CRM-accepted values after the audit found 18 legitimate historical Luma
# answers using them -- these tests prove the values are accepted once the
# options are expanded, and that expansion is additive: pre-existing
# options/selections keep working exactly as before. ---------------------


async def test_corporate_venture_and_fund_manager_are_still_rejected_before_the_options_are_expanded(
    luma_service, crm_service, registration_store, mapping_store
):
    """Baseline/contrast case: with the field's OLD, narrower options
    (this fixture's default -- Angel Investor, Family Office, Venture
    Capital), these two values are still correctly rejected. The next
    test proves the same raw answer is accepted once the field's options
    are expanded to include them."""
    await _seed_four_mappings(mapping_store)
    investor_type_mapping = next(m for m in await mapping_store.list() if m.target_field_key == "custom:investor_type")
    await mapping_store.save(investor_type_mapping.model_copy(update={"normalizer": "investor_type_label"}))
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investor Type", "select", ["Corporate Venture", "Fund Manager / General Partner"])],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=True)

    # _classify_answer counts per ANSWER, not per raw item inside it -- one
    # multi-select answer with two rejected values is one rejected answer.
    assert report.counts.invalid_values_rejected == 1
    assert report.counts.contacts_would_change == 0


async def test_corporate_venture_fund_manager_and_other_are_accepted_once_investor_type_options_are_expanded(
    luma_service, crm_service, registration_store, mapping_store, custom_field_store
):
    """The actual fix: additively expanding investor_type's options to
    include the 3 previously-rejected values makes them eligible, union-
    merged alongside the contact's existing selection -- nothing dropped,
    nothing renamed."""
    field = await custom_field_store.get_by_field_key("investor_type")
    await custom_field_store.save(
        field.model_copy(update={"options": [*field.options, "Corporate Venture", "Fund Manager / General Partner", "Other"]})
    )
    await _seed_four_mappings(mapping_store)
    investor_type_mapping = next(m for m in await mapping_store.list() if m.target_field_key == "custom:investor_type")
    await mapping_store.save(investor_type_mapping.model_copy(update={"normalizer": "investor_type_label"}))
    contact = make_contact(custom_fields={"investor_type": ["Angel Investor"]})
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[
                make_answer("Investor Type", "select", ["Corporate Venture", "Fund Manager / General Partner", "Other"])
            ],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert set(persisted.custom_fields["investor_type"]) == {
        "Angel Investor", "Corporate Venture", "Fund Manager / General Partner", "Other",
    }
    assert report.counts.invalid_values_rejected == 0
    assert report.counts.changes_by_field.get("investor_type") == 1


async def test_investment_industry_other_is_accepted_and_union_merged_using_the_real_industry_options(
    luma_service, crm_service, registration_store, mapping_store
):
    """Uses the REAL app.models.crm.INDUSTRY_OPTIONS (not a fixture list)
    via the real industry_focus_label normalizer -- "Other" is a
    canonical member of that list as of 2026-09-08, so it survives and
    union-merges with the contact's existing canonical selection."""
    await _seed_four_mappings(mapping_store)
    industry_mapping = next(m for m in await mapping_store.list() if m.target_field_key == "custom:investment_industry")
    await mapping_store.save(industry_mapping.model_copy(update={"normalizer": "industry_focus_label"}))
    contact = make_contact(custom_fields={"investment_industry": ["Cybersecurity"]})
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investment Industry", "select", ["Cybersecurity", "Other"])],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert set(persisted.custom_fields["investment_industry"]) == {"Cybersecurity", "Other"}
    assert report.counts.invalid_values_rejected == 0
    assert report.counts.changes_by_field.get("investment_industry") == 1


async def test_pre_existing_investor_type_options_still_work_after_expansion(
    luma_service, crm_service, registration_store, mapping_store, custom_field_store
):
    """The expansion is additive only -- pre-existing options
    (Venture Capital, from this fixture's original 3) still resolve
    exactly as before, unaffected by the 3 new options being appended."""
    field = await custom_field_store.get_by_field_key("investor_type")
    await custom_field_store.save(
        field.model_copy(update={"options": [*field.options, "Corporate Venture", "Fund Manager / General Partner", "Other"]})
    )
    await _seed_four_mappings(mapping_store)
    contact = make_contact()
    await crm_service.contact_store.create(contact)
    await registration_store.save(
        make_registration(
            crm_contact_id=contact.crm_contact_id, registered_at=_now(),
            registration_answers=[make_answer("Investor Type", "select", ["Venture Capital"])],
        )
    )

    report = await run_investor_fields_reconciliation(luma_service, registration_store, dry_run=False)

    persisted = await crm_service.contact_store.get(contact.crm_contact_id)
    assert persisted.custom_fields["investor_type"] == ["Venture Capital"]
    assert report.counts.invalid_values_rejected == 0
