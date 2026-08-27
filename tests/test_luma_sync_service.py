"""
LumaSyncService -- the core Luma-guest-payload -> CRM processing path
shared by the webhook handler and the historical backfill. Exercised
against REAL in-memory CrmService/store instances (never mocks of the
matching/merge engine itself), so these tests prove the actual
classify_match()/apply_import_mapping() behavior this module deliberately
reuses unmodified, not just that this module calls them.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.activity import ActivityCategory
from app.models.crm import CrmContact, CrmCustomFieldDefinition, CustomFieldType
from app.models.luma import LumaApprovalStatus, LumaMatchStatus, LumaQuestionMapping
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services.crm_service import CrmService
from app.services.luma_sync_service import LumaSyncError, LumaSyncService

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_contact(**overrides) -> CrmContact:
    defaults = dict(crm_contact_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now())
    defaults.update(overrides)
    return CrmContact(**defaults)


def make_mapping(**overrides) -> LumaQuestionMapping:
    defaults = dict(
        luma_question_mapping_id=str(uuid.uuid4()),
        question_label="LinkedIn Profile",
        question_type=None,
        target_field_key="linkedin_url",
        extract_key=None,
        active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return LumaQuestionMapping(**defaults)


def make_event(event_id="evt-1", name="Hotshot Dinner", **overrides) -> dict:
    base = {
        "id": event_id,
        "calendar_id": "cal-1",
        "name": name,
        "start_at": "2026-09-01T18:00:00Z",
        "end_at": "2026-09-01T21:00:00Z",
        "url": f"https://lu.ma/{event_id}",
    }
    base.update(overrides)
    return base


def make_guest(
    guest_id="gst-1",
    email="alice@example.com",
    first_name="Alice",
    last_name="Angel",
    approval_status="approved",
    registration_answers=None,
    event_tickets=None,
    **overrides,
) -> dict:
    base = {
        "id": guest_id,
        "user_email": email,
        "user_first_name": first_name,
        "user_last_name": last_name,
        "user_name": f"{first_name} {last_name}",
        "phone_number": None,
        "approval_status": approval_status,
        "registered_at": "2026-08-20T10:00:00Z",
        "invited_at": None,
        "joined_at": None,
        "utm_source": None,
        "registration_answers": registration_answers or [],
        "event_tickets": event_tickets if event_tickets is not None else [],
    }
    base.update(overrides)
    return base


@pytest_asyncio.fixture
async def crm_service():
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="investor_type",
            label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["Angel Investor", "Family Office"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return CrmService(custom_field_store=custom_field_store)


@pytest.fixture
def mapping_store():
    return MemoryLumaQuestionMappingStore()


@pytest.fixture
def event_store():
    return MemoryLumaEventStore()


@pytest.fixture
def registration_store():
    return MemoryLumaRegistrationStore()


@pytest.fixture
def luma_service(crm_service, event_store, registration_store, mapping_store):
    return LumaSyncService(
        crm_service=crm_service,
        event_store=event_store,
        registration_store=registration_store,
        mapping_store=mapping_store,
        activity_log=crm_service.activity_log,
    )


async def _seed_mapping(mapping_store, **overrides):
    mapping = make_mapping(**overrides)
    await mapping_store.create(mapping)
    return mapping


# --- new / existing / possible-duplicate contact matching -------------------


async def test_new_contact_is_created_from_a_luma_registration(luma_service, crm_service):
    result = await luma_service.process_guest_event(make_event(), make_guest())

    assert result.contact_outcome == "created"
    assert result.contact is not None
    assert result.contact.source == "luma"
    assert result.contact.email == "alice@example.com"
    assert result.registration.crm_contact_id == result.contact.crm_contact_id
    assert result.registration.match_status == LumaMatchStatus.MATCHED

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1


async def test_confident_email_match_attaches_to_the_existing_contact(luma_service, crm_service):
    existing = make_contact(email="alice@example.com", first_name="Alice", last_name="Angel")
    await crm_service.contact_store.create(existing)

    result = await luma_service.process_guest_event(make_event(), make_guest(email="alice@example.com"))

    assert result.registration.crm_contact_id == existing.crm_contact_id
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1  # no duplicate created


async def test_possible_duplicate_never_attaches_or_creates(luma_service, crm_service, mapping_store):
    """name+company fallback tier -- never confident, must not auto-attach
    and must not auto-create a second contact for the same real person."""
    await _seed_mapping(
        mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company"
    )
    existing = make_contact(first_name="Alice", last_name="Angel", company="Acme Ventures", email="alice@work.com")
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        email="alice@totally-different-address.com",  # no email match at all
        first_name="Alice",
        last_name="Angel",
        registration_answers=[
            {"label": "Company", "question_id": "q-1", "question_type": "company", "value": {"company": "Acme Ventures"}}
        ],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact_outcome == "needs_review"
    assert result.registration.crm_contact_id is None
    assert result.registration.match_status == LumaMatchStatus.NEEDS_REVIEW

    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 1  # still just the original -- no new contact created
    unchanged = await crm_service.contact_store.get(existing.crm_contact_id)
    assert unchanged.email == "alice@work.com"  # untouched


async def test_luma_personal_email_scenario_creates_a_new_contact_rather_than_guessing(luma_service, crm_service):
    """The documented, accepted limitation: a returning contact registers
    on Luma with a personal email, and the event's form didn't ask for
    company, so the name+company fallback tier can't even evaluate (it
    requires company to be present). Result is a legitimate NEW contact,
    NOT a wrongful auto-merge -- this test documents that this is a real
    new contact, not silently attached to the wrong person."""
    existing = make_contact(first_name="Bob", last_name="Builder", email="bob@acmeventures.com", company="Acme Ventures")
    await crm_service.contact_store.create(existing)

    guest = make_guest(guest_id="gst-2", email="bob.personal@gmail.com", first_name="Bob", last_name="Builder")
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact_outcome == "created"
    assert result.contact.crm_contact_id != existing.crm_contact_id
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2  # a real, known limitation -- not a merge, not data corruption either


async def test_no_fuzzy_name_matching_ever_occurs(luma_service, crm_service, mapping_store):
    """A near-miss name (Jon vs John) with the SAME company must never be
    treated as a confident match -- classify_match is exact-normalized
    only, never fuzzy."""
    await _seed_mapping(
        mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company"
    )
    existing = make_contact(first_name="John", last_name="Smith", company="Acme Ventures", email="john@acme.com")
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        email="jon@somewhere-else.com",
        first_name="Jon",  # deliberately NOT an exact match to "John"
        last_name="Smith",
        registration_answers=[
            {"label": "Company", "question_id": "q-1", "question_type": "company", "value": {"company": "Acme Ventures"}}
        ],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    # Not a confident match (name_company requires exact normalized equality),
    # and no name+company fallback flag either since "jon" != "john" exactly --
    # this simply falls through to NEW, never guessed into either existing record.
    assert result.contact_outcome == "created"
    all_contacts = await crm_service.contact_store.list()
    assert len(all_contacts) == 2
    unchanged = await crm_service.contact_store.get(existing.crm_contact_id)
    assert unchanged.email == "john@acme.com"  # original never touched


# --- enrichment / merge rules (CrmService.apply_import_mapping, unmodified) -


async def test_existing_scalar_field_is_never_overwritten(luma_service, crm_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company"
    )
    existing = make_contact(email="alice@example.com", company="Sequoia Capital")
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q-1", "question_type": "company", "value": {"company": "Sequoia"}}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Sequoia Capital"  # NOT overwritten by the conflicting "Sequoia"


async def test_blank_crm_field_is_enriched(luma_service, crm_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company"
    )
    existing = make_contact(email="alice@example.com", company=None)
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q-1", "question_type": "company", "value": {"company": "Acme Ventures"}}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact_outcome == "enriched"
    assert result.contact.company == "Acme Ventures"
    assert "company" in result.changed_field_keys


async def test_multi_select_custom_field_union_merges_rather_than_overwrites(luma_service, crm_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select", target_field_key="custom:investor_type"
    )
    existing = make_contact(email="alice@example.com", custom_fields={"investor_type": ["Family Office"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investor_type"] == ["Family Office", "Angel Investor"]
    assert result.contact_outcome == "enriched"


async def test_blank_luma_value_never_erases_an_existing_crm_value(luma_service, crm_service):
    existing = make_contact(email="alice@example.com", phone="+15551234567")
    await crm_service.contact_store.create(existing)

    guest = make_guest(phone_number=None)  # Luma didn't collect a phone this time
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.phone == "+15551234567"


async def test_new_luma_contact_source_is_luma(luma_service):
    result = await luma_service.process_guest_event(make_event(), make_guest())
    assert result.contact.source == "luma"


async def test_source_on_an_existing_contact_is_never_overwritten(luma_service, crm_service):
    existing = make_contact(email="alice@example.com", source="itf")
    await crm_service.contact_store.create(existing)

    result = await luma_service.process_guest_event(make_event(), make_guest())

    assert result.contact.source == "itf"  # never touched -- CREATE_ONLY_FIELD_NAMES


# --- question mapping layer --------------------------------------------------


async def test_unmapped_registration_answer_is_preserved_but_never_applied(luma_service, crm_service):
    guest = make_guest(
        registration_answers=[{"label": "Favorite Color", "question_id": "q-9", "question_type": "text", "value": "Blue"}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    stored_labels = [a.label for a in result.registration.registration_answers]
    assert "Favorite Color" in stored_labels
    # Never applied to the contact -- there's no field it could possibly map to.
    assert "Blue" not in (result.contact.model_dump_json())


async def test_mapped_question_populates_the_target_crm_field(luma_service, crm_service, mapping_store):
    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url")
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "https://linkedin.com/in/alice"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://linkedin.com/in/alice"


async def test_company_question_extract_key_splits_company_and_job_title(luma_service, crm_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company"
    )
    await _seed_mapping(
        mapping_store,
        luma_question_mapping_id=str(uuid.uuid4()),
        question_label="Company",
        question_type="company",
        target_field_key="title",
        extract_key="job_title",
    )
    guest = make_guest(
        registration_answers=[
            {
                "label": "Company",
                "question_id": "q-1",
                "question_type": "company",
                "value": {"company": "Acme Ventures", "job_title": "Partner"},
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Acme Ventures"
    assert result.contact.title == "Partner"


async def test_inactive_mapping_is_not_applied(luma_service, crm_service, mapping_store):
    await _seed_mapping(mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", active=False)
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "https://linkedin.com/in/alice"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url is None


# --- status / check-in --------------------------------------------------------


async def test_status_transition_updates_the_same_registration_not_a_new_one(luma_service, registration_store):
    await luma_service.process_guest_event(make_event(), make_guest(approval_status="pending_approval"))
    result2 = await luma_service.process_guest_event(make_event(), make_guest(approval_status="approved"))

    all_regs = await registration_store.list()
    assert len(all_regs) == 1  # same registration, not a second one
    assert result2.registration.approval_status == LumaApprovalStatus.APPROVED


async def test_check_in_transition_is_recorded(luma_service, crm_service):
    await luma_service.process_guest_event(
        make_event(), make_guest(event_tickets=[{"id": "tix-1", "checked_in_at": None}])
    )
    result2 = await luma_service.process_guest_event(
        make_event(), make_guest(event_tickets=[{"id": "tix-1", "checked_in_at": "2026-09-01T18:05:00Z"}])
    )

    assert result2.registration.checked_in_at is not None
    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    checkin_events = [e for e in page.items if e.event_type == "luma.registration.checked_in"]
    assert len(checkin_events) == 1


# --- idempotency ---------------------------------------------------------------


async def test_repeated_identical_payload_produces_no_additional_activity_log_events(luma_service, crm_service):
    await luma_service.process_guest_event(make_event(), make_guest())
    page_after_first = await crm_service.activity_log.list_events()
    count_after_first = page_after_first.total

    await luma_service.process_guest_event(make_event(), make_guest())  # identical, reprocessed
    page_after_second = await crm_service.activity_log.list_events()

    assert page_after_second.total == count_after_first  # no new noise


async def test_registration_idempotency_same_guest_id_never_duplicates(luma_service, registration_store):
    for _ in range(3):
        await luma_service.process_guest_event(make_event(), make_guest())

    all_regs = await registration_store.list()
    assert len(all_regs) == 1


async def test_duplicate_webhook_delivery_id_is_a_pure_no_op(luma_service, crm_service):
    first = await luma_service.process_guest_event(make_event(), make_guest(approval_status="pending_approval"), webhook_delivery_id="wh-1")
    assert first.duplicate_delivery is False

    page_after_first = await crm_service.activity_log.list_events()
    count_after_first = page_after_first.total

    # Same delivery id, even with DIFFERENT data -- proves it's skipped
    # purely because the delivery id matches, not because the data happens
    # to be unchanged.
    second = await luma_service.process_guest_event(
        make_event(), make_guest(approval_status="declined"), webhook_delivery_id="wh-1"
    )
    assert second.duplicate_delivery is True
    assert second.registration.approval_status == LumaApprovalStatus.PENDING_APPROVAL  # unchanged -- never processed

    page_after_second = await crm_service.activity_log.list_events()
    assert page_after_second.total == count_after_first


async def test_a_new_webhook_delivery_id_does_process_a_real_change(luma_service):
    await luma_service.process_guest_event(make_event(), make_guest(approval_status="pending_approval"), webhook_delivery_id="wh-1")
    second = await luma_service.process_guest_event(
        make_event(), make_guest(approval_status="approved"), webhook_delivery_id="wh-2"
    )
    assert second.duplicate_delivery is False
    assert second.registration.approval_status == LumaApprovalStatus.APPROVED


# --- webhook routing (handle_webhook) -------------------------------------


async def test_handle_webhook_guest_registered(luma_service):
    data = {**make_guest(), "event": make_event()}
    result = await luma_service.handle_webhook("guest.registered", data, webhook_delivery_id="wh-1")
    assert result is not None
    assert result.contact_outcome == "created"


async def test_handle_webhook_guest_updated(luma_service):
    data = {**make_guest(approval_status="pending_approval"), "event": make_event()}
    await luma_service.handle_webhook("guest.registered", data, webhook_delivery_id="wh-1")

    data2 = {**make_guest(approval_status="approved"), "event": make_event()}
    result2 = await luma_service.handle_webhook("guest.updated", data2, webhook_delivery_id="wh-2")
    assert result2.registration.approval_status == LumaApprovalStatus.APPROVED


async def test_handle_webhook_guest_refunded(luma_service):
    data = {**make_guest(), "event": make_event(), "refund": {"amount": 5000, "currency": "usd"}}
    result = await luma_service.handle_webhook("guest.refunded", data, webhook_delivery_id="wh-1")
    assert result is not None


async def test_handle_webhook_ticket_registered(luma_service):
    data = {
        **make_guest(event_tickets=[{"id": "tix-1", "checked_in_at": None}]),
        "event": make_event(),
        "event_ticket": {"id": "tix-1", "checked_in_at": None},
    }
    result = await luma_service.handle_webhook("ticket.registered", data, webhook_delivery_id="wh-1")
    assert result is not None
    assert len(result.registration.event_tickets) == 1


async def test_unsupported_webhook_type_is_ignored(luma_service, crm_service):
    result = await luma_service.handle_webhook("event.created", {"id": "evt-1", "name": "Something"}, webhook_delivery_id="wh-1")
    assert result is None
    assert (await crm_service.contact_store.list()) == []


async def test_guest_refunded_without_embedded_event_recovers_from_prior_registration(luma_service):
    registered_data = {**make_guest(guest_id="gst-5"), "event": make_event(event_id="evt-5")}
    await luma_service.handle_webhook("guest.registered", registered_data, webhook_delivery_id="wh-1")

    refund_data = {**make_guest(guest_id="gst-5"), "refund": {"amount": 1000, "currency": "usd"}}  # no "event" key
    result = await luma_service.handle_webhook("guest.refunded", refund_data, webhook_delivery_id="wh-2")

    assert result is not None
    assert result.registration.luma_event_id == "evt-5"


async def test_webhook_with_no_event_and_no_prior_registration_raises(luma_service):
    data = {**make_guest(guest_id="gst-never-seen")}  # no "event" key, never registered before
    with pytest.raises(LumaSyncError):
        await luma_service.handle_webhook("guest.registered", data, webhook_delivery_id="wh-1")


# --- structural: registration answers stay structured JSON -----------------


async def test_event_history_is_never_collapsed_into_a_single_text_field(luma_service):
    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "dropdown", "value": "Angel Investor"},
            {"label": "Check Size", "question_id": "q-2", "question_type": "dropdown", "value": "$100k-$250k"},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert len(result.registration.registration_answers) == 2
    assert result.registration.registration_answers[0].label == "Investor Type"
    assert result.registration.registration_answers[0].value == "Angel Investor"


# --- LinkedIn normalizer, end-to-end through the mapping pipeline ----------


async def test_linkedin_relative_path_is_normalized_end_to_end(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "/in/john-adrian-c-9ba98176"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://www.linkedin.com/in/john-adrian-c-9ba98176"


async def test_linkedin_full_url_is_normalized_end_to_end(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "https://www.linkedin.com/in/alice"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://www.linkedin.com/in/alice"


async def test_linkedin_missing_scheme_is_normalized_end_to_end(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "linkedin.com/in/bob"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://www.linkedin.com/in/bob"


async def test_invalid_linkedin_answer_never_populates_the_field(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "not a linkedin url"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url is None


async def test_blank_linkedin_answer_never_populates_the_field(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    guest = make_guest(
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "   "}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url is None


async def test_normalizer_never_overwrites_an_existing_nonblank_linkedin_url(luma_service, crm_service, mapping_store):
    """The normalizer only changes how a value is COMPUTED -- the fill-only
    merge rule is completely unaffected, still enforced by
    apply_import_mapping() exactly as for any other field."""
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url", normalizer="linkedin_url"
    )
    existing = make_contact(email="alice@example.com", linkedin_url="https://www.linkedin.com/in/already-set")
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "/in/someone-else"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url == "https://www.linkedin.com/in/already-set"


async def test_inactive_normalized_mapping_is_ignored_by_ingestion(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="LinkedIn Profile", target_field_key="linkedin_url",
        normalizer="linkedin_url", active=False,
    )
    guest = make_guest(
        registration_answers=[
            {"label": "LinkedIn Profile", "question_id": "q-1", "question_type": "linkedin", "value": "/in/alice"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.linkedin_url is None


# --- no hardcoded Luma question label anywhere in ingestion logic ----------


def test_no_hardcoded_luma_question_label_in_ingestion_source():
    import inspect

    from app.services import luma_answer_normalizers, luma_sync_service

    for module in (luma_sync_service, luma_answer_normalizers):
        source = inspect.getsource(module)
        for hardcoded in ["What is your LinkedIn profile", "What company do you work for", "What type of investor are you"]:
            assert hardcoded not in source, f"{module.__name__} hardcodes a real Luma question label: {hardcoded!r}"
