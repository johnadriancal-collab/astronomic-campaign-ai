"""
MailCampaignStatsService -- campaign detail stats strip (2026-09-17,
real Open rate added 2026-09-18). Reply rate / Unsub rate / Open rate --
see that service's own module docstring for why Bounce rate has no field
at all (nothing tracks it for Astronomic Mail) and for the exact Open
rate query (shared with MailCampaignListService).
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import (
    MailCampaign,
    MailCampaignStatus,
    MailEnrollment,
    MailEnrollmentStatus,
    MailEnrollmentStep,
    MailEnrollmentStepStatus,
    MailSuppression,
    MailSuppressionReason,
)
from app.repositories.mail_campaign_store import MemoryMailCampaignStore
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_enrollment_store import MemoryMailEnrollmentStore
from app.repositories.mail_open_event_store import MemoryMailOpenEventStore
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.services.mail_campaign_service import MailCampaignNotFound
from app.services.mail_campaign_stats_service import MailCampaignStatsService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def make_campaign(campaign_id: str = "c1", **overrides) -> MailCampaign:
    fields = dict(
        mail_campaign_id=campaign_id, name="Test Campaign", status=MailCampaignStatus.ACTIVE, created_at=NOW, updated_at=NOW
    )
    fields.update(overrides)
    return MailCampaign(**fields)


def make_enrollment(enrollment_id: str, campaign_id: str, contact_id: str, email: str, **overrides) -> MailEnrollment:
    fields = dict(
        enrollment_id=enrollment_id,
        mail_campaign_id=campaign_id,
        crm_contact_id=contact_id,
        email_at_enrollment=email,
        status=MailEnrollmentStatus.ACTIVE,
        enrolled_at=NOW,
        created_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollment(**fields)


def make_suppression(email_normalized: str, reason: MailSuppressionReason, active: bool = True, **overrides) -> MailSuppression:
    fields = dict(email_normalized=email_normalized, reason=reason, created_at=NOW, updated_at=NOW, active=active)
    fields.update(overrides)
    return MailSuppression(**fields)


def make_step(enrollment_id: str, campaign_id: str, step_number: int = 1, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=f"{enrollment_id}-step{step_number}",
        mail_campaign_id=campaign_id,
        enrollment_id=enrollment_id,
        crm_contact_id=f"contact-{enrollment_id}",
        step_id=f"step-{step_number}",
        step_number=step_number,
        subject="Quick hello",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


@pytest_asyncio.fixture
async def stores():
    return {
        "campaign_store": MemoryMailCampaignStore(),
        "enrollment_store": MemoryMailEnrollmentStore(),
        "enrollment_step_store": MemoryMailEnrollmentStepStore(),
        "open_event_store": MemoryMailOpenEventStore(),
        "suppression_store": MemoryMailSuppressionStore(),
    }


@pytest_asyncio.fixture
async def service(stores):
    return MailCampaignStatsService(**stores)


async def test_unknown_campaign_raises(service):
    with pytest.raises(MailCampaignNotFound):
        await service.get_stats("does-not-exist")


async def test_zero_enrollments_gives_zero_rates_not_a_divide_by_zero(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    stats = await service.get_stats("c1")
    assert stats.total == 0
    assert stats.reply_rate_percent == 0.0
    assert stats.unsub_rate_percent == 0.0
    assert stats.open_rate_percent is None


# --- Open rate (2026-09-18) --------------------------------------------


async def test_open_rate_is_none_when_tracking_disabled(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", open_tracking_enabled=False))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "a@example.com"))
    await stores["enrollment_step_store"].create(make_step("e1", "c1"))
    await stores["open_event_store"].record_open(enrollment_step_id="e1-step1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)

    stats = await service.get_stats("c1")
    assert stats.open_tracking_enabled is False
    assert stats.open_rate_percent is None


async def test_open_rate_is_none_when_tracking_enabled_but_nothing_sent_yet(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", open_tracking_enabled=True))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "a@example.com"))

    stats = await service.get_stats("c1")
    assert stats.open_tracking_enabled is True
    assert stats.open_rate_percent is None


async def test_open_rate_is_unique_opens_over_unique_sent(stores, service):
    await stores["campaign_store"].create(make_campaign("c1", open_tracking_enabled=True))
    for i in range(5):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}", f"p{i}@example.com"))
        await stores["enrollment_step_store"].create(make_step(f"e{i}", "c1"))
    # 2 of 5 opened -- one of them twice, which must still count as ONE unique open.
    await stores["open_event_store"].record_open(enrollment_step_id="e0-step1", mail_campaign_id="c1", enrollment_id="e0", at=NOW)
    await stores["open_event_store"].record_open(enrollment_step_id="e0-step1", mail_campaign_id="c1", enrollment_id="e0", at=NOW)
    await stores["open_event_store"].record_open(enrollment_step_id="e1-step1", mail_campaign_id="c1", enrollment_id="e1", at=NOW)

    stats = await service.get_stats("c1")
    assert stats.open_rate_percent == 40.0


async def test_reply_rate_is_replied_over_total(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "a@example.com", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", "b@example.com", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e3", "c1", "contact3", "c@example.com", status=MailEnrollmentStatus.ACTIVE))
    await stores["enrollment_store"].create(make_enrollment("e4", "c1", "contact4", "d@example.com", status=MailEnrollmentStatus.PENDING))

    stats = await service.get_stats("c1")
    assert stats.total == 4
    assert stats.replied == 1
    assert stats.reply_rate_percent == 25.0


async def test_reply_rate_matches_pilot_shaped_state(stores, service):
    """5-person pilot shape: 3 replied out of 5 -- matches the live
    pilot's real, verified state elsewhere in this session (60%)."""
    await stores["campaign_store"].create(make_campaign("c1"))
    for i, status in enumerate([
        MailEnrollmentStatus.REPLIED,
        MailEnrollmentStatus.REPLIED,
        MailEnrollmentStatus.REPLIED,
        MailEnrollmentStatus.ACTIVE,
        MailEnrollmentStatus.ACTIVE,
    ]):
        await stores["enrollment_store"].create(make_enrollment(f"e{i}", "c1", f"contact{i}", f"p{i}@example.com", status=status))

    stats = await service.get_stats("c1")
    assert stats.reply_rate_percent == 60.0


async def test_unsub_rate_counts_only_unsubscribed_reason_not_other_suppression_reasons(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "unsub@example.com", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", "bounced@example.com", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["enrollment_store"].create(make_enrollment("e3", "c1", "contact3", "manual@example.com", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["enrollment_store"].create(make_enrollment("e4", "c1", "contact4", "ok@example.com", status=MailEnrollmentStatus.ACTIVE))

    await stores["suppression_store"].upsert(make_suppression("unsub@example.com", MailSuppressionReason.UNSUBSCRIBED))
    await stores["suppression_store"].upsert(make_suppression("bounced@example.com", MailSuppressionReason.HARD_BOUNCE))
    await stores["suppression_store"].upsert(make_suppression("manual@example.com", MailSuppressionReason.MANUAL))

    stats = await service.get_stats("c1")
    assert stats.unsubscribed == 1
    assert stats.unsub_rate_percent == 25.0


async def test_unsub_rate_ignores_an_inactive_unsuppressed_row(stores, service):
    """An email that WAS unsubscribed and later reactivated (active=False)
    must not count -- only a currently-active suppression is real."""
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "resub@example.com", status=MailEnrollmentStatus.ACTIVE))
    await stores["suppression_store"].upsert(
        make_suppression("resub@example.com", MailSuppressionReason.UNSUBSCRIBED, active=False, unsuppressed_at=NOW)
    )

    stats = await service.get_stats("c1")
    assert stats.unsubscribed == 0
    assert stats.unsub_rate_percent == 0.0


async def test_unsub_rate_matches_email_case_insensitively_via_normalization(stores, service):
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "Person@Example.COM", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["suppression_store"].upsert(make_suppression("person@example.com", MailSuppressionReason.UNSUBSCRIBED))

    stats = await service.get_stats("c1")
    assert stats.unsubscribed == 1


async def test_reply_rate_and_unsub_rate_are_independent_of_each_other(stores, service):
    """A replied enrollment and an unsubscribed enrollment are mutually
    exclusive states in practice, but this service must never conflate
    the two counts even if both happened to be true for the same row."""
    await stores["campaign_store"].create(make_campaign("c1"))
    await stores["enrollment_store"].create(make_enrollment("e1", "c1", "contact1", "a@example.com", status=MailEnrollmentStatus.REPLIED))
    await stores["enrollment_store"].create(make_enrollment("e2", "c1", "contact2", "b@example.com", status=MailEnrollmentStatus.SUPPRESSED))
    await stores["suppression_store"].upsert(make_suppression("b@example.com", MailSuppressionReason.UNSUBSCRIBED))

    stats = await service.get_stats("c1")
    assert stats.replied == 1
    assert stats.unsubscribed == 1
    assert stats.reply_rate_percent == 50.0
    assert stats.unsub_rate_percent == 50.0
