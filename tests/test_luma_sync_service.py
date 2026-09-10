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
from app.services import luma_sync_service as luma_sync_service_module
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


_CHECK_SIZE_PERSONAL_OPTIONS = [
    "$1k - $10k", "$10k - $25k", "$25k - $50k", "$50k - $100k", "$100k - $250k",
    "$250k - $500k", "$500k - $1M", "$1M - $2M", "$2M - $5M", "$5M - $10M",
    "$10M+", "Other:",
]


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
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="check_size_personal",
            label="Check Size (Personal)",
            field_type=CustomFieldType.MULTI_SELECT,
            options=_CHECK_SIZE_PERSONAL_OPTIONS,
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="deploying_capital",
            label="Deploying Capital",
            field_type=CustomFieldType.SINGLE_SELECT,
            options=["Yes, actively", "Selectively", "Not at the moment"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="role",
            label="Role",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["Investor", "Founder", "CEO"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    # Matches live production exactly: investment_industry is deliberately
    # OPEN-ENDED (empty options -- a tag-style field, never a fixed
    # picklist, per its own live description "New values are accepted
    # automatically -- no fixed option list"). _filter_to_allowed_options()
    # now treats empty options as "no fixed field-level restriction" (see
    # that function's own docstring), so canonical-vs-arbitrary filtering
    # for Luma's Industry Focus question happens entirely in
    # normalize_industry_focus_labels(), not here.
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="investment_industry",
            label="Investment Industry",
            field_type=CustomFieldType.MULTI_SELECT,
            options=[],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    # A second, SYNTHETIC open-ended multi_select field with no normalizer
    # at all -- exists purely to prove the GENERIC empty-options-passthrough
    # mechanism in isolation, decoupled from investment_industry's own
    # normalizer. Not a real CRM field.
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="open_ended_test_field",
            label="Open Ended Test Field",
            field_type=CustomFieldType.MULTI_SELECT,
            options=[],
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


@pytest.fixture
def luma_contact_enrichment_enabled(monkeypatch):
    """Deliberately NOT autouse -- most tests in this file must keep
    testing this feature's OFF-by-default, byte-identical-to-before
    behavior. Only the self-report enrichment tests below request this
    explicitly."""
    monkeypatch.setattr(luma_sync_service_module.settings, "luma_contact_enrichment_enabled", True)


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
    """Uses "Department" rather than "Company"/"Title" deliberately --
    those two now have their OWN dedicated, deliberately-overwriting
    self-report path (see app/services/luma_contact_enrichment.py and
    Stage: Luma Contact Enrichment), so they're no longer a valid example
    of apply_import_mapping()'s generic fill-only rule this test exists
    to guard. "Department" has no such override and still demonstrates
    the exact same fill-only semantics unchanged."""
    await _seed_mapping(mapping_store, question_label="Department", question_type="text", target_field_key="department")
    existing = make_contact(email="alice@example.com", department="Growth")
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[{"label": "Department", "question_id": "q-1", "question_type": "text", "value": "Sales"}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.department == "Growth"  # NOT overwritten by the conflicting "Sales"


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


# --- Generic empty-options semantics: open field vs. fixed field -----------
#
# investment_industry's live CrmCustomFieldDefinition.options is
# deliberately empty (an intentionally open, tag-style field -- see the
# crm_service fixture's own comment). _filter_to_allowed_options() now
# treats empty options as "no fixed field-level restriction," matching the
# identical convention crm_filter_service.py already uses. These two tests
# prove that mechanism generically, decoupled from investment_industry's
# own normalizer.


async def test_open_ended_multi_select_field_with_empty_options_passes_values_through_unchanged(
    luma_service, mapping_store
):
    await _seed_mapping(
        mapping_store,
        question_label="Open Ended Question",
        question_type="multi-select",
        target_field_key="custom:open_ended_test_field",
    )
    guest = make_guest(
        registration_answers=[
            {
                "label": "Open Ended Question",
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Literally Anything", "No Fixed List Here"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["open_ended_test_field"] == ["Literally Anything", "No Fixed List Here"]


async def test_fixed_multi_select_field_with_populated_options_still_filters_unknown_values(
    luma_service, mapping_store
):
    """investor_type has real, populated options -- confirms the pre-
    existing filter-by-configured-options behavior is completely
    unchanged for any field that actually has options."""
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select", target_field_key="custom:investor_type"
    )
    guest = make_guest(
        registration_answers=[
            {
                "label": "Investor Type",
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Angel Investor", "Fund Manager / General Partner"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investor_type"] == ["Angel Investor"]


# --- Luma "primary investment or industry areas of focus" -> investment_industry ---
#
# investment_industry itself stays a genuinely open CRM field (no live
# options list, matching product decision -- see the empty-options tests
# above). Canonical-vs-arbitrary filtering for THIS question happens
# entirely in normalize_industry_focus_labels() (LumaAnswerNormalizer.
# INDUSTRY_FOCUS_LABEL): only exact INDUSTRY_OPTIONS members survive,
# unrecognized Luma strings are dropped before the value ever reaches the
# (now-unrestricted) CRM field. "Other" is itself a canonical
# INDUSTRY_OPTIONS member (added 2026-09-08), so it survives like any
# other recognized value.

LUMA_INDUSTRY_QUESTION_LABEL = "What are your primary investment or industry areas of focus?"


async def _seed_industry_mapping(mapping_store):
    return await _seed_mapping(
        mapping_store,
        question_label=LUMA_INDUSTRY_QUESTION_LABEL,
        question_type="multi-select",
        target_field_key="custom:investment_industry",
        normalizer="industry_focus_label",
    )


async def test_industry_focus_multi_select_populates_investment_industry_on_a_new_contact(luma_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Artificial Intelligence / Machine Learning"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Artificial Intelligence / Machine Learning"]


async def test_industry_focus_multiple_selections_are_all_preserved(luma_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Crypto / Web3", "Professional / Business Services", "Cybersecurity"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == [
        "Crypto / Web3",
        "Professional / Business Services",
        "Cybersecurity",
    ]


async def test_industry_focus_union_merges_with_existing_values_never_overwrites(luma_service, crm_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    existing = make_contact(email="alice@example.com", custom_fields={"investment_industry": ["Healthcare & HealthTech"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Crypto / Web3"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Healthcare & HealthTech", "Crypto / Web3"]


async def test_industry_focus_does_not_duplicate_an_already_present_value(luma_service, crm_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    existing = make_contact(email="alice@example.com", custom_fields={"investment_industry": ["Cybersecurity"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Cybersecurity"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Cybersecurity"]
    assert "custom:investment_industry" not in result.changed_field_keys  # no-op, value already present


async def test_industry_focus_other_is_now_kept_alongside_other_canonical_values(luma_service, mapping_store):
    """"Other" was added to INDUSTRY_OPTIONS 2026-09-08 -- it's a
    recognized canonical value now, not dropped."""
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Cybersecurity", "Other"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Cybersecurity", "Other"]


async def test_industry_focus_arbitrary_unrecognized_value_is_still_dropped(luma_service, mapping_store):
    """A genuinely uncontrolled string (never added to INDUSTRY_OPTIONS)
    is still dropped, since investment_industry's live field has no
    options list of its own to fall back on for this -- only "Other"
    itself changed status, not the general "unrecognized is dropped"
    rule."""
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Cybersecurity", "Underwater Basket Weaving"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Cybersecurity"]


async def test_industry_focus_only_other_selected_now_writes_other(luma_service, mapping_store):
    """"Other" alone now survives normalize_industry_focus_labels and gets
    written -- contrast test_industry_focus_only_unrecognized_writes_nothing
    below, where the "nothing valid survives -> write nothing" contract
    still holds for a genuinely unrecognized value."""
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {"label": LUMA_INDUSTRY_QUESTION_LABEL, "question_id": "q-1", "question_type": "multi-select", "value": ["Other"]}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investment_industry"] == ["Other"]


async def test_industry_focus_only_unrecognized_writes_nothing(luma_service, mapping_store):
    """Confirms normalize_industry_focus_labels's "nothing valid survives
    -> None -> write nothing" contract still holds for a value that was
    never added to INDUSTRY_OPTIONS."""
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Underwater Basket Weaving"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert "investment_industry" not in result.contact.custom_fields


async def test_industry_focus_other_is_still_preserved_in_the_raw_registration_answer(luma_service, mapping_store):
    """The raw Luma answer is never lost regardless of mapping outcome --
    see LumaRegistration.registration_answers's own docstring."""
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Cybersecurity", "Other"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.registration.registration_answers[0].value == ["Cybersecurity", "Other"]


async def test_industry_focus_unanswered_question_does_not_touch_the_field(luma_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(registration_answers=[])  # never answered this question

    result = await luma_service.process_guest_event(make_event(), guest)

    assert "investment_industry" not in result.contact.custom_fields


async def test_industry_focus_webhook_replay_is_idempotent(luma_service, mapping_store):
    await _seed_industry_mapping(mapping_store)
    guest = make_guest(
        registration_answers=[
            {
                "label": LUMA_INDUSTRY_QUESTION_LABEL,
                "question_id": "q-1",
                "question_type": "multi-select",
                "value": ["Crypto / Web3", "Cybersecurity"],
            }
        ]
    )
    await luma_service.process_guest_event(make_event(), guest)
    second_result = await luma_service.process_guest_event(make_event(), guest)

    assert second_result.contact.custom_fields["investment_industry"] == ["Crypto / Web3", "Cybersecurity"]
    assert "custom:investment_industry" not in second_result.changed_field_keys  # unchanged on rerun -- no-op


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


# --- Check Size translation, Investor Type translation, generic ------------
# --- scalar->multi-select wrapping, CRM option-allowlist validation --------


async def test_check_size_dropdown_answer_is_translated_into_crm_buckets(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "$25K–$100K"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]


async def test_check_size_translation_union_merges_with_an_existing_value(luma_service, crm_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    existing = make_contact(email="alice@example.com", custom_fields={"check_size_personal": ["$1M - $2M"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "Under $25K"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert set(result.contact.custom_fields["check_size_personal"]) == {"$1M - $2M", "$1k - $10k", "$10k - $25k"}


async def test_unrecognized_check_size_value_never_writes_the_field(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "Some old free-text answer"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert "check_size_personal" not in result.contact.custom_fields


async def test_syndicate_lead_investor_type_is_translated_to_its_crm_equivalent(luma_service, crm_service, mapping_store):
    field = await crm_service.custom_field_store.get_by_field_key("investor_type")
    await crm_service.custom_field_store.save(
        field.model_copy(update={"options": [*field.options, "I sponsor deals that I find"]})
    )
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select",
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Syndicate Lead"]}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investor_type"] == ["I sponsor deals that I find"]


async def test_investor_type_labels_with_no_crm_equivalent_are_dropped_not_written(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select",
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    guest = make_guest(
        registration_answers=[
            {
                "label": "Investor Type", "question_id": "q-1", "question_type": "multi-select",
                "value": ["Fund Manager / General Partner", "Corporate Venture"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert "investor_type" not in result.contact.custom_fields


async def test_investor_type_mixed_list_keeps_valid_entries_and_drops_the_rest(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select",
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    guest = make_guest(
        registration_answers=[
            {
                "label": "Investor Type", "question_id": "q-1", "question_type": "multi-select",
                "value": ["Angel Investor", "Corporate Venture"],
            }
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investor_type"] == ["Angel Investor"]


async def test_deploying_capital_scalar_dropdown_maps_to_a_single_select_field(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type="dropdown",
        target_field_key="custom:deploying_capital",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Deploying Capital", "question_id": "q-1", "question_type": "dropdown", "value": "Yes, actively"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["deploying_capital"] == "Yes, actively"


async def test_single_select_value_outside_the_crm_allowlist_is_never_written(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type="dropdown",
        target_field_key="custom:deploying_capital",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Deploying Capital", "question_id": "q-1", "question_type": "dropdown", "value": "Maybe later"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert "deploying_capital" not in result.contact.custom_fields


async def test_scalar_answer_is_wrapped_into_a_list_for_a_multi_select_target(luma_service, mapping_store):
    """No normalizer at all -- a bare scalar answer mapped straight onto a
    multi_select custom field must still land as a one-item list, never a
    raw scalar written into a list-typed field."""
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "$1M - $2M"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["check_size_personal"] == ["$1M - $2M"]


async def test_raw_registration_answers_are_never_rewritten_by_translation(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "$25K–$100K"}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.registration.registration_answers[0].value == "$25K–$100K"


# --- automatic Role=Investor tagging from the investor questionnaire -------


async def _seed_investor_mappings(mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type="multi-select",
        target_field_key="custom:investor_type",
    )
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type="dropdown",
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type="dropdown",
        target_field_key="custom:deploying_capital",
    )


async def test_investor_questionnaire_answer_tags_role_investor(luma_service, mapping_store):
    await _seed_investor_mappings(mapping_store)
    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["role"] == ["Investor"]


async def test_existing_role_values_are_preserved_and_union_merged(luma_service, crm_service, mapping_store):
    await _seed_investor_mappings(mapping_store)
    existing = make_contact(email="alice@example.com", custom_fields={"role": ["Founder"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert set(result.contact.custom_fields["role"]) == {"Founder", "Investor"}


async def test_rerunning_the_same_registration_does_not_duplicate_investor_role(luma_service, mapping_store):
    await _seed_investor_mappings(mapping_store)
    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]},
        ]
    )
    await luma_service.process_guest_event(make_event(), guest)
    second_result = await luma_service.process_guest_event(make_event(), guest)

    assert second_result.contact.custom_fields["role"] == ["Investor"]  # not ["Investor", "Investor"]
    assert "custom:role" not in second_result.changed_field_keys  # unchanged on rerun -- no-op


@pytest.mark.parametrize("deploying_capital_value", ["Not at the moment", "Selectively", "Yes, actively"])
async def test_any_deploying_capital_answer_classifies_as_investor(luma_service, mapping_store, deploying_capital_value):
    await _seed_investor_mappings(mapping_store)
    guest = make_guest(
        registration_answers=[
            {"label": "Deploying Capital", "question_id": "q-1", "question_type": "dropdown", "value": deploying_capital_value},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["role"] == ["Investor"]


async def test_invited_never_registered_guests_are_never_classified():
    """No registration_answers at all (never actually registered) -- no
    investor signal present, so no Role tagging. (Luma never sends a
    guest.registered/guest.updated webhook, and event-scoped backfill's
    approval_status filter never calls process_guest_event, for a guest who
    only exists as an "invited" record -- this test proves the underlying
    _build_mapped_fields logic itself also never invents a signal from
    nothing, as a second, independent guarantee.)"""
    from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
    from app.repositories.luma_event_store import MemoryLumaEventStore
    from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
    from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
    from app.services.crm_service import CrmService

    crm = CrmService(custom_field_store=MemoryCrmCustomFieldStore())
    service = LumaSyncService(
        crm_service=crm, event_store=MemoryLumaEventStore(), registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(), activity_log=crm.activity_log,
    )
    guest = make_guest(registration_answers=[])

    result = await service.process_guest_event(make_event(), guest)

    assert "role" not in result.contact.custom_fields


async def test_check_size_investor_type_deploying_capital_mappings_still_apply_unchanged(luma_service, mapping_store):
    """Role tagging is additive -- the existing translated values for the
    three investor-signal fields themselves must be completely unaffected."""
    await _seed_investor_mappings(mapping_store)
    guest = make_guest(
        registration_answers=[
            {"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]},
            {"label": "Check Size", "question_id": "q-2", "question_type": "dropdown", "value": "$25K–$100K"},
            {"label": "Deploying Capital", "question_id": "q-3", "question_type": "dropdown", "value": "Selectively"},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["investor_type"] == ["Angel Investor"]
    assert result.contact.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]
    assert result.contact.custom_fields["deploying_capital"] == "Selectively"
    assert result.contact.custom_fields["role"] == ["Investor"]


async def test_a_guest_with_no_investor_signal_gets_no_role_tag(luma_service, mapping_store):
    await _seed_investor_mappings(mapping_store)
    await _seed_mapping(mapping_store, question_label="Company", question_type="company", target_field_key="company", extract_key="company")
    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q-1", "question_type": "company", "value": {"company": "Acme"}},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert "role" not in result.contact.custom_fields


# =====================================================================
# Investor-question mappings: question_type=None resilience (the
# "Deploying Capital" bug fix). Luma began sending question_type="select"
# for these four questions instead of their historical "dropdown"/
# "multi-select" -- the mapping's own question_type guard, when set to a
# specific string, silently stopped matching. The fix is a MAPPING
# CONFIG change (question_type -> None), not a code change: the
# `if mapping.question_type and mapping.question_type != question_type`
# guard in _build_mapped_fields already treats None as "matches any
# type" (see LinkedIn's own mapping, which already relies on this). These
# tests seed each of the four real mappings with question_type=None and
# prove BOTH the historical type and the new "select" type resolve to
# the exact same destination field with the exact same value semantics.
# =====================================================================


async def test_deploying_capital_matches_both_dropdown_and_select_with_type_none(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type=None, target_field_key="custom:deploying_capital"
    )
    guest_dropdown = make_guest(
        email="dropdown@example.com",
        registration_answers=[{"label": "Deploying Capital", "question_id": "q-1", "question_type": "dropdown", "value": "Not at the moment"}],
    )
    guest_select = make_guest(
        email="select@example.com",
        registration_answers=[{"label": "Deploying Capital", "question_id": "q-1", "question_type": "select", "value": "Yes, actively"}],
    )

    result_dropdown = await luma_service.process_guest_event(make_event(), guest_dropdown)
    result_select = await luma_service.process_guest_event(make_event(), guest_select)

    # Exact strings preserved -- never reduced to a boolean.
    assert result_dropdown.contact.custom_fields["deploying_capital"] == "Not at the moment"
    assert result_select.contact.custom_fields["deploying_capital"] == "Yes, actively"
    assert result_select.contact.custom_fields["deploying_capital"] is not True
    assert result_select.contact.custom_fields["deploying_capital"] is not False


async def test_deploying_capital_selectively_value_preserved_exactly(luma_service, mapping_store):
    """The exact regression case this whole investigation started from."""
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type=None, target_field_key="custom:deploying_capital"
    )
    guest = make_guest(
        registration_answers=[{"label": "Deploying Capital", "question_id": "q-1", "question_type": "select", "value": "Selectively"}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)
    assert result.contact.custom_fields["deploying_capital"] == "Selectively"


async def test_investor_type_matches_both_multi_select_and_select_with_type_none(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type=None,
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    guest_multi = make_guest(
        email="multi@example.com",
        registration_answers=[{"label": "Investor Type", "question_id": "q-1", "question_type": "multi-select", "value": ["Angel Investor"]}],
    )
    guest_select = make_guest(
        email="select@example.com",
        registration_answers=[{"label": "Investor Type", "question_id": "q-1", "question_type": "select", "value": ["Family Office"]}],
    )

    result_multi = await luma_service.process_guest_event(make_event(), guest_multi)
    result_select = await luma_service.process_guest_event(make_event(), guest_select)

    assert result_multi.contact.custom_fields["investor_type"] == ["Angel Investor"]
    assert result_select.contact.custom_fields["investor_type"] == ["Family Office"]


async def test_check_size_matches_both_dropdown_and_select_with_type_none(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Check Size", question_type=None,
        target_field_key="custom:check_size_personal", normalizer="check_size_personal_bucket",
    )
    guest_dropdown = make_guest(
        email="dropdown@example.com",
        registration_answers=[{"label": "Check Size", "question_id": "q-1", "question_type": "dropdown", "value": "$25K–$100K"}],
    )
    guest_select = make_guest(
        email="select@example.com",
        registration_answers=[{"label": "Check Size", "question_id": "q-1", "question_type": "select", "value": "$100K–$250K"}],
    )

    result_dropdown = await luma_service.process_guest_event(make_event(), guest_dropdown)
    result_select = await luma_service.process_guest_event(make_event(), guest_select)

    assert result_dropdown.contact.custom_fields["check_size_personal"] == ["$25k - $50k", "$50k - $100k"]
    assert result_select.contact.custom_fields["check_size_personal"] == ["$100k - $250k"]


async def test_investment_industry_matches_both_multi_select_and_select_with_type_none(luma_service, mapping_store):
    await _seed_mapping(
        mapping_store, question_label="Investment Industry", question_type=None,
        target_field_key="custom:investment_industry", normalizer="industry_focus_label",
    )
    guest_multi = make_guest(
        email="multi@example.com",
        registration_answers=[{"label": "Investment Industry", "question_id": "q-1", "question_type": "multi-select", "value": ["Artificial Intelligence / Machine Learning"]}],
    )
    guest_select = make_guest(
        email="select@example.com",
        registration_answers=[{"label": "Investment Industry", "question_id": "q-1", "question_type": "select", "value": ["Real Estate & PropTech", "Cybersecurity"]}],
    )

    result_multi = await luma_service.process_guest_event(make_event(), guest_multi)
    result_select = await luma_service.process_guest_event(make_event(), guest_select)

    # Exact-match filter against INDUSTRY_OPTIONS -- these two values pass
    # through verbatim (both are real canonical members), never translated.
    assert result_multi.contact.custom_fields["investment_industry"] == ["Artificial Intelligence / Machine Learning"]
    assert set(result_select.contact.custom_fields["investment_industry"]) == {"Real Estate & PropTech", "Cybersecurity"}


# --- multi-select destination field determines list-vs-scalar, not Luma's question_type --


async def test_a_select_typed_scalar_answer_is_still_wrapped_into_a_list_for_a_multi_select_field(luma_service, mapping_store):
    """Defensive: even if Luma ever sends a BARE SCALAR (not a list) for
    a question whose CRM destination is multi_select, the destination
    field's own definition -- not Luma's question_type string -- decides
    the shape. _wrap_scalar_for_multi_select already guarantees this;
    this test proves it holds for the "select" type specifically."""
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type=None,
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    guest = make_guest(
        registration_answers=[{"label": "Investor Type", "question_id": "q-1", "question_type": "select", "value": "Angel Investor"}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)
    assert result.contact.custom_fields["investor_type"] == ["Angel Investor"]  # wrapped into a list, not a bare string


async def test_a_new_select_typed_registration_unions_with_existing_multi_select_selections(luma_service, crm_service, mapping_store):
    """Must not discard existing selections merely because a LATER
    registration reports the question as "select" instead of the
    historical "multi-select" -- union-merge, not replace."""
    await _seed_mapping(
        mapping_store, question_label="Investor Type", question_type=None,
        target_field_key="custom:investor_type", normalizer="investor_type_label",
    )
    existing = make_contact(email="alice@example.com", custom_fields={"investor_type": ["Venture Capital"]})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Investor Type", "question_id": "q-1", "question_type": "select", "value": ["Family Office"]}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert set(result.contact.custom_fields["investor_type"]) == {"Venture Capital", "Family Office"}


async def test_deploying_capital_fill_only_is_unaffected_by_type_none(luma_service, crm_service, mapping_store):
    """deploying_capital is single_select (fill-only, not union-merge) --
    an ALREADY-SET value must not be overwritten by a later "select"-typed
    registration, exactly like every other fill-only custom field."""
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type=None, target_field_key="custom:deploying_capital"
    )
    existing = make_contact(email="alice@example.com", custom_fields={"deploying_capital": "Not at the moment"})
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Deploying Capital", "question_id": "q-1", "question_type": "select", "value": "Yes, actively"}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["deploying_capital"] == "Not at the moment"  # unchanged -- fill-only


# =====================================================================
# Luma self-report Company/Job Title enrichment -- live webhook path
# (app/services/luma_contact_enrichment.py). Deliberately NO
# LumaQuestionMapping seeded in most of these -- the whole point is that
# this path works independent of that configurable system.
# =====================================================================


async def test_a_new_registration_with_a_company_answer_enriches_the_contact_with_no_mapping_configured(luma_service, luma_contact_enrichment_enabled):
    guest = make_guest(
        registration_answers=[
            {"label": "Where do you work?", "question_id": "q1", "question_type": "company", "value": {"company": "Acme", "job_title": "CEO"}}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Acme"
    assert result.contact.title == "CEO"
    assert "company" in result.changed_field_keys
    assert "title" in result.changed_field_keys
    assert result.contact.custom_fields["field_provenance"]["company"]["source"] == "luma_self_report"


async def test_self_report_replaces_an_existing_different_value_unlike_generic_mapping(luma_service, crm_service, luma_contact_enrichment_enabled):
    existing = make_contact(email="alice@example.com", company="Sequoia Capital", title="Partner")
    await crm_service.contact_store.create(existing)
    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Sequoia", "job_title": "GP"}}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Sequoia"  # replaced, not fill-only
    assert result.contact.title == "GP"


async def test_an_edited_registration_reapplies_the_self_report(luma_service, luma_contact_enrichment_enabled):
    guest_v1 = make_guest(
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme"}}]
    )
    await luma_service.process_guest_event(make_event(), guest_v1, webhook_delivery_id="d1")

    guest_v2 = make_guest(
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme Ventures"}}]
    )
    result = await luma_service.process_guest_event(make_event(), guest_v2, webhook_delivery_id="d2")

    assert result.contact.company == "Acme Ventures"


async def test_a_late_arriving_older_registration_cannot_regress_a_newer_self_report(luma_service, luma_contact_enrichment_enabled):
    """Two DIFFERENT registrations (different events) for the SAME person
    -- the newer one is processed FIRST, the older one arrives LATE and is
    processed SECOND. Recomputing from the complete stored set each time
    means the older one can never win."""
    newer_event = make_event(event_id="evt-newer")
    newer_guest = make_guest(
        guest_id="gst-newer",
        registered_at="2026-09-01T10:00:00Z",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "NewCo"}}],
    )
    await luma_service.process_guest_event(newer_event, newer_guest)

    older_event = make_event(event_id="evt-older")
    older_guest = make_guest(
        guest_id="gst-older",
        registered_at="2026-01-01T10:00:00Z",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "VeryOldCo"}}],
    )
    result = await luma_service.process_guest_event(older_event, older_guest)

    assert result.contact.company == "NewCo"  # the late-arriving older registration did not regress it


async def test_blank_luma_company_never_erases_an_existing_value(luma_service, crm_service, luma_contact_enrichment_enabled):
    existing = make_contact(email="alice@example.com", company="Existing Co")
    await crm_service.contact_store.create(existing)
    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": None, "job_title": None}}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Existing Co"


async def test_ambiguous_unknown_recency_fires_the_enrichment_ambiguous_activity_event(luma_service, crm_service, luma_contact_enrichment_enabled):
    guest1 = make_guest(
        guest_id="gst-1",
        registered_at=None,
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Co1"}}],
    )
    await luma_service.process_guest_event(make_event(event_id="evt-1", start_at=None), guest1)

    guest2 = make_guest(
        guest_id="gst-2",
        registered_at=None,
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Co2"}}],
    )
    result = await luma_service.process_guest_event(make_event(event_id="evt-2", start_at=None), guest2)

    # The FIRST registration's single, uncontested unknown-recency answer
    # was legitimately adopted at the time it was the only one available --
    # that adoption is never retroactively undone. "Ambiguous" means "no
    # FURTHER guess was made once a second, conflicting unknown-recency
    # answer showed up" -- not "erase what was already legitimately there".
    assert result.contact.company == "Co1"
    assert "company" in result.luma_self_report_ambiguous_fields

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    ambiguous_events = [e for e in page.items if e.event_type == "luma.contact.enrichment_ambiguous"]
    assert len(ambiguous_events) == 1
    assert ambiguous_events[0].metadata == {"fields": ["company"]}


async def test_material_company_change_preserves_stale_website_end_to_end(luma_service, crm_service, luma_contact_enrichment_enabled):
    """V1 corrected behavior: an existing website is never cleared/
    overwritten, even on a material Company change -- it's preserved and
    the outcome is flagged for review instead."""
    existing = make_contact(email="alice@example.com", company="OldCo", company_website="oldco.com")
    await crm_service.contact_store.create(existing)
    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "NewCo"}}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "NewCo"
    assert result.contact.company_website == "oldco.com"  # preserved, not cleared
    assert "company_website" not in result.changed_field_keys
    assert result.luma_self_report_website_review_needed is True

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    review_events = [e for e in page.items if e.event_type == "luma.contact.website_review_needed"]
    assert len(review_events) == 1


async def test_tier1_populates_a_blank_website_end_to_end(luma_service, crm_service, luma_contact_enrichment_enabled):
    reference = make_contact(email="reference@example.com", company="NewCo", company_website="newco.com")
    await crm_service.contact_store.create(reference)
    existing = make_contact(email="alice@example.com", company="OldCo", company_website=None)
    await crm_service.contact_store.create(existing)

    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "NewCo"}}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company_website == "https://newco.com"
    assert result.luma_self_report_website_review_needed is False


async def test_enriched_activity_event_includes_self_report_field_keys(luma_service, crm_service, luma_contact_enrichment_enabled):
    """A brand-new contact's own contact_outcome stays "created" (more
    informative than downgrading it) -- but the self-report's OWN
    additional change still gets its own luma.contact.enriched event,
    fired independently alongside luma.contact.created."""
    guest = make_guest(
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme", "job_title": "CEO"}}]
    )
    result = await luma_service.process_guest_event(make_event(), guest)
    assert result.contact_outcome == "created"

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    enriched_events = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert len(enriched_events) == 1
    fields_updated = enriched_events[0].metadata["fields_updated"]
    assert "company" in fields_updated
    assert "title" in fields_updated
    assert "custom:field_provenance" in fields_updated
    # Structural only -- never the self-reported values themselves.
    assert "Acme" not in str(enriched_events[0].metadata)
    assert "CEO" not in str(enriched_events[0].metadata)


async def test_a_registration_with_no_company_question_answer_does_not_touch_company_or_title(luma_service, crm_service, luma_contact_enrichment_enabled):
    existing = make_contact(email="alice@example.com", company="Existing Co", title="Existing Title")
    await crm_service.contact_store.create(existing)
    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "LinkedIn Profile", "question_id": "q1", "question_type": "linkedin", "value": "https://linkedin.com/in/alice"}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Existing Co"
    assert result.contact.title == "Existing Title"


async def test_luma_contact_enrichment_disabled_by_default_leaves_the_feature_completely_inert(luma_service, crm_service):
    """No `luma_contact_enrichment_enabled` fixture requested here --
    settings.luma_contact_enrichment_enabled is False (its real production
    default). A structurally perfect question_type=="company" answer must
    produce ZERO effect: no company/title write, no field_provenance
    custom field, no new Activity Log event beyond the ordinary
    created/enriched ones from any OTHER (unrelated) mapped fields."""
    assert luma_sync_service_module.settings.luma_contact_enrichment_enabled is False

    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme", "job_title": "CEO"}}
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company is None
    assert result.contact.title is None
    assert "field_provenance" not in result.contact.custom_fields
    assert result.luma_self_report_ambiguous_fields == []

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    assert not any(e.event_type == "luma.contact.enrichment_ambiguous" for e in page.items)
    enriched_events = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert enriched_events == []  # nothing else changed either -- purely a "created" outcome


async def test_settings_luma_contact_enrichment_enabled_defaults_false():
    from app.config import Settings

    assert Settings.model_fields["luma_contact_enrichment_enabled"].default is False


async def test_a_registration_whose_answer_already_matches_causes_zero_save_and_zero_activity_event(
    luma_service, crm_service, luma_contact_enrichment_enabled
):
    """The V1 provenance correction, exercised end-to-end: Luma reconfirming
    an ALREADY-identical Company/Title must produce no Contact save, no
    field_provenance write, and no luma.contact.enriched event -- not even
    a "provenance-only" one."""
    # first/last name pre-set to match the guest's own defaults (Alice
    # Angel) so the generic mapping's own fill-only enrichment has nothing
    # blank left to fill -- isolates this test to the self-report path.
    existing = make_contact(email="alice@example.com", first_name="Alice", last_name="Angel", company="SameCo", title="SameTitle")
    await crm_service.contact_store.create(existing)
    guest = make_guest(
        email="alice@example.com",
        registration_answers=[{"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "SameCo", "job_title": "SameTitle"}}],
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact_outcome == "unchanged"
    assert result.changed_field_keys == []
    assert "field_provenance" not in result.contact.custom_fields

    page = await crm_service.activity_log.list_events(category=ActivityCategory.LUMA)
    enriched_events = [e for e in page.items if e.event_type == "luma.contact.enriched"]
    assert enriched_events == []


# --- production-readiness: independence from investor-question mapping, ----
# and no automatic historical replay from merely flipping the flag --------
# (verified 2026-09-08 before enabling LUMA_CONTACT_ENRICHMENT_ENABLED live)


async def test_investor_mapping_and_company_title_enrichment_both_apply_from_one_event_when_flag_is_on(
    luma_service, mapping_store, luma_contact_enrichment_enabled
):
    """The generic LumaQuestionMapping path (investor_type/deploying_capital/
    etc.) and the self-report Company/Title path are two independent code
    paths in _process_guest_event_locked -- mapped_fields is always built
    and applied FIRST, unconditionally; the self-report block runs after,
    gated only by the flag. One registration carrying both kinds of
    answers must produce both kinds of results from the same webhook call."""
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type=None, target_field_key="custom:deploying_capital",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme", "job_title": "CEO"}},
            {"label": "Deploying Capital", "question_id": "q2", "question_type": "select", "value": "Yes, actively"},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.company == "Acme"
    assert result.contact.title == "CEO"
    assert result.contact.custom_fields["deploying_capital"] == "Yes, actively"


async def test_investor_mapping_still_applies_when_enrichment_flag_is_off(luma_service, mapping_store):
    """The reverse direction: with the flag OFF (this file's real
    production default -- no luma_contact_enrichment_enabled fixture
    requested), the investor-question mapping must be completely
    unaffected -- it was already live and independent of this flag before
    Company/Title enrichment existed, and must remain so after enabling it
    elsewhere."""
    assert luma_sync_service_module.settings.luma_contact_enrichment_enabled is False
    await _seed_mapping(
        mapping_store, question_label="Deploying Capital", question_type=None, target_field_key="custom:deploying_capital",
    )
    guest = make_guest(
        registration_answers=[
            {"label": "Company", "question_id": "q1", "question_type": "company", "value": {"company": "Acme", "job_title": "CEO"}},
            {"label": "Deploying Capital", "question_id": "q2", "question_type": "select", "value": "Yes, actively"},
        ]
    )
    result = await luma_service.process_guest_event(make_event(), guest)

    assert result.contact.custom_fields["deploying_capital"] == "Yes, actively"
    assert result.contact.company is None  # self-report enrichment stays completely inert
    assert result.contact.title is None


def test_only_the_live_webhook_path_and_the_explicit_backfill_script_ever_call_the_self_report_merge_functions():
    """Structural guard: proves enabling LUMA_CONTACT_ENRICHMENT_ENABLED
    cannot, by itself, trigger any kind of historical replay. The self-
    report merge functions (apply_luma_self_report / resolve_contact_luma_
    fields) run ONLY in response to an explicit call -- either a real live
    webhook event reaching LumaSyncService._apply_luma_contact_self_report,
    or an explicit, human-invoked run of the historical backfill script.
    There is no scheduled job, startup hook, or config-change listener
    anywhere in the app that calls either function -- this test scans
    every .py file under app/ and fails the moment a THIRD call site
    (outside the enrichment module's own definition, luma_sync_service.py,
    and luma_contact_enrichment_backfill.py) ever appears."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    allowed_files = {
        repo_root / "app" / "services" / "luma_contact_enrichment.py",  # the functions' own definitions
        repo_root / "app" / "services" / "luma_sync_service.py",  # the live webhook path
        repo_root / "app" / "services" / "luma_contact_enrichment_backfill.py",  # the explicit backfill driver
    }
    offending_files = []
    for path in (repo_root / "app").rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text()
        if "apply_luma_self_report(" in text or "resolve_contact_luma_fields(" in text:
            offending_files.append(str(path))
    assert offending_files == []


# =====================================================================
# Client CRM Stage 1H-B (2026-09-10) -- participant sync wiring
# =====================================================================


@pytest_asyncio.fixture
async def participant_sync_service():
    from app.repositories.crm_contact_store import MemoryCrmContactStore
    from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
    from app.repositories.engagement_store import MemoryEngagementStore
    from app.services.luma_engagement_participant_sync_service import LumaEngagementParticipantSyncService

    return LumaEngagementParticipantSyncService, MemoryEngagementStore(), MemoryEngagementParticipantStore(), MemoryCrmContactStore()


@pytest_asyncio.fixture
async def luma_service_with_participant_sync(crm_service, event_store, registration_store, mapping_store, participant_sync_service):
    """Same as `luma_service`, but with Stage 1H-B's participant sync
    actually wired in -- exposes the Engagement/EngagementParticipant/
    CrmContact stores directly so a test can link an Engagement first."""
    sync_cls, engagement_store, engagement_participant_store, crm_contact_store = participant_sync_service
    sync_service = sync_cls(
        engagement_store=engagement_store,
        engagement_participant_store=engagement_participant_store,
        crm_contact_store=crm_service.contact_store,  # SAME contact store the webhook path itself writes to
        activity_log=crm_service.activity_log,
    )
    service = LumaSyncService(
        crm_service=crm_service,
        event_store=event_store,
        registration_store=registration_store,
        mapping_store=mapping_store,
        activity_log=crm_service.activity_log,
        participant_sync_service=sync_service,
    )
    return service, engagement_store, engagement_participant_store


async def test_default_construction_leaves_participant_sync_unwired(luma_service):
    """Backward-compatibility proof: every existing caller/test that builds
    LumaSyncService without participant_sync_service (the `luma_service`
    fixture, unchanged by this stage) gets exactly None -- the new final
    step in _process_guest_event_locked is skipped entirely, byte-identical
    to before Stage 1H-B existed."""
    assert luma_service.participant_sync_service is None


async def test_wired_participant_sync_creates_a_participant_for_a_linked_event(luma_service_with_participant_sync):
    from datetime import datetime, timezone

    from app.models.client_crm import Engagement, EngagementStatus, EngagementType

    service, engagement_store, engagement_participant_store = luma_service_with_participant_sync
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    await engagement_store.create(
        Engagement(
            engagement_id="e1", client_id="c1", title="SF Investor Dinner", engagement_type=EngagementType.DINNER,
            luma_event_id="evt-1", status=EngagementStatus.CONFIRMED, created_at=now, updated_at=now,
        )
    )

    result = await service.process_guest_event(make_event(event_id="evt-1"), make_guest())

    participants = await engagement_participant_store.list_for_engagement("e1")
    assert len(participants) == 1
    assert participants[0].crm_contact_id == result.contact.crm_contact_id


async def test_no_engagement_linked_to_this_event_still_processes_registration_normally(luma_service_with_participant_sync):
    service, _engagement_store, engagement_participant_store = luma_service_with_participant_sync
    result = await service.process_guest_event(make_event(event_id="evt-unlinked"), make_guest())
    assert result.registration.luma_event_id == "evt-unlinked"
    assert result.contact is not None
    assert await engagement_participant_store.list_for_engagement("e1") == []


async def test_participant_sync_exception_never_breaks_luma_registration_or_contact_processing(luma_service, monkeypatch):
    """Forces the participant-sync step to raise, and confirms
    process_guest_event() still returns its normal, fully-successful
    result (registration saved, contact created) -- the exact fail-open
    contract Stage 1H-B requires."""

    class _ExplodingSyncService:
        async def sync_luma_registration_to_engagement_participant(self, registration):
            raise RuntimeError("simulated participant sync failure")

    luma_service.participant_sync_service = _ExplodingSyncService()

    result = await luma_service.process_guest_event(make_event(), make_guest())

    assert result.contact is not None
    assert result.registration.luma_guest_id == "gst-1"
    assert result.registration_is_new is True


async def test_no_participant_sync_exception_ever_escapes_process_guest_event(luma_service):
    """Same forced failure as above, but the assertion is specifically
    that NO exception propagates out of process_guest_event() at all --
    this is what guarantees the webhook route's own HTTP 200 response is
    never affected (see app/api/luma.py's luma_webhook route, which only
    checks for a raised LumaSyncError, never anything from participant
    sync)."""

    class _ExplodingSyncService:
        async def sync_luma_registration_to_engagement_participant(self, registration):
            raise RuntimeError("simulated participant sync failure")

    luma_service.participant_sync_service = _ExplodingSyncService()

    try:
        await luma_service.process_guest_event(make_event(), make_guest())
    except Exception as e:  # noqa: BLE001 -- the test itself asserts none should ever reach here
        pytest.fail(f"process_guest_event() must never raise from a participant-sync failure, but raised: {e!r}")


def test_no_automatic_call_site_syncs_a_registration_to_an_engagement_participant():
    """Structural guard, mirroring test_only_the_live_webhook_path_and_
    the_explicit_backfill_script_ever_call_the_self_report_merge_functions
    above: sync_luma_registration_to_engagement_participant() is called
    from exactly ONE place in the entire app -- the live guest-processing
    path in luma_sync_service.py (itself shared by the webhook AND the
    operator-triggered backfill, by this app's own existing, unmodified
    design) -- never from app startup (main.py), never from Stage 1H-A's
    own Engagement-linking code (client_crm_service.py), never from a
    scheduled job."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    allowed_files = {
        repo_root / "app" / "services" / "luma_engagement_participant_sync_service.py",  # the function's own definition
        repo_root / "app" / "services" / "luma_sync_service.py",  # the one caller
    }
    offending_files = []
    for path in (repo_root / "app").rglob("*.py"):
        if path in allowed_files:
            continue
        text = path.read_text()
        if "sync_luma_registration_to_engagement_participant(" in text:
            offending_files.append(str(path))
    assert offending_files == []


def test_app_startup_never_calls_run_backfill_or_process_guest_event_automatically():
    """main.py's lifespan wiring must only ever CONSTRUCT LumaSyncService/
    LumaEngagementParticipantSyncService -- it must never itself call
    run_backfill(), run_event_backfill(), process_guest_event(), or
    sync_luma_registration_to_engagement_participant() at startup. Those
    are reached only via an explicit HTTP request (the webhook, or an
    operator hitting POST /sync/luma-backfill)."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    main_source = (repo_root / "app" / "main.py").read_text()
    for forbidden in ("run_backfill(", "run_event_backfill(", "process_guest_event(", "sync_luma_registration_to_engagement_participant("):
        assert forbidden not in main_source, f"main.py must never call {forbidden} at startup"


def test_engagement_linking_code_has_no_awareness_of_luma_registrations_or_participant_sync():
    """Stage 1H-A's own Engagement<->Luma-event linking code
    (client_crm_service.py) must remain completely unaware of
    LumaRegistration/participant sync -- proving that linking an
    Engagement (or merely validating/looking up a luma_event_id) can never,
    by itself, trigger any historical participant creation."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = (repo_root / "app" / "services" / "client_crm_service.py").read_text()
    for forbidden in ("LumaRegistration", "sync_luma_registration_to_engagement_participant", "LumaEngagementParticipantSyncService"):
        assert forbidden not in source
