"""
Identity-field conflict handling -- see LumaSyncService._detect_identity_conflicts()/
_enrich_existing_contact(). Root cause: an existing CRM contact matched via
email can have OTHER unique dedup-tier fields (apollo_contact_id,
linkedin_url) that a Luma enrichment would try to fill in, but that new
value might already belong to a DIFFERENT existing contact -- discovered
live in production (two real CrmContacts for the same person, one via ITF
under a personal email, one via Luma under a work email, sharing one real
LinkedIn URL). Before this fix, the resulting UNIQUE-constraint violation
crashed the whole webhook and rolled back EVERY field, including
non-conflicting ones (company/title/investor_type).
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncService
from tests.test_luma_sync_service import make_event, make_guest, make_mapping

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


@pytest_asyncio.fixture
async def crm_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()), field_key="investor_type", label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT, options=["Angel Investor"], active=True,
            created_at=_now(), updated_at=_now(),
        )
    )
    return CrmService(custom_field_store=custom_field_store)


@pytest.fixture
def mapping_store():
    return MemoryLumaQuestionMappingStore()


@pytest.fixture
def luma_service(crm_service, mapping_store):
    return LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=mapping_store,
        activity_log=crm_service.activity_log,
    )


async def _seed_mapping(mapping_store, **overrides):
    mapping = make_mapping(**overrides)
    await mapping_store.create(mapping)
    return mapping


# --- 1. LinkedIn conflict -- the exact real production scenario -------------


async def test_linkedin_conflict_is_skipped_while_other_fields_still_enrich(luma_service, crm_service, mapping_store):
    other = make_contact(email="other@example.com", linkedin_url="https://www.linkedin.com/in/john-adrian-c-9ba98176")
    await crm_service.contact_store.create(other)
    ours = make_contact(email="johnadriancal@astronomic.com", first_name="John", last_name="Cal")
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Company", question_type="company", target_field_key="title", extract_key="job_title")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Investor Type", question_type="multi-select", target_field_key="custom:investor_type")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")

    guest = make_guest(
        email="johnadriancal@astronomic.com",
        first_name="John",  # must match `ours`'s name, else classify_match's
        last_name="Cal",  # _conflicts_on_identity downgrades this to POSSIBLE_DUPLICATE
        registration_answers=[
            {"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Astronomic", "job_title": "Investor"}},
            {"label": "Investor Type", "question_id": "q2", "question_type": "multi-select", "value": ["Angel Investor"]},
            {"label": "LinkedIn Profile", "question_id": "q3", "question_type": "linkedin", "value": "/in/john-adrian-c-9ba98176"},
        ],
    )

    result = await luma_service.process_guest_event(make_event(), guest)  # must not raise

    assert result.contact.company == "Astronomic"
    assert result.contact.title == "Investor"
    assert result.contact.custom_fields["investor_type"] == ["Angel Investor"]
    assert result.contact.linkedin_url is None  # NOT applied -- conflicts with `other`
    assert result.contact_outcome == "enriched"
    assert set(result.changed_field_keys) == {"company", "title", "custom:investor_type"}
    assert result.identity_conflicts == {"linkedin_url": other.crm_contact_id}

    # `other` is completely untouched -- no merge, no overwrite, no deletion.
    unchanged_other = await crm_service.contact_store.get(other.crm_contact_id)
    assert unchanged_other.linkedin_url == "https://www.linkedin.com/in/john-adrian-c-9ba98176"
    assert unchanged_other.email == "other@example.com"

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2  # no merge, no delete

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    conflict_events = [e for e in page.items if e.event_type == "luma.contact.identity_conflict"]
    assert len(conflict_events) == 1
    assert conflict_events[0].metadata["conflicting_fields"] == ["linkedin_url"]
    assert conflict_events[0].metadata["conflicting_contact_ids"] == {"linkedin_url": other.crm_contact_id}


# --- 2. Email conflict -------------------------------------------------------


async def test_email_conflict_is_detected(luma_service, crm_service):
    """Direct unit test of `_detect_identity_conflicts()` for the email
    field -- the exact mechanism `_enrich_existing_contact()` relies on.

    (Note on why this is tested at the detector level rather than
    end-to-end through process_guest_event(): classify_match()'s tier 1
    is always email, and a Luma guest's mapped email always equals its
    native `user_email` -- so a guest whose email already belongs to
    `other` would be classified as EXISTING-matching-`other` directly,
    which is a legitimate match, not a conflict. A genuine "matched via a
    different tier, but the enrichment's email would collide" scenario
    can still arise (e.g. a contact matched by apollo_contact_id/linkedin
    with no email on file yet, later enriched by some field that sets
    email to a value already claimed elsewhere) -- this proves the
    detector itself catches exactly that shape correctly.)"""
    other = make_contact(email="taken@example.com")
    await crm_service.contact_store.create(other)
    ours = make_contact(email=None, apollo_contact_id="apollo-123")
    await crm_service.contact_store.create(ours)

    conflicts = await luma_service._detect_identity_conflicts(
        ours, ours.model_copy(update={"email": "taken@example.com"})
    )
    assert conflicts == {"email": other.crm_contact_id}


# --- 3. Apollo contact ID conflict -------------------------------------------


async def test_apollo_contact_id_conflict_is_skipped(luma_service, crm_service, mapping_store):
    other = make_contact(email="apollo-owner@example.com", apollo_contact_id="apollo-999")
    await crm_service.contact_store.create(other)
    ours = make_contact(email="ours@example.com", apollo_contact_id=None, company=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="Apollo ID", target_field_key="apollo_contact_id")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Company", question_type="company", target_field_key="company", extract_key="company")

    guest = make_guest(
        email="ours@example.com",
        registration_answers=[
            {"label": "Apollo ID", "question_id": "q1", "question_type": "text", "value": "apollo-999"},
            {"label": "Company", "question_id": "q2", "question_type": "company", "value": {"company": "Acme"}},
        ],
    )

    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.apollo_contact_id is None  # not applied -- conflicts
    assert result.contact.company == "Acme"  # non-conflicting field still applied
    assert result.identity_conflicts == {"apollo_contact_id": other.crm_contact_id}

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2


# --- 4. Non-conflicting identity enrichment still works ---------------------


async def test_non_conflicting_linkedin_enrichment_applies_normally(luma_service, crm_service, mapping_store):
    ours = make_contact(email="alice@example.com", linkedin_url=None)
    await crm_service.contact_store.create(ours)
    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")

    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/alice-nobody-else-has"}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://www.linkedin.com/in/alice-nobody-else-has"
    assert result.identity_conflicts == {}
    assert result.contact_outcome == "enriched"


# --- 5. Same value on the same contact is not a conflict --------------------


async def test_reprocessing_the_same_already_set_value_is_not_a_conflict(luma_service, crm_service, mapping_store):
    ours = make_contact(email="alice@example.com", linkedin_url="https://www.linkedin.com/in/alice")
    await crm_service.contact_store.create(ours)
    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")

    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/alice"}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.identity_conflicts == {}
    assert result.contact.linkedin_url == "https://www.linkedin.com/in/alice"


# --- 6. Multiple conflicting identity fields ---------------------------------


async def test_multiple_conflicting_identity_fields_all_suppressed(luma_service, crm_service, mapping_store):
    linkedin_owner = make_contact(email="linkedin-owner@example.com", linkedin_url="https://www.linkedin.com/in/shared")
    apollo_owner = make_contact(email="apollo-owner@example.com", apollo_contact_id="apollo-shared")
    await crm_service.contact_store.create(linkedin_owner)
    await crm_service.contact_store.create(apollo_owner)
    ours = make_contact(email="ours2@example.com", linkedin_url=None, apollo_contact_id=None, company=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Apollo ID", target_field_key="apollo_contact_id")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Company", question_type="company", target_field_key="company", extract_key="company")

    guest = make_guest(
        email="ours2@example.com",
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/shared"},
            {"label": "Apollo ID", "question_id": "q2", "question_type": "text", "value": "apollo-shared"},
            {"label": "Company", "question_id": "q3", "question_type": "company", "value": {"company": "Acme"}},
        ],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url is None
    assert result.contact.apollo_contact_id is None
    assert result.contact.company == "Acme"
    assert result.identity_conflicts == {
        "linkedin_url": linkedin_owner.crm_contact_id,
        "apollo_contact_id": apollo_owner.crm_contact_id,
    }
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 3  # ours + the two owners -- nothing merged/deleted


# --- 7. luma.contact.enriched lists only successfully changed fields --------


async def test_enriched_event_excludes_the_conflicting_field(luma_service, crm_service, mapping_store):
    other = make_contact(email="other3@example.com", linkedin_url="https://www.linkedin.com/in/conflict-target")
    await crm_service.contact_store.create(other)
    # first/last name pre-set to match the guest's defaults (Alice Angel) so
    # ONLY "company" is actually a change -- isolates the assertion to what
    # this test is actually about.
    ours = make_contact(email="ours3@example.com", first_name="Alice", last_name="Angel", company=None, linkedin_url=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")
    await _seed_mapping(mapping_store, luma_question_mapping_id=str(uuid.uuid4()), question_label="Company", question_type="company", target_field_key="company", extract_key="company")

    guest = make_guest(
        email="ours3@example.com",
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/conflict-target"},
            {"label": "Company", "question_id": "q2", "question_type": "company", "value": {"company": "Acme"}},
        ],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    enriched_events = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert len(enriched_events) == 1
    assert enriched_events[0].metadata["fields_updated"] == ["company"]  # linkedin_url excluded


# --- 8. conflict event metadata never carries raw values --------------------


async def test_conflict_event_metadata_never_contains_raw_values(luma_service, crm_service, mapping_store):
    other = make_contact(email="secret-owner@example.com", linkedin_url="https://www.linkedin.com/in/should-never-appear")
    await crm_service.contact_store.create(other)
    ours = make_contact(email="ours4@example.com", linkedin_url=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")
    guest = make_guest(
        email="ours4@example.com",
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/should-never-appear"}],
    )
    await luma_service.process_guest_event(make_event(), guest)

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    conflict_event = next(e for e in page.items if e.event_type == "luma.contact.identity_conflict")
    metadata_str = str(conflict_event.metadata)
    assert "should-never-appear" not in metadata_str
    assert "secret-owner@example.com" not in metadata_str
    assert "ours4@example.com" not in metadata_str
    # Only structural info: field keys + contact IDs.
    assert set(conflict_event.metadata.keys()) == {"conflicting_fields", "matched_contact_id", "conflicting_contact_ids"}


# --- 9. no contacts merged or deleted ----------------------------------------


async def test_no_contacts_merged_or_deleted_on_conflict(luma_service, crm_service, mapping_store):
    other = make_contact(email="other5@example.com", linkedin_url="https://www.linkedin.com/in/never-merged")
    await crm_service.contact_store.create(other)
    ours = make_contact(email="ours5@example.com", linkedin_url=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")
    guest = make_guest(
        email="ours5@example.com",
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/never-merged"}],
    )
    await luma_service.process_guest_event(make_event(), guest)

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2
    assert {c.crm_contact_id for c in all_contacts} == {other.crm_contact_id, ours.crm_contact_id}
    unchanged_other = await crm_service.contact_store.get(other.crm_contact_id)
    assert unchanged_other.email == "other5@example.com"
    assert unchanged_other.archived is False


# --- 10. registration status still updates during a conflict ----------------


async def test_registration_status_updates_despite_identity_conflict(luma_service, crm_service, mapping_store):
    other = make_contact(email="other6@example.com", linkedin_url="https://www.linkedin.com/in/still-updates")
    await crm_service.contact_store.create(other)
    ours = make_contact(email="ours6@example.com", linkedin_url=None)
    await crm_service.contact_store.create(ours)

    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url")

    guest_id = "gst-status-conflict"
    await luma_service.process_guest_event(
        make_event(),
        make_guest(
            guest_id=guest_id, email="ours6@example.com", approval_status="pending_approval",
            registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/still-updates"}],
        ),
    )

    result2 = await luma_service.process_guest_event(
        make_event(),
        make_guest(
            guest_id=guest_id, email="ours6@example.com", approval_status="approved",
            registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "/in/still-updates"}],
        ),
    )

    assert result2.registration.approval_status.value == "approved"  # updates despite the LinkedIn conflict persisting
    all_regs = await luma_service.registration_store.list()
    assert len(all_regs) == 1  # still just the one registration, not stale/duplicated
