"""
Tests for EmailIntakeService -- ingestion idempotency, contact matching
(reusing CrmService.classify_match() verbatim), deterministic extraction,
manual match, and the approve()/reject() human-approval pipeline
(including stale-review protection and partial approval).

Data-integrity tests at the bottom of this file are the single most
important guarantee in this feature: NOTHING before an explicit Approve
call may ever change a CrmContact.
"""

import uuid

import pytest
import pytest_asyncio

from app.models.crm import CustomFieldType
from app.models.email_intake import (
    EmailAttachmentMeta,
    EmailIntakeStatus,
    EmailIntakeWebhookRequest,
)
from app.repositories.email_intake_store import MemoryEmailIntakeStore
from app.services.crm_service import CrmService
from app.services.email_intake_service import (
    EmailIntakeInvalidStateError,
    EmailIntakeItemNotFound,
    EmailIntakeService,
)
from app.models.crm import CrmCustomFieldDefinition
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)


def make_payload(**overrides) -> EmailIntakeWebhookRequest:
    defaults = dict(
        gmail_message_id=f"msg-{uuid.uuid4()}",
        sender="Amos Ben-Meir <amos@example.com>",
        subject="Update",
        body_text="Just a quick note.",
        received_at=now(),
    )
    defaults.update(overrides)
    return EmailIntakeWebhookRequest(**defaults)


def make_custom_field_definition(field_key: str, label: str, options: list[str]) -> CrmCustomFieldDefinition:
    ts = now()
    return CrmCustomFieldDefinition(
        crm_custom_field_id=str(uuid.uuid4()),
        field_key=field_key,
        label=label,
        field_type=CustomFieldType.MULTI_SELECT,
        options=options,
        created_at=ts,
        updated_at=ts,
    )


@pytest_asyncio.fixture
async def crm_service():
    service = CrmService()
    await service.custom_field_store.create(make_custom_field_definition("investment_industry", "Investment Industry", []))
    await service.custom_field_store.create(
        make_custom_field_definition(
            "check_size_personal",
            "Check Size (Personal)",
            ["$25k-$50k", "$50k-$100k", "$100k-$250k", "$250k-$500k", "$500k-$1M"],
        )
    )
    await service.custom_field_store.create(
        make_custom_field_definition(
            "check_size_institutional", "Check Size (Institutional)", ["$100k-$250k", "$250k-$500k"]
        )
    )
    return service


@pytest_asyncio.fixture
async def store():
    return MemoryEmailIntakeStore()


@pytest_asyncio.fixture
async def service(store, crm_service):
    # Shares crm_service's own activity_log instance -- exactly what
    # main.py's real wiring does, and required for
    # test_approve_uses_one_crm_update_and_emits_both_events to see BOTH
    # the email_intake.approved event and update_contact()'s own
    # contact.updated event in the same log.
    return EmailIntakeService(store=store, crm_service=crm_service, activity_log=crm_service.activity_log)


async def make_contact(crm_service, **overrides):
    defaults = dict(email="amos@example.com", first_name="Amos", last_name="Ben-Meir", company="Massive Capital")
    defaults.update(overrides)
    return await crm_service.create_contact(defaults)


# ---- ingestion / idempotency ------------------------------------------


@pytest.mark.asyncio
async def test_ingest_creates_item(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(make_payload())
    assert result.already_processed is False
    item = await service.get_item(result.intake_id)
    assert item.sender == "Amos Ben-Meir <amos@example.com>"


@pytest.mark.asyncio
async def test_duplicate_gmail_message_id_returns_existing_item_not_new_one(service, crm_service):
    await make_contact(crm_service)
    payload = make_payload()
    first = await service.ingest(payload)
    second = await service.ingest(payload)
    assert second.intake_id == first.intake_id
    assert second.already_processed is True
    items = await service.list_items()
    assert len(items) == 1


@pytest.mark.asyncio
async def test_duplicate_does_not_emit_duplicate_activity_events(service, crm_service):
    await make_contact(crm_service)
    payload = make_payload()
    await service.ingest(payload)
    await service.ingest(payload)
    events = await service.activity_log.store.list()
    email_intake_events = [e for e in events if e.event_type.startswith("email_intake.")]
    assert len(email_intake_events) == 1


@pytest.mark.asyncio
async def test_ingest_with_no_confident_extraction_is_still_pending_review_not_error(service, crm_service):
    """The audit's explicit adjustment: no extracted fields must never be
    treated as a processing failure."""
    await make_contact(crm_service)
    result = await service.ingest(make_payload(body_text="Just checking in, no updates."))
    assert result.status == EmailIntakeStatus.PENDING_REVIEW
    item = await service.get_item(result.intake_id)
    assert item.proposal == []
    assert item.error_message is None


# ---- matching -----------------------------------------------------------


@pytest.mark.asyncio
async def test_matches_by_email(service, crm_service):
    contact = await make_contact(crm_service)
    result = await service.ingest(make_payload(sender="Amos Ben-Meir <amos@example.com>"))
    item = await service.get_item(result.intake_id)
    assert item.matched_contact_id == contact.crm_contact_id
    assert item.matched_on == "email"
    assert item.status == EmailIntakeStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_matches_by_linkedin_url_in_body(service, crm_service):
    contact = await make_contact(
        crm_service, email=None, linkedin_url="https://www.linkedin.com/in/amos-ben-meir"
    )
    result = await service.ingest(
        make_payload(
            sender="unrelated@example.com",  # bare address, no display name -- nothing to conflict on
            body_text="See his profile: https://www.linkedin.com/in/amos-ben-meir for details.",
        )
    )
    item = await service.get_item(result.intake_id)
    assert item.matched_contact_id == contact.crm_contact_id
    assert item.matched_on == "linkedin_url"


@pytest.mark.asyncio
async def test_identity_conflict_downgrades_to_needs_match(service, crm_service):
    """Same rule CSV import already relies on: a shared email does not
    override an obvious 'this is actually someone else' name mismatch."""
    await make_contact(crm_service, email="amos@example.com", first_name="Amos", last_name="BenMeir")
    result = await service.ingest(
        make_payload(sender="Totally Different Person <amos@example.com>")
    )
    item = await service.get_item(result.intake_id)
    assert item.status == EmailIntakeStatus.NEEDS_MATCH
    assert item.matched_contact_id is None
    assert item.matched_on == "email_conflicting_identity"


@pytest.mark.asyncio
async def test_no_match_is_needs_match_never_auto_creates_contact(service, crm_service):
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    item = await service.get_item(result.intake_id)
    assert item.status == EmailIntakeStatus.NEEDS_MATCH
    assert item.matched_contact_id is None
    contacts = await crm_service.list_contacts()
    assert contacts.total == 0  # never auto-created


@pytest.mark.asyncio
async def test_manual_match_generates_proposal_and_moves_to_pending_review(service, crm_service):
    contact = await make_contact(crm_service, email="someone@example.com")
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    item = await service.manual_match(result.intake_id, contact.crm_contact_id)
    assert item.status == EmailIntakeStatus.PENDING_REVIEW
    assert item.matched_contact_id == contact.crm_contact_id
    assert item.matched_on == "manual"


@pytest.mark.asyncio
async def test_manual_match_on_non_needs_match_item_raises(service, crm_service):
    contact = await make_contact(crm_service)
    result = await service.ingest(make_payload())  # matches by email -> PENDING_REVIEW already
    with pytest.raises(EmailIntakeInvalidStateError):
        await service.manual_match(result.intake_id, contact.crm_contact_id)


# ---- extraction -----------------------------------------------------------


@pytest.mark.asyncio
async def test_extracts_explicit_company_change(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "company")
    assert change.current_value == "Massive Capital"
    assert change.proposed_value == "Massive Ventures"


@pytest.mark.asyncio
async def test_extracts_exact_industry_and_controlled_alias(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(
        make_payload(body_text="He's mostly interested in healthcare and AI these days.")
    )
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "custom:investment_industry")
    assert set(change.proposed_value) == {
        "Healthcare & HealthTech",
        "Artificial Intelligence / Machine Learning",
    }
    assert change.field_label == "Investment Industry"


@pytest.mark.asyncio
async def test_industry_alias_resolves_only_to_canonical_option(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(make_payload(body_text="He's excited about ai right now."))
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "custom:investment_industry")
    assert change.proposed_value == ["Artificial Intelligence / Machine Learning"]


@pytest.mark.asyncio
async def test_extracts_exact_check_size_option(service, crm_service):
    await make_contact(crm_service, custom_fields={"check_size_personal": ["$100k-$250k"]})
    result = await service.ingest(
        make_payload(body_text="He's writing $250k-$500k checks now.")
    )
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "custom:check_size_personal")
    assert change.current_value == ["$100k-$250k"]
    assert change.proposed_value == ["$100k-$250k", "$250k-$500k"]


@pytest.mark.asyncio
async def test_unrecognized_check_size_range_is_never_guessed(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(make_payload(body_text="Somewhere between $25k-$100k probably."))
    item = await service.get_item(result.intake_id)
    assert not any(c.field_key == "custom:check_size_personal" for c in item.proposal)


@pytest.mark.asyncio
async def test_extracts_email_when_currently_empty(service, crm_service):
    await make_contact(crm_service, email=None)
    result = await service.ingest(
        make_payload(sender="Stranger <ignored@example.com>", body_text="Reach me at amos@newco.com anytime.")
    )
    item = await service.get_item(result.intake_id)
    # No email match possible (contact had none) -- falls to NEEDS_MATCH, proposal empty.
    assert item.status == EmailIntakeStatus.NEEDS_MATCH


@pytest.mark.asyncio
async def test_extracts_linkedin_change_for_matched_contact(service, crm_service):
    await make_contact(crm_service, linkedin_url=None)
    result = await service.ingest(
        make_payload(body_text="My LinkedIn is https://www.linkedin.com/in/amos-new")
    )
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "linkedin_url")
    assert change.proposed_value == "https://www.linkedin.com/in/amos-new"


@pytest.mark.asyncio
async def test_extracts_phone_change(service, crm_service):
    await make_contact(crm_service, phone=None)
    result = await service.ingest(make_payload(body_text="Call me at 512-555-0134 anytime."))
    item = await service.get_item(result.intake_id)
    change = next(c for c in item.proposal if c.field_key == "phone")
    assert change.proposed_value == "512-555-0134"


@pytest.mark.asyncio
async def test_unsupported_narrative_prose_produces_no_invented_change(service, crm_service):
    """Category C: subjective/narrative language must never become a
    structured field update."""
    await make_contact(crm_service)
    result = await service.ingest(
        make_payload(
            body_text="He's leaning more toward enterprise software lately and she'll "
            "probably write something around half a million."
        )
    )
    item = await service.get_item(result.intake_id)
    assert item.proposal == []
    assert item.status == EmailIntakeStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_attachment_metadata_stored_but_not_processed(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(
        make_payload(attachments=[EmailAttachmentMeta(filename="deck.pdf", content_type="application/pdf", size_bytes=1024)])
    )
    item = await service.get_item(result.intake_id)
    assert item.attachments[0].filename == "deck.pdf"
    # No attachment-derived field ever appears in the proposal.
    assert item.proposal == []


# ---- approval -------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_applies_only_selected_fields(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital", phone=None)
    result = await service.ingest(
        make_payload(body_text="Amos is now at Massive Ventures. Call him at 512-555-0134.")
    )
    item = await service.get_item(result.intake_id)
    field_keys = {c.field_key for c in item.proposal}
    assert field_keys == {"company", "phone"}

    approve_result = await service.approve(result.intake_id, ["company"])
    assert approve_result.status == "approved"

    updated_contact = await crm_service.get_contact(contact.crm_contact_id)
    assert updated_contact.company == "Massive Ventures"
    assert updated_contact.phone is None  # unchecked field left untouched

    approved_item = await service.get_item(result.intake_id)
    assert approved_item.status == EmailIntakeStatus.APPROVED
    assert approved_item.reviewed_at is not None


@pytest.mark.asyncio
async def test_approve_updates_custom_fields_correctly(service, crm_service):
    contact = await make_contact(crm_service, custom_fields={"check_size_personal": ["$100k-$250k"]})
    result = await service.ingest(make_payload(body_text="He's writing $250k-$500k checks now."))
    approve_result = await service.approve(result.intake_id, ["custom:check_size_personal"])
    assert approve_result.status == "approved"
    updated = await crm_service.get_contact(contact.crm_contact_id)
    assert updated.custom_fields["check_size_personal"] == ["$100k-$250k", "$250k-$500k"]


@pytest.mark.asyncio
async def test_approve_with_zero_selected_fields_rejected_cleanly(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    with pytest.raises(ValueError):
        await service.approve(result.intake_id, [])


@pytest.mark.asyncio
async def test_approve_unknown_field_key_raises(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    with pytest.raises(ValueError):
        await service.approve(result.intake_id, ["not_a_real_field"])


@pytest.mark.asyncio
async def test_stale_contact_blocks_approval(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))

    # Contact changes AFTER the proposal was generated.
    await crm_service.update_contact(contact.crm_contact_id, {"company": "XYZ Ventures"})

    approve_result = await service.approve(result.intake_id, ["company"])
    assert approve_result.status == "stale"
    assert len(approve_result.conflicts) == 1
    conflict = approve_result.conflicts[0]
    assert conflict.reviewed_value == "Massive Capital"
    assert conflict.live_value == "XYZ Ventures"
    assert conflict.proposed_value == "Massive Ventures"

    # Nothing was written -- contact keeps the value set by the "someone else edited it" step.
    unchanged = await crm_service.get_contact(contact.crm_contact_id)
    assert unchanged.company == "XYZ Ventures"

    # Item remains PENDING_REVIEW, but its proposal's current_value is refreshed.
    refreshed_item = await service.get_item(result.intake_id)
    assert refreshed_item.status == EmailIntakeStatus.PENDING_REVIEW
    refreshed_change = next(c for c in refreshed_item.proposal if c.field_key == "company")
    assert refreshed_change.current_value == "XYZ Ventures"


@pytest.mark.asyncio
async def test_approve_uses_one_crm_update_and_emits_both_events(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.approve(result.intake_id, ["company"])

    events = await service.activity_log.store.list()
    approved_events = [e for e in events if e.event_type == "email_intake.approved"]
    contact_updated_events = [e for e in events if e.event_type == "contact.updated"]
    assert len(approved_events) == 1
    assert len(contact_updated_events) == 1  # the normal CrmService.update_contact() event still fires


@pytest.mark.asyncio
async def test_approve_on_already_approved_item_raises(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.approve(result.intake_id, ["company"])
    with pytest.raises(EmailIntakeInvalidStateError):
        await service.approve(result.intake_id, ["company"])


# ---- rejection -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_never_touches_crm(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    item = await service.reject(result.intake_id)
    assert item.status == EmailIntakeStatus.REJECTED
    assert item.reviewed_at is not None
    unchanged = await crm_service.get_contact(contact.crm_contact_id)
    assert unchanged.company == "Massive Capital"


@pytest.mark.asyncio
async def test_reject_emits_activity_event(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.reject(result.intake_id)
    events = await service.activity_log.store.list()
    rejected_events = [e for e in events if e.event_type == "email_intake.rejected"]
    assert len(rejected_events) == 1


@pytest.mark.asyncio
async def test_reject_retains_item_for_audit(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.reject(result.intake_id)
    item = await service.get_item(result.intake_id)  # still retrievable -- no delete route exists
    assert item.status == EmailIntakeStatus.REJECTED
    assert item.body_text  # original email retained


@pytest.mark.asyncio
async def test_reject_needs_match_item_succeeds_without_a_match(service, crm_service):
    # No CRM contact matches this sender at all -- lands in NEEDS_MATCH,
    # never PENDING_REVIEW. A reviewer must be able to dismiss it as junk
    # without being forced to falsely match it to some contact first.
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    pre_reject = await service.get_item(result.intake_id)
    assert pre_reject.status == EmailIntakeStatus.NEEDS_MATCH
    assert pre_reject.matched_contact_id is None

    item = await service.reject(result.intake_id)
    assert item.status == EmailIntakeStatus.REJECTED
    assert item.reviewed_at is not None
    assert item.matched_contact_id is None  # rejecting never performed or required a match


@pytest.mark.asyncio
async def test_reject_needs_match_item_never_touches_crm(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    before = await crm_service.get_contact(contact.crm_contact_id)
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    assert (await service.get_item(result.intake_id)).status == EmailIntakeStatus.NEEDS_MATCH

    await service.reject(result.intake_id)

    after = await crm_service.get_contact(contact.crm_contact_id)
    assert after == before
    contacts = await crm_service.list_contacts()
    assert contacts.total == 1  # no contact was created either


@pytest.mark.asyncio
async def test_reject_needs_match_item_emits_activity_event(service, crm_service):
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    await service.reject(result.intake_id)
    events = await service.activity_log.store.list()
    rejected_events = [e for e in events if e.event_type == "email_intake.rejected"]
    assert len(rejected_events) == 1


@pytest.mark.asyncio
async def test_reject_on_already_approved_item_raises(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.approve(result.intake_id, ["company"])
    with pytest.raises(EmailIntakeInvalidStateError):
        await service.reject(result.intake_id)


@pytest.mark.asyncio
async def test_reject_on_already_rejected_item_raises(service, crm_service):
    await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.reject(result.intake_id)
    with pytest.raises(EmailIntakeInvalidStateError):
        await service.reject(result.intake_id)


# ---- queue filtering/search -------------------------------------------


@pytest.mark.asyncio
async def test_list_items_newest_first(service, crm_service):
    await make_contact(crm_service)
    first = await service.ingest(make_payload(subject="First"))
    second = await service.ingest(make_payload(subject="Second"))
    items = await service.list_items()
    assert [i.intake_id for i in items] == [second.intake_id, first.intake_id]


@pytest.mark.asyncio
async def test_list_items_filters_by_status(service, crm_service):
    await make_contact(crm_service)
    pending = await service.ingest(make_payload(sender="Amos Ben-Meir <amos@example.com>"))
    needs_match = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    only_pending = await service.list_items(status=EmailIntakeStatus.PENDING_REVIEW)
    assert [i.intake_id for i in only_pending] == [pending.intake_id]
    only_needs_match = await service.list_items(status=EmailIntakeStatus.NEEDS_MATCH)
    assert [i.intake_id for i in only_needs_match] == [needs_match.intake_id]


@pytest.mark.asyncio
async def test_list_items_search_by_sender_subject_and_contact_name(service, crm_service):
    await make_contact(crm_service)
    result = await service.ingest(make_payload(subject="Quarterly check-in"))
    assert [i.intake_id for i in await service.list_items(q="quarterly")] == [result.intake_id]
    assert [i.intake_id for i in await service.list_items(q="amos@example.com")] == [result.intake_id]
    assert [i.intake_id for i in await service.list_items(q="Ben-Meir")] == [result.intake_id]
    assert await service.list_items(q="nonexistent") == []


@pytest.mark.asyncio
async def test_get_missing_item_raises(service):
    with pytest.raises(EmailIntakeItemNotFound):
        await service.get_item("does-not-exist")


# ---- data integrity: the core safety guarantee -------------------------


@pytest.mark.asyncio
async def test_ingestion_never_changes_crm_contact(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    before = await crm_service.get_contact(contact.crm_contact_id)
    await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    after = await crm_service.get_contact(contact.crm_contact_id)
    assert before == after


@pytest.mark.asyncio
async def test_needs_match_state_never_changes_crm(service, crm_service):
    contact = await make_contact(crm_service, email="someone@example.com")
    before = await crm_service.get_contact(contact.crm_contact_id)
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>"))
    assert (await service.get_item(result.intake_id)).status == EmailIntakeStatus.NEEDS_MATCH
    after = await crm_service.get_contact(contact.crm_contact_id)
    assert before == after


@pytest.mark.asyncio
async def test_manual_match_alone_never_changes_crm(service, crm_service):
    contact = await make_contact(crm_service, email="someone@example.com", company="Massive Capital")
    before = await crm_service.get_contact(contact.crm_contact_id)
    result = await service.ingest(make_payload(sender="Stranger <stranger@example.com>", body_text="Now at Massive Ventures."))
    await service.manual_match(result.intake_id, contact.crm_contact_id)
    after = await crm_service.get_contact(contact.crm_contact_id)
    assert before == after  # proposal generated, but contact itself untouched


@pytest.mark.asyncio
async def test_rejected_proposal_never_changes_crm(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    before = await crm_service.get_contact(contact.crm_contact_id)
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    await service.reject(result.intake_id)
    after = await crm_service.get_contact(contact.crm_contact_id)
    assert before == after


@pytest.mark.asyncio
async def test_only_successful_approval_changes_crm(service, crm_service):
    contact = await make_contact(crm_service, company="Massive Capital")
    result = await service.ingest(make_payload(body_text="Amos is now at Massive Ventures."))
    before = await crm_service.get_contact(contact.crm_contact_id)
    approve_result = await service.approve(result.intake_id, ["company"])
    after = await crm_service.get_contact(contact.crm_contact_id)
    assert approve_result.status == "approved"
    assert before != after
    assert after.company == "Massive Ventures"
