from datetime import datetime, timezone

import pytest

from app.models.client_crm import (
    ClientStatus,
    DinnerType,
    EngagementContractStatus,
    EngagementPaymentStatus,
    EngagementStatus,
    EngagementType,
)
from app.models.crm import CrmContact
from app.repositories.activity_event_store import MemoryActivityEventStore
from app.repositories.client_contact_store import MemoryClientContactStore
from app.repositories.client_store import MemoryClientStore
from app.repositories.client_touchpoint_store import MemoryClientTouchpointStore
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.engagement_closeout_store import MemoryEngagementCloseoutStore
from app.repositories.engagement_participant_store import MemoryEngagementParticipantStore
from app.repositories.engagement_store import MemoryEngagementStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.services.activity_log_service import ActivityLogService
from app.services.client_crm_service import ClientCrmService
from app.services.contact_engagement_signal_service import ContactEngagementSignalService
from app.services.crm_service import CrmService
from app.services.sale_onboarding_service import (
    SaleOnboardingConflict,
    SaleOnboardingPayload,
    SaleOnboardingService,
    normalize_service_sold,
    parse_event_date,
    split_full_name,
)

pytestmark = pytest.mark.asyncio


def _now():
    return datetime.now(timezone.utc)


async def _services():
    client_store = MemoryClientStore()
    client_contact_store = MemoryClientContactStore()
    crm_contact_store = MemoryCrmContactStore()
    engagement_store = MemoryEngagementStore()
    engagement_closeout_store = MemoryEngagementCloseoutStore()
    engagement_participant_store = MemoryEngagementParticipantStore()
    luma_event_store = MemoryLumaEventStore()
    client_touchpoint_store = MemoryClientTouchpointStore()
    activity_store = MemoryActivityEventStore()
    activity_log = ActivityLogService(store=activity_store)
    signal_service = ContactEngagementSignalService(crm_contact_store=crm_contact_store, activity_log=activity_log)
    client_crm_service = ClientCrmService(
        client_store=client_store,
        activity_log=activity_log,
        client_contact_store=client_contact_store,
        crm_contact_store=crm_contact_store,
        engagement_store=engagement_store,
        engagement_closeout_store=engagement_closeout_store,
        engagement_participant_store=engagement_participant_store,
        luma_event_store=luma_event_store,
        client_touchpoint_store=client_touchpoint_store,
        contact_engagement_signal_service=signal_service,
    )
    crm_service = CrmService(contact_store=crm_contact_store, activity_log=activity_log)
    return SaleOnboardingService(client_crm_service=client_crm_service, crm_service=crm_service), client_crm_service


def _payload(**overrides) -> SaleOnboardingPayload:
    defaults = dict(
        sale_id="sale-001",
        docusign_envelope_id="env-001",
        mercury_invoice_id="merc-001",
        qb_invoice_id="qb-inv-001",
        qb_customer_id="qb-cust-001",
        qb_amount=5000.0,
        client_company="Hive ASMBLD",
        primary_contact="Jane Doe",
        contact_email="jane@hiveasmbld.com",
        signer_name="John Roe",
        signer_email="john@hiveasmbld.com",
        service_sold="Investor Dinner",
        city="Austin",
        event_date="10/15/2026",
        internal_owner="Chris",
        referral_source="Warm intro",
        special_terms="Net 15",
    )
    defaults.update(overrides)
    return SaleOnboardingPayload(**defaults)


# --- normalize_service_sold ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_type",
    [
        ("Investor Dinner", DinnerType.INVESTOR_DINNER),
        ("investor dinner", DinnerType.INVESTOR_DINNER),
        ("  Investor   Dinner  ", DinnerType.INVESTOR_DINNER),
        ("Fireside Dinner", DinnerType.FIRESIDE_DINNER),
        ("BizDev Dinner", DinnerType.BIZDEV_DINNER),
        ("Donor Dinner", DinnerType.DONOR_DINNER),
        ("Custom Dinner", DinnerType.CUSTOM_DINNER),
    ],
)
def test_normalize_service_sold_matches_known_labels_conservatively(raw, expected_type):
    engagement_type, dinner_type, raw_out = normalize_service_sold(raw)
    assert engagement_type == EngagementType.DINNER
    assert dinner_type == expected_type
    assert raw_out is None  # never populated on a clean match


@pytest.mark.parametrize("raw", ["Consulting Retainer", "Speaking Engagement", "Something New", "", None])
def test_normalize_service_sold_never_guesses_an_unknown_value(raw):
    engagement_type, dinner_type, raw_out = normalize_service_sold(raw)
    assert engagement_type == EngagementType.OTHER
    assert dinner_type is None
    assert raw_out == raw  # preserved verbatim, never dropped


def test_normalize_service_sold_does_not_map_unknown_to_custom_dinner():
    """The one thing explicitly NOT allowed: falling back to CUSTOM_DINNER
    for an unrecognized value just because it's a dinner-shaped string."""
    engagement_type, dinner_type, raw_out = normalize_service_sold("Some Other Dinner")
    assert engagement_type == EngagementType.OTHER
    assert dinner_type is None
    assert raw_out == "Some Other Dinner"


# --- parse_event_date -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10/15/2026", "2026-10-15"),
        ("2026-10-15", "2026-10-15"),
        ("October 15, 2026", "2026-10-15"),
        ("Oct 15 2026", "2026-10-15"),
    ],
)
def test_parse_event_date_accepts_known_formats(raw, expected):
    parsed, raw_out = parse_event_date(raw)
    assert str(parsed) == expected
    assert raw_out is None


@pytest.mark.parametrize("raw", ["mid-October", "TBD", "next month", "sometime in Q4", "", None, "   "])
def test_parse_event_date_fails_safely_on_anything_else(raw):
    parsed, raw_out = parse_event_date(raw)
    assert parsed is None
    assert raw_out == raw  # preserved verbatim, never guessed


# --- split_full_name -------------------------------------------------------


def test_split_full_name_splits_on_first_whitespace_only():
    assert split_full_name("Jane Doe") == ("Jane", "Doe")
    assert split_full_name("Jane Van Der Berg") == ("Jane", "Van Der Berg")
    assert split_full_name("Cher") == ("Cher", None)
    assert split_full_name("") == (None, None)
    assert split_full_name(None) == (None, None)


# --- Client matching --------------------------------------------------------


async def test_new_client_is_created_active():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload())
    client = await client_crm_service.get_client(result.client_id)
    assert client.name == "Hive ASMBLD"
    assert client.status == ClientStatus.ACTIVE
    assert result.created_new_client is True


async def test_repeat_client_reuses_the_existing_client_and_creates_a_new_engagement():
    service, client_crm_service = await _services()
    first = await service.onboard(_payload(sale_id="sale-001", docusign_envelope_id="env-001"))
    second = await service.onboard(
        _payload(sale_id="sale-002", docusign_envelope_id="env-002", client_company="Hive ASMBLD")
    )

    assert first.client_id == second.client_id
    assert first.engagement_id != second.engagement_id
    assert second.created_new_client is False

    all_clients = await client_crm_service.client_store.list()
    hive_clients = [c for c in all_clients if c.name == "Hive ASMBLD"]
    assert len(hive_clients) == 1  # no duplicate Client


@pytest.mark.parametrize("variant", ["  Hive ASMBLD  ", "hive asmbld", "Hive   ASMBLD", "HIVE ASMBLD"])
async def test_client_matching_is_conservative_normalized_exact_not_fuzzy(variant):
    service, client_crm_service = await _services()
    first = await service.onboard(_payload(sale_id="sale-001", docusign_envelope_id="env-001"))
    second = await service.onboard(
        _payload(sale_id="sale-002", docusign_envelope_id="env-002", client_company=variant)
    )
    assert first.client_id == second.client_id
    assert second.created_new_client is False


async def test_client_matching_never_merges_genuinely_different_names():
    service, client_crm_service = await _services()
    first = await service.onboard(_payload(sale_id="sale-001", docusign_envelope_id="env-001"))
    second = await service.onboard(
        _payload(sale_id="sale-002", docusign_envelope_id="env-002", client_company="Hive ASMBLD Inc")
    )
    assert first.client_id != second.client_id
    assert second.created_new_client is True


# --- Contact matching/creation ---------------------------------------------


async def test_new_contact_is_created_with_source_sale_bot():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload())
    assert result.created_new_contact is True
    contact = await client_crm_service.crm_contact_store.get(result.crm_contact_id)
    assert contact.source == "sale_bot"
    assert contact.first_name == "Jane"
    assert contact.last_name == "Doe"
    assert contact.email == "jane@hiveasmbld.com"


async def test_existing_contact_by_normalized_email_is_reused_not_duplicated():
    service, client_crm_service = await _services()
    now = datetime.now(timezone.utc)
    existing = CrmContact(
        crm_contact_id="existing-1",
        created_at=now,
        updated_at=now,
        first_name="Jane",
        last_name="Doe",
        email="jane@hiveasmbld.com",
    )
    await client_crm_service.crm_contact_store.create(existing)

    result = await service.onboard(_payload(contact_email="  JANE@HiveASMBLD.com  "))
    assert result.created_new_contact is False
    assert result.crm_contact_id == "existing-1"

    all_contacts = await client_crm_service.crm_contact_store.list()
    assert len(all_contacts) == 1  # no duplicate person record


async def test_contact_create_race_is_handled_by_refetching_not_failing():
    """Simulates the create_contact() ValueError race: another request
    creates the same email between our lookup and our create call. The
    onboarding call must still succeed, reusing whichever Contact now
    exists, never bubbling the race up as a failure."""
    service, client_crm_service = await _services()

    real_create_contact = service.crm_service.create_contact

    async def racy_create_contact(fields):
        now = datetime.now(timezone.utc)
        # Someone else "wins" the race first.
        await client_crm_service.crm_contact_store.create(
            CrmContact(crm_contact_id="racer-1", created_at=now, updated_at=now, email=fields["email"])
        )
        return await real_create_contact(fields)

    service.crm_service.create_contact = racy_create_contact

    result = await service.onboard(_payload())
    assert result.crm_contact_id == "racer-1"
    assert result.created_new_contact is False


# --- Signer vs primary contact ---------------------------------------------


async def test_signer_different_from_primary_contact_is_preserved_but_never_creates_a_second_contact():
    service, client_crm_service = await _services()
    result = await service.onboard(
        _payload(contact_email="primary@hiveasmbld.com", signer_name="Someone Else", signer_email="signer@hiveasmbld.com")
    )

    engagement = await client_crm_service.engagement_store.get(result.engagement_id)
    assert engagement.signer_name == "Someone Else"
    assert engagement.signer_email == "signer@hiveasmbld.com"

    contact = await client_crm_service.crm_contact_store.get(result.crm_contact_id)
    assert contact.email == "primary@hiveasmbld.com"  # the PRIMARY contact, not the signer

    all_contacts = await client_crm_service.crm_contact_store.list()
    assert len(all_contacts) == 1  # signing never creates a second Contact


async def test_signer_same_as_primary_contact_still_just_stores_both_fields():
    service, client_crm_service = await _services()
    result = await service.onboard(
        _payload(contact_email="same@hiveasmbld.com", signer_name="Jane Doe", signer_email="same@hiveasmbld.com")
    )
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)
    assert engagement.signer_email == "same@hiveasmbld.com"
    all_contacts = await client_crm_service.crm_contact_store.list()
    assert len(all_contacts) == 1


# --- Engagement mapping ------------------------------------------------


async def test_engagement_commercial_fields_mapped_correctly():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload(qb_amount=12345.67))
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)

    assert engagement.fee == 12345.67
    assert engagement.contract_status == EngagementContractStatus.SIGNED
    assert engagement.payment_status == EngagementPaymentStatus.PAID
    assert engagement.location == "Austin"
    assert engagement.owner == "Chris"
    assert engagement.status == EngagementStatus.PLANNED  # "Tentative" -- never assumed CONFIRMED
    assert engagement.contract_url is None  # not verified stable -- never populated
    assert engagement.signed_date is None  # not reliably obtainable here -- never guessed


async def test_engagement_external_ids_all_preserved():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload())
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)

    assert engagement.sale_id == "sale-001"
    assert engagement.docusign_envelope_id == "env-001"
    assert engagement.mercury_invoice_id == "merc-001"
    assert engagement.qb_invoice_id == "qb-inv-001"
    assert engagement.qb_customer_id == "qb-cust-001"


async def test_referral_source_and_special_terms_preserved_on_the_engagement():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload(referral_source="LinkedIn", special_terms="Net 30"))
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)
    assert engagement.referral_source == "LinkedIn"
    assert engagement.special_terms == "Net 30"


async def test_malformed_event_date_does_not_invent_a_date_and_surfaces_a_warning():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload(event_date="sometime next quarter"))
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)

    assert engagement.engagement_date is None
    assert engagement.event_date_raw == "sometime next quarter"
    assert result.event_date_parsed is False
    assert any("sometime next quarter" in w for w in result.warnings)


async def test_well_formed_event_date_is_parsed_with_no_warning():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload(event_date="10/15/2026"))
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)

    assert str(engagement.engagement_date) == "2026-10-15"
    assert engagement.event_date_raw is None
    assert result.event_date_parsed is True
    assert result.warnings == []


async def test_unknown_service_sold_produces_other_type_and_preserves_raw_value():
    service, client_crm_service = await _services()
    result = await service.onboard(_payload(service_sold="Brand Strategy Workshop"))
    engagement = await client_crm_service.engagement_store.get(result.engagement_id)

    assert engagement.engagement_type == EngagementType.OTHER
    assert engagement.dinner_type is None
    assert engagement.service_sold_raw == "Brand Strategy Workshop"


# --- Idempotency -------------------------------------------------------


async def test_repeated_call_with_the_same_sale_id_is_a_safe_no_op():
    service, client_crm_service = await _services()
    first = await service.onboard(_payload())
    second = await service.onboard(_payload())  # identical payload, simulating a retry/re-poll

    assert second.already_processed is True
    assert second.client_id == first.client_id
    assert second.engagement_id == first.engagement_id

    all_clients = await client_crm_service.client_store.list()
    assert len(all_clients) == 1
    engagements = await client_crm_service.engagement_store.list_for_client(first.client_id)
    assert len(engagements) == 1  # no duplicate Engagement


async def test_repeated_call_after_a_crash_before_sale_bot_marks_complete_is_still_safe():
    """Simulates the Sale Bot calling AstroHub, AstroHub succeeding, then
    crashing before persisting its own "done" flag -- the next retry must
    be indistinguishable from the first successful call's own idempotency
    guarantee."""
    service, client_crm_service = await _services()
    payload = _payload()
    await service.onboard(payload)
    result = await service.onboard(payload)
    result2 = await service.onboard(payload)

    assert result.already_processed is True
    assert result2.already_processed is True
    engagements = await client_crm_service.engagement_store.list_for_client(result.client_id)
    assert len(engagements) == 1


async def test_docusign_envelope_conflict_under_a_different_sale_id_is_rejected():
    service, client_crm_service = await _services()
    await service.onboard(_payload(sale_id="sale-001", docusign_envelope_id="env-shared"))

    with pytest.raises(SaleOnboardingConflict):
        await service.onboard(_payload(sale_id="sale-002", docusign_envelope_id="env-shared"))

    # No second Engagement was created by the rejected call.
    engagements = await client_crm_service.engagement_store.list_for_client(
        (await client_crm_service.find_client_by_normalized_name("Hive ASMBLD")).client_id
    )
    assert len(engagements) == 1


async def test_no_new_crm_contacts_created_on_a_repeated_call():
    service, client_crm_service = await _services()
    await service.onboard(_payload())
    await service.onboard(_payload())
    all_contacts = await client_crm_service.crm_contact_store.list()
    assert len(all_contacts) == 1


# --- ClientContact idempotency ---------------------------------------------


async def test_client_contact_link_is_idempotent_across_two_different_sales_for_the_same_person_and_client():
    service, client_crm_service = await _services()
    first = await service.onboard(_payload(sale_id="sale-001", docusign_envelope_id="env-001"))
    second = await service.onboard(
        _payload(sale_id="sale-002", docusign_envelope_id="env-002", client_company="Hive ASMBLD")
    )

    assert first.client_contact_id == second.client_contact_id
    links = await client_crm_service.client_contact_store.list_for_client(first.client_id)
    assert len(links) == 1  # no duplicate ClientContact relationship
