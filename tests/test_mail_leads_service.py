"""
Leads V1 (2026-09-17) -- MailLeadsService.list_leads()/get_lead_detail().
Plain in-memory stores directly, same convention as
test_mail_inbox_service.py. A "Lead" here is a pure read-side
aggregation over existing MailEnrollment/MailEnrollmentStep/
MailCampaign/MailReply data -- these tests exercise exactly that
join/aggregation logic, never touch persistence.
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
    MailReply,
)
from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.crm_contact_store import MemoryCrmContactStore
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_reply_store import MemoryMailReplyStore
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.mail_leads_service import MailLeadsService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
MAILBOX_ID = "fd090fb1-81e1-4575-94e4-5c7c83fc0137"


def make_campaign(campaign_id: str, name: str, **overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=campaign_id, name=name, status=MailCampaignStatus.ACTIVE, created_at=NOW, updated_at=NOW
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_contact(contact_id: str, first_name: str, last_name: str, email: str, **overrides) -> CrmContact:
    fields = dict(
        crm_contact_id=contact_id,
        created_at=NOW,
        updated_at=NOW,
        first_name=first_name,
        last_name=last_name,
        email=email,
        company="Acme Inc",
        title="CTO",
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
        granted_scopes=[],
        connected_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return Mailbox(**fields)


def make_enrollment(
    enrollment_id: str, campaign_id: str, contact_id: str, email: str, enrolled_at: datetime = NOW, **overrides
) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=contact_id,
        email_at_enrollment=email,
        status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=enrolled_at,
        created_at=enrolled_at,
        assigned_mailbox_id=MAILBOX_ID,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_step(
    enrollment_id: str, campaign_id: str, contact_id: str, step_number: int, **overrides
) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step{step_number}",
        mail_campaign_id=campaign_id,
        enrollment_id=enrollment_id,
        crm_contact_id=contact_id,
        step_id=f"step-{step_number}",
        step_number=step_number,
        subject="Quick hello from Astronomic",
        body="Hi {{first_name}},",
        delay_days=0 if step_number == 1 else 3,
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


def make_reply(enrollment_id: str, campaign_id: str, contact_id: str, email: str, **overrides) -> MailReply:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=contact_id,
        mailbox_id=MAILBOX_ID,
        gmail_thread_id=f"thread-{enrollment_id}",
        gmail_message_id=f"reply-msg-{enrollment_id}",
        reply_email_normalized=email,
        detected_at=NOW,
        created_at=NOW,
    )
    fields.update(overrides)
    return MailReply(**fields)


@pytest_asyncio.fixture
async def stores():
    return {
        "campaign_store": MemoryMailCampaignStore(),
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "contact_store": MemoryCrmContactStore(),
        "reply_store": MemoryMailReplyStore(),
        "mailbox_store": MemoryMailboxStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailLeadsService(**stores)


async def test_contact_with_zero_enrollments_never_appears_as_a_lead(stores, service):
    await stores["contact_store"].create(make_contact("c1", "No", "Enrollment", "no-enrollment@example.com"))
    page = await service.list_leads()
    assert page.total == 0
    assert page.items == []


async def test_same_contact_across_multiple_campaigns_appears_once(stores, service):
    contact = make_contact("c1", "John Adrian", "Cal", "johnadriancal@astronomic.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-a", "ZZTEST Reply Stop Production Verification"))
    await stores["campaign_store"].create(make_campaign("camp-b", "Campaign Manager — 5-Person External Pilot"))
    await stores["enrollment_store"].create(
        make_enrollment("e-a", "camp-a", "c1", contact.email, enrolled_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(
        make_enrollment("e-b", "camp-b", "c1", contact.email, enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    page = await service.list_leads()
    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0].crm_contact_id == "c1"


async def test_campaigns_count_is_correct(stores, service):
    contact = make_contact("c1", "Multi", "Campaign", "multi@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-a", "Campaign A"))
    await stores["campaign_store"].create(make_campaign("camp-b", "Campaign B"))
    await stores["campaign_store"].create(make_campaign("camp-c", "Campaign C"))
    await stores["enrollment_store"].create(make_enrollment("e-a", "camp-a", "c1", contact.email))
    await stores["enrollment_store"].create(make_enrollment("e-b", "camp-b", "c1", contact.email))
    await stores["enrollment_store"].create(make_enrollment("e-c", "camp-c", "c1", contact.email))

    [lead] = (await service.list_leads()).items
    assert lead.campaigns_count == 3


async def test_last_campaign_is_the_most_recently_enrolled_one(stores, service):
    contact = make_contact("c1", "Order", "Test", "order@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-old", "Old Campaign"))
    await stores["campaign_store"].create(make_campaign("camp-new", "New Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e-old", "camp-old", "c1", contact.email, enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(
        make_enrollment("e-new", "camp-new", "c1", contact.email, enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    [lead] = (await service.list_leads()).items
    assert lead.last_campaign_id == "camp-new"
    assert lead.last_campaign_name == "New Campaign"


async def test_status_reflects_the_most_recent_enrollment(stores, service):
    contact = make_contact("c1", "Status", "Test", "status@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-old", "Old Campaign"))
    await stores["campaign_store"].create(make_campaign("camp-new", "New Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment(
            "e-old", "camp-old", "c1", contact.email,
            enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc), status=MailEnrollmentStatus.COMPLETED,
        )
    )
    await stores["enrollment_store"].create(
        make_enrollment(
            "e-new", "camp-new", "c1", contact.email,
            enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc), status=MailEnrollmentStatus.ACTIVE,
        )
    )

    [lead] = (await service.list_leads()).items
    assert lead.status == MailEnrollmentStatus.ACTIVE


async def test_replied_is_true_if_any_enrollment_has_replied(stores, service):
    contact = make_contact("c1", "Replied", "Person", "replied@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-a", "Campaign A"))
    await stores["campaign_store"].create(make_campaign("camp-b", "Campaign B"))
    await stores["enrollment_store"].create(
        make_enrollment("e-a", "camp-a", "c1", contact.email, status=MailEnrollmentStatus.ACTIVE)
    )
    await stores["enrollment_store"].create(
        make_enrollment(
            "e-b", "camp-b", "c1", contact.email, status=MailEnrollmentStatus.REPLIED, replied_at=NOW,
            enrolled_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )
    )

    [lead] = (await service.list_leads()).items
    assert lead.replied is True


async def test_replied_is_false_when_no_enrollment_has_replied(stores, service):
    contact = make_contact("c1", "Not", "Replied", "notreplied@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-a", "Campaign A"))
    await stores["enrollment_store"].create(make_enrollment("e-a", "camp-a", "c1", contact.email))

    [lead] = (await service.list_leads()).items
    assert lead.replied is False


async def _seed_five_pilot_leads(stores):
    await stores["campaign_store"].create(make_campaign("pilot", "Campaign Manager — 5-Person External Pilot"))
    people = [
        ("c-john", "John Adrian", "Cal", "johnadriancal@astronomic.com", MailEnrollmentStatus.REPLIED, True),
        ("c-max", "Maximus", "Romy", "maximusromy1165@gmail.com", MailEnrollmentStatus.REPLIED, True),
        ("c-brendan", "Brendan", "Hayes", "brendan@bizdevdinners.com", MailEnrollmentStatus.REPLIED, True),
        ("c-chris", "Chris", "Beaman", "chris@galaxysway.com", MailEnrollmentStatus.ACTIVE, False),
        ("c-victoria", "Victoria", "Bennett", "victoria@astronomicconnect.com", MailEnrollmentStatus.ACTIVE, False),
    ]
    for contact_id, first, last, email, status, replied in people:
        await stores["contact_store"].create(make_contact(contact_id, first, last, email))
        kwargs = {"status": status}
        if replied:
            kwargs["replied_at"] = NOW
        await stores["enrollment_store"].create(make_enrollment(f"e-{contact_id}", "pilot", contact_id, email, **kwargs))
    return people


async def test_search_by_name_email_company_and_campaign(stores, service):
    await _seed_five_pilot_leads(stores)

    by_name = await service.list_leads(q="Maximus")
    assert [lead.crm_contact_id for lead in by_name.items] == ["c-max"]

    by_email = await service.list_leads(q="brendan@bizdevdinners.com")
    assert [lead.crm_contact_id for lead in by_email.items] == ["c-brendan"]

    by_company = await service.list_leads(q="acme")  # every fixture contact has company="Acme Inc"
    assert by_company.total == 5

    by_campaign = await service.list_leads(q="5-Person External Pilot")
    assert by_campaign.total == 5


async def test_status_filter(stores, service):
    await _seed_five_pilot_leads(stores)
    replied_only = await service.list_leads(status="replied")
    assert replied_only.total == 3
    assert all(lead.status == MailEnrollmentStatus.REPLIED for lead in replied_only.items)

    active_only = await service.list_leads(status="active")
    assert active_only.total == 2


async def test_campaign_filter_matches_any_of_the_leads_campaigns_not_only_the_latest(stores, service):
    contact = make_contact("c1", "Two", "Campaigns", "two@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-old", "Old Campaign"))
    await stores["campaign_store"].create(make_campaign("camp-new", "New Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e-old", "camp-old", "c1", contact.email, enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(
        make_enrollment("e-new", "camp-new", "c1", contact.email, enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    # last_campaign_id is camp-new, but filtering by the OLDER campaign must still surface this lead.
    page = await service.list_leads(campaign_id="camp-old")
    assert page.total == 1
    assert page.items[0].crm_contact_id == "c1"


async def test_replied_filter(stores, service):
    await _seed_five_pilot_leads(stores)
    page = await service.list_leads(replied=True)
    assert page.total == 3
    page_false = await service.list_leads(replied=False)
    assert page_false.total == 2


async def test_pagination(stores, service):
    await _seed_five_pilot_leads(stores)
    page1 = await service.list_leads(page=1, page_size=2, sort_by="name", sort_dir="asc")
    page2 = await service.list_leads(page=2, page_size=2, sort_by="name", sort_dir="asc")
    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert page1.page == 1
    assert page2.page == 2
    ids_page1 = {lead.crm_contact_id for lead in page1.items}
    ids_page2 = {lead.crm_contact_id for lead in page2.items}
    assert ids_page1.isdisjoint(ids_page2)


async def test_default_sort_is_newest_activity_first(stores, service):
    contact_a = make_contact("c-a", "Older", "Activity", "older@example.com")
    contact_b = make_contact("c-b", "Newer", "Activity", "newer@example.com")
    await stores["contact_store"].create(contact_a)
    await stores["contact_store"].create(contact_b)
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e-a", "camp", "c-a", contact_a.email, enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(
        make_enrollment("e-b", "camp", "c-b", contact_b.email, enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    page = await service.list_leads()  # default sort_by="last_activity", sort_dir="desc"
    assert [lead.crm_contact_id for lead in page.items] == ["c-b", "c-a"]


async def test_last_activity_prefers_step_sent_at_over_enrolled_at(stores, service):
    contact = make_contact("c1", "Activity", "Test", "activity@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e1", "camp", "c1", contact.email, enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    later_send = datetime(2026, 9, 17, tzinfo=timezone.utc)
    await stores["enrollment_step_store"].create(make_step("e1", "camp", "c1", 1, sent_at=later_send))

    [lead] = (await service.list_leads()).items
    assert lead.last_activity_at == later_send


async def test_lead_detail_shows_multiple_campaign_history_entries(stores, service):
    contact = make_contact("c1", "History", "Test", "history@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-a", "Campaign A"))
    await stores["campaign_store"].create(make_campaign("camp-b", "Campaign B"))
    await stores["enrollment_store"].create(
        make_enrollment("e-a", "camp-a", "c1", contact.email, enrolled_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    )
    await stores["enrollment_store"].create(
        make_enrollment("e-b", "camp-b", "c1", contact.email, enrolled_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
    )

    detail = await service.get_lead_detail("c1")
    assert detail is not None
    assert detail.campaigns_count == 2
    assert len(detail.campaign_history) == 2
    # newest first
    assert detail.campaign_history[0].mail_campaign_id == "camp-b"
    assert detail.campaign_history[1].mail_campaign_id == "camp-a"


async def test_get_lead_detail_returns_none_for_a_contact_with_no_enrollments(stores, service):
    await stores["contact_store"].create(make_contact("c1", "No", "Leads", "noleads@example.com"))
    assert await service.get_lead_detail("c1") is None


async def test_get_lead_detail_returns_none_for_an_unknown_contact_id(stores, service):
    assert await service.get_lead_detail("does-not-exist") is None


async def test_step_history_is_correct_sent_then_skipped_replied(stores, service):
    contact = make_contact("c1", "Steps", "Test", "steps@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e1", "camp", "c1", contact.email, status=MailEnrollmentStatus.REPLIED, replied_at=NOW)
    )
    await stores["enrollment_step_store"].create(make_step("e1", "camp", "c1", 1, status=MailEnrollmentStepStatus.SENT))
    await stores["enrollment_step_store"].create(
        make_step(
            "e1", "camp", "c1", 2, status=MailEnrollmentStepStatus.SKIPPED_REPLIED, sent_at=None, rendered_subject=None
        )
    )

    detail = await service.get_lead_detail("c1")
    [entry] = detail.campaign_history
    assert [s.status for s in entry.steps] == [MailEnrollmentStepStatus.SENT, MailEnrollmentStepStatus.SKIPPED_REPLIED]
    assert entry.steps[0].step_number == 1
    assert entry.steps[1].step_number == 2


async def test_reply_links_to_the_same_enrollment_id_inbox_uses(stores, service):
    contact = make_contact("c1", "Reply", "Link", "replylink@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(
        make_enrollment("e1", "camp", "c1", contact.email, status=MailEnrollmentStatus.REPLIED, replied_at=NOW)
    )
    await stores["reply_store"].create(make_reply("e1", "camp", "c1", contact.email))

    detail = await service.get_lead_detail("c1")
    [entry] = detail.campaign_history
    assert entry.has_reply is True
    assert entry.reply_enrollment_id == "e1"
    assert detail.replies_count == 1


async def test_no_reply_means_has_reply_false_and_no_enrollment_id(stores, service):
    contact = make_contact("c1", "No", "Reply", "noreply@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(make_enrollment("e1", "camp", "c1", contact.email))

    detail = await service.get_lead_detail("c1")
    [entry] = detail.campaign_history
    assert entry.has_reply is False
    assert entry.reply_enrollment_id is None
    assert detail.replies_count == 0


async def test_archived_campaign_still_appears_in_historical_lead_view(stores, service):
    contact = make_contact("c1", "Archived", "History", "archived@example.com")
    await stores["contact_store"].create(contact)
    await stores["campaign_store"].create(make_campaign("camp-archived", "Old Test Campaign", status=MailCampaignStatus.ARCHIVED))
    await stores["enrollment_store"].create(make_enrollment("e1", "camp-archived", "c1", contact.email))

    page = await service.list_leads()
    assert page.total == 1
    assert page.items[0].crm_contact_id == "c1"

    detail = await service.get_lead_detail("c1")
    assert detail.campaign_history[0].campaign_status == MailCampaignStatus.ARCHIVED


async def test_sender_mailbox_email_resolved_for_campaign_history(stores, service):
    contact = make_contact("c1", "Mailbox", "Test", "mailbox@example.com")
    await stores["contact_store"].create(contact)
    await stores["mailbox_store"].create(make_mailbox())
    await stores["campaign_store"].create(make_campaign("camp", "Campaign"))
    await stores["enrollment_store"].create(make_enrollment("e1", "camp", "c1", contact.email))

    detail = await service.get_lead_detail("c1")
    assert detail.campaign_history[0].mailbox_email == "victoria@useastronomic.com"
