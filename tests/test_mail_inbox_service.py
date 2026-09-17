"""
Inbox V1 (2026-09-17) -- MailInboxService.list_replies(), the single
read path behind GET /mail/inbox/replies. Uses plain in-memory stores
directly (no reply-detection worker machinery needed here -- that's
already covered by test_mail_reply_detection.py) since this service is
a pure join/read over rows the caller constructs directly.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.crm import CrmContact
from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailInboxReplyView,
    MailReply,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_reply_store import MemoryMailReplyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.mail_inbox_service import MailInboxService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CAMPAIGN_ID = "1fe98436-b4e5-4459-b415-20714726f469"
MAILBOX_ID = "fd090fb1-81e1-4575-94e4-5c7c83fc0137"


def make_campaign(**overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=CAMPAIGN_ID,
        name="Campaign Manager — 5-Person External Pilot",
        status=MailCampaignStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_contact(crm_contact_id: str, first_name: str, last_name: str, email: str, **overrides) -> CrmContact:
    fields = dict(
        crm_contact_id=crm_contact_id,
        created_at=NOW,
        updated_at=NOW,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )
    fields.update(overrides)
    return CrmContact(**fields)


def make_mailbox(**overrides) -> Mailbox:
    fields = dict(
        mailbox_id=MAILBOX_ID,
        provider=MailboxProvider.GOOGLE,
        email="victoria@useastronomic.com",
        display_name=None,
        status=MailboxStatus.CONNECTED,
        google_user_id="g-victoria",
        granted_scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.metadata",
        ],
        connected_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return Mailbox(**fields)


def make_enrollment(enrollment_id: str, crm_contact_id: str, email: str, **overrides) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=CAMPAIGN_ID,
        crm_contact_id=crm_contact_id,
        email_at_enrollment=email,
        status=MailEnrollmentStatus.REPLIED,
        enrolled_at=NOW,
        created_at=NOW,
        replied_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_step1(enrollment_id: str, crm_contact_id: str, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step1",
        mail_campaign_id=CAMPAIGN_ID,
        enrollment_id=enrollment_id,
        crm_contact_id=crm_contact_id,
        step_id="step-1",
        step_number=1,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        rendered_subject="Quick hello from Astronomic",
        mailbox_id=MAILBOX_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


def make_reply(enrollment_id: str, crm_contact_id: str, email: str, detected_at: datetime = NOW, **overrides) -> MailReply:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=CAMPAIGN_ID,
        crm_contact_id=crm_contact_id,
        mailbox_id=MAILBOX_ID,
        gmail_thread_id=f"thread-{enrollment_id}",
        gmail_message_id=f"reply-msg-{enrollment_id}",
        reply_email_normalized=email,
        detected_at=detected_at,
        created_at=detected_at,
    )
    fields.update(overrides)
    return MailReply(**fields)


@pytest_asyncio.fixture
async def stores():
    return {
        "reply_store": MemoryMailReplyStore(),
        "campaign_store": MemoryMailCampaignStore(),
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "contact_store": MemoryCrmContactStore(),
        "mailbox_store": MemoryMailboxStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailInboxService(**stores)


async def test_empty_inbox_returns_empty_list(service):
    assert await service.list_replies() == []


async def test_john_and_maximus_production_equivalent_replies_both_display(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())

    john = make_contact("9e147f6d-f58f-4171-9d03-1f39751f6291", "John Adrian", "Cal", "johnadriancal@astronomic.com")
    maximus = make_contact("70590a0d-8795-4a78-aa79-01ef9ae00968", "Maximus", "Romy", "maximusromy1165@gmail.com")
    await stores["contact_store"].create(john)
    await stores["contact_store"].create(maximus)

    john_enrollment = make_enrollment("ff5cbede-4785-4f5f-9f32-0d031ce1b7fd", john.crm_contact_id, john.email)
    maximus_enrollment = make_enrollment("47d5fdf0-4aaa-4184-ba6c-ae0aa6ff0dad", maximus.crm_contact_id, maximus.email)
    await stores["enrollment_store"].create(john_enrollment)
    await stores["enrollment_store"].create(maximus_enrollment)

    await stores["enrollment_step_store"].create(make_step1(john_enrollment.enrollment_id, john.crm_contact_id))
    await stores["enrollment_step_store"].create(make_step1(maximus_enrollment.enrollment_id, maximus.crm_contact_id))

    earlier = NOW
    later = datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc)
    await stores["reply_store"].create(make_reply(john_enrollment.enrollment_id, john.crm_contact_id, john.email, detected_at=earlier))
    await stores["reply_store"].create(make_reply(maximus_enrollment.enrollment_id, maximus.crm_contact_id, maximus.email, detected_at=later))

    views = await service.list_replies()
    assert len(views) == 2

    emails = {v.email for v in views}
    assert emails == {"johnadriancal@astronomic.com", "maximusromy1165@gmail.com"}
    names = {v.contact_name for v in views}
    assert names == {"John Adrian Cal", "Maximus Romy"}


async def test_newest_reply_sorts_first(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact_a = make_contact("contact-a", "Alice", "Older", "alice@example.com")
    contact_b = make_contact("contact-b", "Bob", "Newer", "bob@example.com")
    await stores["contact_store"].create(contact_a)
    await stores["contact_store"].create(contact_b)
    enrollment_a = make_enrollment("enroll-a", contact_a.crm_contact_id, contact_a.email)
    enrollment_b = make_enrollment("enroll-b", contact_b.crm_contact_id, contact_b.email)
    await stores["enrollment_store"].create(enrollment_a)
    await stores["enrollment_store"].create(enrollment_b)
    await stores["enrollment_step_store"].create(make_step1(enrollment_a.enrollment_id, contact_a.crm_contact_id))
    await stores["enrollment_step_store"].create(make_step1(enrollment_b.enrollment_id, contact_b.crm_contact_id))

    older = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    # Deliberately created out of chronological order to prove sorting isn't
    # accidentally just insertion order.
    await stores["reply_store"].create(make_reply(enrollment_a.enrollment_id, contact_a.crm_contact_id, contact_a.email, detected_at=older))
    await stores["reply_store"].create(make_reply(enrollment_b.enrollment_id, contact_b.crm_contact_id, contact_b.email, detected_at=newer))

    views = await service.list_replies()
    assert [v.contact_name for v in views] == ["Bob Newer", "Alice Older"]


async def test_correct_campaign_name_and_status_attached(stores, service):
    await stores["campaign_store"].create(make_campaign(name="Campaign Manager — 5-Person External Pilot", status=MailCampaignStatus.ACTIVE))
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.mail_campaign_id == CAMPAIGN_ID
    assert view.campaign_name == "Campaign Manager — 5-Person External Pilot"
    assert view.campaign_status == MailCampaignStatus.ACTIVE


async def test_replied_at_matches_detected_at(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    detected = datetime(2026, 9, 17, 4, 50, 0, tzinfo=timezone.utc)
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email, detected_at=detected))

    [view] = await service.list_replies()
    assert view.replied_at == detected


async def test_contact_name_and_email_displayed(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Brendan", "Hayes", "brendan@bizdevdinners.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.contact_name == "Brendan Hayes"
    assert view.email == "brendan@bizdevdinners.com"


async def test_completed_and_paused_campaign_replies_remain_visible(stores, service):
    await stores["campaign_store"].create(make_campaign(status=MailCampaignStatus.PAUSED))
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Victoria", "Bennett", "victoria@astronomicconnect.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    views = await service.list_replies()
    assert len(views) == 1
    assert views[0].campaign_status == MailCampaignStatus.PAUSED

    # A later archive/complete doesn't hide it either.
    campaign = await stores["campaign_store"].get(CAMPAIGN_ID)
    await stores["campaign_store"].save(campaign.model_copy(update={"status": MailCampaignStatus.ARCHIVED}))
    views_after = await service.list_replies()
    assert len(views_after) == 1
    assert views_after[0].campaign_status == MailCampaignStatus.ARCHIVED


async def test_repeated_reply_polling_never_produces_a_duplicate_row(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))

    reply = make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email)
    first = await stores["reply_store"].create(reply)
    second = await stores["reply_store"].create(reply)
    assert first is True
    assert second is False

    views = await service.list_replies()
    assert len(views) == 1


async def test_missing_contact_and_mailbox_degrade_gracefully_not_hidden(stores, service):
    await stores["campaign_store"].create(make_campaign())
    # Deliberately never create a contact or mailbox row.
    enrollment = make_enrollment("e1", "ghost-contact", "ghost@example.com")
    await stores["enrollment_store"].create(enrollment)
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, "ghost-contact", "ghost@example.com"))

    [view] = await service.list_replies()
    assert view.contact_name is None
    assert view.mailbox_email is None
    assert view.email == "ghost@example.com"


async def test_missing_enrollment_or_campaign_drops_the_row_rather_than_fabricating_one(stores, service):
    # No campaign, no enrollment ever created for this reply.
    await stores["reply_store"].create(make_reply("orphan-enrollment", "orphan-contact", "orphan@example.com"))
    assert await service.list_replies() == []


async def test_subject_falls_back_from_rendered_subject_to_frozen_subject(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    # A step predating the rendered_subject field (None) still falls back
    # to the frozen `subject` -- see MailEnrollmentStep's own docstring.
    await stores["enrollment_step_store"].create(
        make_step1(enrollment.enrollment_id, contact.crm_contact_id, rendered_subject=None, subject="Quick hello from Astronomic")
    )
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.subject == "Quick hello from Astronomic"


async def test_skipped_step_numbers_reflect_steps_the_reply_actually_skipped(stores, service):
    await stores["campaign_store"].create(make_campaign())
    await stores["mailbox_store"].create(make_mailbox())
    contact = make_contact("c1", "Chris", "Beaman", "chris@galaxysway.com")
    await stores["contact_store"].create(contact)
    enrollment = make_enrollment("e1", contact.crm_contact_id, contact.email)
    await stores["enrollment_store"].create(enrollment)
    await stores["enrollment_step_store"].create(make_step1(enrollment.enrollment_id, contact.crm_contact_id))
    await stores["enrollment_step_store"].create(
        make_step1(
            enrollment.enrollment_id, contact.crm_contact_id,
            enrollment_step_id="e1-step2", step_id="step-2", step_number=2,
            status=MailEnrollmentStepStatus.SKIPPED_REPLIED, sent_at=None, rendered_subject=None,
        )
    )
    await stores["enrollment_step_store"].create(
        make_step1(
            enrollment.enrollment_id, contact.crm_contact_id,
            enrollment_step_id="e1-step3", step_id="step-3", step_number=3,
            status=MailEnrollmentStepStatus.SKIPPED_REPLIED, sent_at=None, rendered_subject=None,
        )
    )
    await stores["reply_store"].create(make_reply(enrollment.enrollment_id, contact.crm_contact_id, contact.email))

    [view] = await service.list_replies()
    assert view.skipped_step_numbers == [2, 3]


async def test_inbox_reply_view_has_no_body_or_unread_state():
    """Structural guard: Inbox V1 must never grow a fake body/snippet or
    an invented unread/read field -- gmail.metadata never provided body
    content, and there is no read/unread model anywhere in this codebase."""
    field_names = set(MailInboxReplyView.model_fields.keys())
    assert "body" not in field_names
    assert "snippet" not in field_names
    assert "unread" not in field_names
    assert "read" not in field_names
