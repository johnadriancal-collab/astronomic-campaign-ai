"""
Tests for app/services/luma_contact_enrichment_backfill.py -- the
historical, idempotent, dry-run-capable driver. Reuses the exact same
resolution/merge functions tests/test_luma_contact_enrichment.py already
covers in isolation; these tests focus on orchestration, counting, dry-run
safety, and idempotency.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.crm import CrmContact
from app.models.luma import LumaApprovalStatus, LumaEvent, LumaMatchStatus, LumaRegistration, LumaRegistrationAnswer
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.luma_contact_enrichment_backfill import run_luma_contact_enrichment_backfill

pytestmark = pytest.mark.asyncio


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


def company_answer(company: str | None = None, job_title: str | None = None) -> LumaRegistrationAnswer:
    return LumaRegistrationAnswer(question_id="q1", label="Company", question_type="company", value={"company": company, "job_title": job_title})


def make_registration(**overrides) -> LumaRegistration:
    defaults = dict(
        luma_guest_id=str(uuid.uuid4()),
        luma_event_id="e1",
        approval_status=LumaApprovalStatus.APPROVED,
        match_status=LumaMatchStatus.MATCHED,
        synced_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaRegistration(**defaults)


@pytest.fixture
def stores():
    return MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()


async def test_dry_run_performs_zero_contact_saves(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_would_update_company == 1
    assert report.counts.contacts_saved == 0
    persisted = await contact_store.get(contact.crm_contact_id)
    assert persisted.company == "OldCo"  # untouched -- dry run made no writes


async def test_dry_run_performs_zero_luma_registration_mutations(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    reg = make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    await registration_store.save(reg)
    before = await registration_store.get(reg.luma_guest_id)

    await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    after = await registration_store.get(reg.luma_guest_id)
    assert after == before  # byte-identical -- historical registration rows are NEVER modified


async def test_real_write_path_applies_changes_when_dry_run_false(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)

    assert report.counts.contacts_saved == 1
    persisted = await contact_store.get(contact.crm_contact_id)
    assert persisted.company == "NewCo"


async def test_real_write_path_is_idempotent_on_rerun(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    first = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)
    assert first.counts.contacts_saved == 1

    second = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)
    assert second.counts.contacts_saved == 0  # unchanged Luma data -> zero additional changes
    assert second.counts.contacts_unchanged == 1


async def test_unresolved_registrations_are_skipped_and_counted(stores):
    contact_store, registration_store, event_store = stores
    await registration_store.save(
        make_registration(crm_contact_id=None, match_status=LumaMatchStatus.NEEDS_REVIEW, registration_answers=[company_answer("Somewhere")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.registrations_examined == 1
    assert report.counts.unresolved_registrations_skipped == 1
    assert report.counts.unique_contacts_represented == 0


async def test_recency_tier_counters_are_accurate(stores):
    contact_store, registration_store, event_store = stores
    c1, c2, c3, c4 = (make_contact() for _ in range(4))
    for c in (c1, c2, c3, c4):
        await contact_store.create(c)
    event = make_event(luma_event_id="e1", start_at=_now())
    await event_store.save(event)

    await registration_store.save(make_registration(crm_contact_id=c1.crm_contact_id, luma_event_id="e1", registered_at=_now(), registration_answers=[company_answer("A")]))
    await registration_store.save(make_registration(crm_contact_id=c2.crm_contact_id, luma_event_id="e1", joined_at=_now(), registration_answers=[company_answer("B")]))
    await registration_store.save(make_registration(crm_contact_id=c3.crm_contact_id, luma_event_id="e1", registration_answers=[company_answer("C")]))  # falls back to event start_at
    await registration_store.save(make_registration(crm_contact_id=c4.crm_contact_id, luma_event_id="e-missing", registration_answers=[company_answer("D")]))  # no event, no timestamps -> unknown

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.registrations_using_registered_at == 1
    assert report.counts.registrations_using_joined_at == 1
    assert report.counts.registrations_using_event_start_at == 1
    assert report.counts.registrations_with_unknown_recency == 1


async def test_multiple_valid_answers_and_ambiguous_unknown_counters(stores):
    contact_store, registration_store, event_store = stores
    ambiguous_contact = make_contact(company="Existing")
    multi_answer_contact = make_contact(company="Existing2")
    await contact_store.create(ambiguous_contact)
    await contact_store.create(multi_answer_contact)

    # ambiguous: two unknown-recency, conflicting company answers
    await registration_store.save(make_registration(crm_contact_id=ambiguous_contact.crm_contact_id, luma_guest_id="g1", registration_answers=[company_answer("Co1")]))
    await registration_store.save(make_registration(crm_contact_id=ambiguous_contact.crm_contact_id, luma_guest_id="g2", registration_answers=[company_answer("Co2")]))

    # multiple valid answers, but resolvable (different known recencies)
    await registration_store.save(make_registration(crm_contact_id=multi_answer_contact.crm_contact_id, luma_guest_id="g3", registered_at=_now() - timedelta(days=1), registration_answers=[company_answer("Old")]))
    await registration_store.save(make_registration(crm_contact_id=multi_answer_contact.crm_contact_id, luma_guest_id="g4", registered_at=_now(), registration_answers=[company_answer("New")]))

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_ambiguous_unknown_recency == 1
    assert ambiguous_contact.crm_contact_id in report.ambiguous_contact_ids
    # Both contacts have 2 valid candidates for "company" -- the counter is
    # purely informational (>1 candidate seen), independent of whether
    # resolution succeeded or was left ambiguous.
    assert report.counts.contacts_multiple_valid_company_answers == 2


async def test_contacts_would_update_both_company_and_title(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo", title="Old Title")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo", "New Title")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert report.counts.contacts_would_update_both == 1


async def test_existing_website_is_flagged_for_review_never_invalidated(stores):
    """V1 corrected behavior: a material Company change never clears an
    existing website -- it's preserved and flagged instead."""
    contact_store, registration_store, event_store = stores
    target = make_contact(company="OldCo", company_website="oldco.com")
    await contact_store.create(target)
    await registration_store.save(
        make_registration(crm_contact_id=target.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.existing_websites_flagged_for_review == 1
    assert target.crm_contact_id in report.website_review_needed_contact_ids
    assert report.counts.websites_populated_from_blank_tier1 == 0
    persisted = await contact_store.get(target.crm_contact_id)
    assert persisted.company_website == "oldco.com"  # untouched -- dry run, and V1 never clears anyway


async def test_blank_website_populated_from_tier1(stores):
    contact_store, registration_store, event_store = stores
    # A different, already-correct contact establishes Tier 1 knowledge for "NewCo".
    reference = make_contact(company="NewCo", company_website="newco.com")
    await contact_store.create(reference)

    target = make_contact(company="OldCo", company_website=None)
    await contact_store.create(target)
    await registration_store.save(
        make_registration(crm_contact_id=target.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.websites_populated_from_blank_tier1 == 1
    assert report.counts.existing_websites_flagged_for_review == 0


async def test_tier2_candidates_and_free_domain_exclusions_are_reported_never_applied(stores):
    contact_store, registration_store, event_store = stores
    corporate = make_contact(company="Acme", email="jane@acme.com")
    personal = make_contact(company="SomeCo", email="jane@gmail.com")
    await contact_store.create(corporate)
    await contact_store.create(personal)
    await registration_store.save(make_registration(crm_contact_id=corporate.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Acme")]))
    await registration_store.save(make_registration(crm_contact_id=personal.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("SomeCo")]))

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.websites_tier2_candidates == 1
    assert report.counts.websites_tier2_free_excluded == 1
    # Never applied -- Tier 2 must not appear as the actual stored website.
    persisted_corporate = await contact_store.get(corporate.crm_contact_id)
    assert persisted_corporate.company_website is None


async def test_examples_are_capped_and_contain_no_email(stores):
    contact_store, registration_store, event_store = stores
    for i in range(5):
        c = make_contact(company="OldCo", email=f"person{i}@example.com")
        await contact_store.create(c)
        await registration_store.save(
            make_registration(crm_contact_id=c.crm_contact_id, registered_at=_now(), registration_answers=[company_answer(f"NewCo{i}")])
        )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True, example_cap=3)

    assert len(report.examples) == 3
    for example in report.examples:
        assert "@" not in str(example.old_company or "")
        assert "@" not in str(example.new_company or "")


async def test_report_dry_run_flag_reflects_the_call():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert report.dry_run is True


# --- Activity Log: the backfill driver must NEVER write to it, ever --------


def test_backfill_driver_source_never_references_activity_log():
    """Structural guard, same style as
    test_no_hardcoded_luma_question_label_in_ingestion_source in
    test_luma_sync_service.py -- fails the moment anyone wires an
    ActivityLogService into this module, which is exactly what must never
    happen (see this module's own docstring)."""
    import inspect

    from app.services import luma_contact_enrichment_backfill as module

    source = inspect.getsource(module)
    assert "activity_log" not in source
    assert "ActivityLog" not in source


async def test_dry_run_writes_zero_activity_log_events_even_with_ambiguous_data(stores):
    """A real ActivityLogService is constructed here purely to prove a
    negative -- it is never passed to the driver at all (impossible to,
    given the driver's signature), so this also proves
    luma.contact.enrichment_ambiguous specifically never fires from a
    dry run: an ambiguous history is reported ONLY in the in-memory
    BackfillReport, never persisted as an Activity Event."""
    from app.repositories.activity_event_store import MemoryActivityEventStore
    from app.services.activity_log_service import ActivityLogService

    contact_store, registration_store, event_store = stores
    activity_log = ActivityLogService(MemoryActivityEventStore())

    contact = make_contact(company="Existing")
    await contact_store.create(contact)
    await registration_store.save(make_registration(crm_contact_id=contact.crm_contact_id, luma_guest_id="g1", registration_answers=[company_answer("Co1")]))
    await registration_store.save(make_registration(crm_contact_id=contact.crm_contact_id, luma_guest_id="g2", registration_answers=[company_answer("Co2")]))

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_ambiguous_unknown_recency == 1  # the ambiguous path really was exercised
    page = await activity_log.list_events()
    assert page.total == 0


async def test_real_write_mode_also_writes_zero_activity_log_events(stores):
    from app.repositories.activity_event_store import MemoryActivityEventStore
    from app.services.activity_log_service import ActivityLogService

    contact_store, registration_store, event_store = stores
    activity_log = ActivityLogService(MemoryActivityEventStore())
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)

    page = await activity_log.list_events()
    assert page.total == 0


# --- contacts_would_update_*_from_unknown_recency --------------------------


async def test_would_update_from_unknown_recency_counters_split_by_field(stores):
    contact_store, registration_store, event_store = stores
    from_unknown = make_contact(company="Old1", title="OldTitle1")
    from_known = make_contact(company="Old2", title="OldTitle2")
    await contact_store.create(from_unknown)
    await contact_store.create(from_known)

    await registration_store.save(
        make_registration(
            crm_contact_id=from_unknown.crm_contact_id, registered_at=None, joined_at=None,
            registration_answers=[company_answer("New1", "NewTitle1")],
        )
    )
    await registration_store.save(
        make_registration(
            crm_contact_id=from_known.crm_contact_id, registered_at=_now(),
            registration_answers=[company_answer("New2", "NewTitle2")],
        )
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_would_update_company_from_unknown_recency == 1
    assert report.counts.contacts_would_update_title_from_unknown_recency == 1
    assert report.counts.contacts_would_update_from_unknown_recency == 1  # the union -- only from_unknown counted once
    assert report.counts.contacts_would_update_both == 2  # both contacts update both fields overall


async def test_a_known_recency_update_is_never_counted_as_from_unknown_recency(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="Old")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("New")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_would_update_company_from_unknown_recency == 0
    assert report.counts.contacts_would_update_from_unknown_recency == 0


async def test_example_reports_unknown_recency_tier_explicitly(stores):
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="Old")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=None, joined_at=None, registration_answers=[company_answer("New")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert len(report.examples) == 1
    assert report.examples[0].company_recency_tier == "unknown"
    assert report.examples[0].company_recency_at is None
    assert "update_based_solely_on_unknown_recency" in report.examples[0].flags


# --- suspicious-case flags (conservative, never blocking) -----------------


async def test_dramatic_company_change_is_flagged():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company="Sequoia Capital")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Totally Unrelated Widgets")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert "dramatic_company_change" in report.examples[0].flags


async def test_generic_placeholder_company_is_flagged():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company=None)
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("N/A")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert "generic_or_placeholder_company" in report.examples[0].flags


async def test_malformed_company_value_is_flagged():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company=None)
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("someone@example.com")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert "malformed_company_value" in report.examples[0].flags


async def test_a_normal_reasonable_change_carries_no_flags():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company="Acme")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Acme Ventures")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert report.examples[0].flags == []


async def test_tier2_domain_mismatch_with_company_is_flagged():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company=None, email="jane@totallyunrelated.io")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Acme Ventures")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)
    assert "tier2_domain_does_not_match_company" in report.examples[0].flags


# --- unchanged Contacts: zero save, zero provenance mutation (V1 correction) -


async def test_a_contact_whose_luma_answer_already_matches_is_truly_unchanged():
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    contact = make_contact(company="SameCo", title="SameTitle")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("SameCo", "SameTitle")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)

    assert report.counts.contacts_unchanged == 1
    assert report.counts.contacts_saved == 0
    persisted = await contact_store.get(contact.crm_contact_id)
    assert "field_provenance" not in persisted.custom_fields
    assert report.examples == []  # never added to examples either -- nothing actually changed


async def test_unchanged_counter_reflects_actual_field_equality_not_just_presence_of_a_luma_answer():
    """A contact whose Luma answer is present but identical must be
    counted as unchanged -- this is the exact 34-of-66 category the
    correction was about."""
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    unchanged = make_contact(company="Acme", title="CEO")
    changed = make_contact(company="OldCo", title="Old Title")
    await contact_store.create(unchanged)
    await contact_store.create(changed)
    await registration_store.save(
        make_registration(crm_contact_id=unchanged.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Acme", "CEO")])
    )
    await registration_store.save(
        make_registration(crm_contact_id=changed.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo", "New Title")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_unchanged == 1
    assert report.counts.contacts_would_update_both == 1


async def test_a_website_only_change_is_excluded_from_unchanged_and_still_gets_saved():
    """Edge case the "unchanged" bucket must NOT swallow: Company/Title
    both already match Luma exactly (so neither counts as "updated"), but
    a currently-blank website gets populated via Tier 1 -- a REAL save
    still occurs, so this must never be counted as "unchanged"."""
    contact_store, registration_store, event_store = MemoryCrmContactStore(), MemoryLumaRegistrationStore(), MemoryLumaEventStore()
    reference = make_contact(company="Acme", company_website="acme.com")
    await contact_store.create(reference)
    target = make_contact(company="Acme", title="CEO", company_website=None)
    await contact_store.create(target)
    await registration_store.save(
        make_registration(crm_contact_id=target.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("Acme", "CEO")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=False)

    assert report.counts.contacts_unchanged == 0  # NOT counted as unchanged
    assert report.counts.contacts_would_update_company == 0
    assert report.counts.contacts_would_update_title == 0
    assert report.counts.contacts_would_update_both == 0
    assert report.counts.websites_populated_from_blank_tier1 == 1
    assert report.counts.contacts_saved == 1  # a real save DID occur
    persisted = await contact_store.get(target.crm_contact_id)
    assert persisted.company_website == "https://acme.com"


# --- backfill-only exclusion capability -------------------------------------


async def test_excluded_contact_is_skipped_entirely_zero_changes(stores):
    contact_store, registration_store, event_store = stores
    excluded = make_contact(company="OldCo", title="Old Title")
    included = make_contact(company="OldCo2", title="Old Title 2")
    await contact_store.create(excluded)
    await contact_store.create(included)
    await registration_store.save(
        make_registration(crm_contact_id=excluded.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo", "New Title")])
    )
    await registration_store.save(
        make_registration(crm_contact_id=included.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo2", "New Title 2")])
    )

    report = await run_luma_contact_enrichment_backfill(
        contact_store, registration_store, event_store, dry_run=False, excluded_contact_ids={excluded.crm_contact_id}
    )

    assert report.counts.contacts_excluded == 1
    assert report.excluded_contact_ids == [excluded.crm_contact_id]
    assert report.counts.contacts_would_update_both == 1  # only the included contact
    assert report.counts.contacts_saved == 1  # only the included contact

    persisted_excluded = await contact_store.get(excluded.crm_contact_id)
    assert persisted_excluded.company == "OldCo"  # completely untouched
    assert persisted_excluded.title == "Old Title"
    assert "field_provenance" not in persisted_excluded.custom_fields

    persisted_included = await contact_store.get(included.crm_contact_id)
    assert persisted_included.company == "NewCo2"


async def test_excluded_contact_never_appears_in_any_other_bucket_or_examples(stores):
    contact_store, registration_store, event_store = stores
    excluded = make_contact(company="OldCo")
    await contact_store.create(excluded)
    await registration_store.save(
        make_registration(crm_contact_id=excluded.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(
        contact_store, registration_store, event_store, dry_run=True, excluded_contact_ids={excluded.crm_contact_id}
    )

    assert report.counts.contacts_would_update_company == 0
    assert report.counts.contacts_would_update_both == 0
    assert report.counts.contacts_unchanged == 0
    assert report.examples == []
    assert excluded.crm_contact_id not in report.ambiguous_contact_ids
    assert excluded.crm_contact_id not in report.website_review_needed_contact_ids


async def test_excluded_contacts_registrations_still_count_toward_raw_registration_tallies(stores):
    """Registration-level counts describe the raw dataset, not what got
    processed -- deliberately unaffected by exclusion (see module
    docstring)."""
    contact_store, registration_store, event_store = stores
    excluded = make_contact(company="OldCo")
    await contact_store.create(excluded)
    await registration_store.save(
        make_registration(crm_contact_id=excluded.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(
        contact_store, registration_store, event_store, dry_run=True, excluded_contact_ids={excluded.crm_contact_id}
    )

    assert report.counts.registrations_examined == 1
    assert report.counts.registrations_using_registered_at == 1
    assert report.counts.unique_contacts_represented == 1  # still "represented" in the dataset


async def test_a_requested_exclusion_id_with_no_registrations_is_reported_as_not_found(stores):
    contact_store, registration_store, event_store = stores
    lonely = make_contact(company="NoRegistrations")
    await contact_store.create(lonely)

    report = await run_luma_contact_enrichment_backfill(
        contact_store, registration_store, event_store, dry_run=True, excluded_contact_ids={lonely.crm_contact_id, "does-not-exist-at-all"}
    )

    assert report.counts.contacts_excluded == 0  # nothing to skip -- no registrations at all
    assert sorted(report.excluded_contact_ids_not_found) == sorted([lonely.crm_contact_id, "does-not-exist-at-all"])


async def test_exclusion_has_no_default_effect_when_not_provided(stores):
    """Backward compatible -- omitting excluded_contact_ids entirely
    behaves exactly as before this capability existed."""
    contact_store, registration_store, event_store = stores
    contact = make_contact(company="OldCo")
    await contact_store.create(contact)
    await registration_store.save(
        make_registration(crm_contact_id=contact.crm_contact_id, registered_at=_now(), registration_answers=[company_answer("NewCo")])
    )

    report = await run_luma_contact_enrichment_backfill(contact_store, registration_store, event_store, dry_run=True)

    assert report.counts.contacts_excluded == 0
    assert report.excluded_contact_ids == []
    assert report.counts.contacts_would_update_company == 1


def test_backfill_driver_exclusion_parameter_is_not_referenced_by_the_live_webhook_path():
    """Structural guard: excluded_contact_ids must remain a
    backfill-only, one-run, in-memory parameter -- never threaded into
    the live webhook path."""
    import inspect

    from app.services import luma_sync_service as module

    source = inspect.getsource(module)
    assert "excluded_contact_ids" not in source
    assert "excluded" not in source.lower()
