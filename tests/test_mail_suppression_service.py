"""
Tests for MailSuppressionService -- keyed by normalized email, independent
of CrmContact.email_status. Uses MemoryMailSuppressionStore throughout,
same convention as every other in-memory-store test in this suite.
"""

import pytest

from app.models.mail import MailSuppressionReason
from app.repositories.mail_suppression_store import MemoryMailSuppressionStore
from app.services.activity_log_service import ActivityLogService
from app.services.mail_suppression_service import (
    InvalidMailSuppressionEmailError,
    MailSuppressionNotFoundError,
    MailSuppressionService,
)


@pytest.fixture
def store():
    return MemoryMailSuppressionStore()


@pytest.fixture
def activity_log():
    from app.repositories.activity_event_store import MemoryActivityEventStore

    return ActivityLogService(MemoryActivityEventStore())


@pytest.fixture
def service(store, activity_log):
    return MailSuppressionService(store=store, activity_log=activity_log)


@pytest.mark.asyncio
async def test_suppress_creates_an_active_row(service):
    row = await service.suppress("Amos@Example.com", MailSuppressionReason.MANUAL)
    assert row.email_normalized == "amos@example.com"  # normalized -- lowercased, stripped
    assert row.active is True
    assert row.reason == MailSuppressionReason.MANUAL


@pytest.mark.asyncio
async def test_suppress_is_idempotent_for_an_already_active_email(service, activity_log):
    first = await service.suppress("amos@example.com", MailSuppressionReason.UNSUBSCRIBED)
    second = await service.suppress("amos@example.com", MailSuppressionReason.UNSUBSCRIBED)
    assert first.email_normalized == second.email_normalized
    assert first.created_at == second.created_at  # same row, not a new one

    events = await activity_log.store.list()
    suppressed_events = [e for e in events if e.event_type == "mail.contact_suppressed"]
    assert len(suppressed_events) == 1  # only the first call actually changed anything


@pytest.mark.asyncio
async def test_suppress_never_creates_a_duplicate_row_for_the_same_normalized_email(service):
    await service.suppress("Ada@Example.com")
    await service.suppress("  ada@example.com  ".strip())
    await service.suppress("ADA@EXAMPLE.COM")

    all_rows = await service.list_all()
    assert len(all_rows) == 1


@pytest.mark.asyncio
async def test_suppress_rejects_blank_or_unusable_email(service):
    with pytest.raises(InvalidMailSuppressionEmailError):
        await service.suppress("")
    with pytest.raises(InvalidMailSuppressionEmailError):
        await service.suppress("   ")


@pytest.mark.asyncio
async def test_unsuppress_deactivates_but_preserves_the_row(service):
    await service.suppress("amos@example.com", MailSuppressionReason.HARD_BOUNCE, notes="bounced 3x")
    updated = await service.unsuppress("amos@example.com")

    assert updated.active is False
    assert updated.unsuppressed_at is not None
    assert updated.reason == MailSuppressionReason.HARD_BOUNCE  # original reason preserved, not erased
    assert updated.notes == "bounced 3x"

    all_rows = await service.list_all()
    assert len(all_rows) == 1  # never deleted


@pytest.mark.asyncio
async def test_unsuppress_on_never_suppressed_email_raises_not_found(service):
    with pytest.raises(MailSuppressionNotFoundError):
        await service.unsuppress("never-suppressed@example.com")


@pytest.mark.asyncio
async def test_unsuppress_twice_is_a_safe_noop(service):
    await service.suppress("amos@example.com")
    first = await service.unsuppress("amos@example.com")
    second = await service.unsuppress("amos@example.com")
    assert first.active is False
    assert second.active is False


@pytest.mark.asyncio
async def test_resuppressing_an_inactive_email_reactivates_the_same_row(service):
    await service.suppress("amos@example.com", MailSuppressionReason.MANUAL)
    await service.unsuppress("amos@example.com")
    reactivated = await service.suppress("amos@example.com", MailSuppressionReason.COMPLAINT, notes="second time")

    assert reactivated.active is True
    assert reactivated.reason == MailSuppressionReason.COMPLAINT
    assert reactivated.unsuppressed_at is None

    all_rows = await service.list_all()
    assert len(all_rows) == 1  # still the one row


@pytest.mark.asyncio
async def test_is_suppressed_reflects_current_active_state(service):
    assert await service.is_suppressed("amos@example.com") is False
    await service.suppress("amos@example.com")
    assert await service.is_suppressed("amos@example.com") is True
    await service.unsuppress("amos@example.com")
    assert await service.is_suppressed("amos@example.com") is False


@pytest.mark.asyncio
async def test_is_suppressed_handles_blank_email_without_raising(service):
    assert await service.is_suppressed(None) is False
    assert await service.is_suppressed("") is False


@pytest.mark.asyncio
async def test_get_status_for_a_never_suppressed_email(service):
    status = await service.get_status("nobody@example.com")
    assert status.suppressed is False
    assert status.reason is None
    assert status.created_at is None


@pytest.mark.asyncio
async def test_get_status_for_a_suppressed_email(service):
    await service.suppress("amos@example.com", MailSuppressionReason.UNSUBSCRIBED, notes="clicked link")
    status = await service.get_status("AMOS@example.com")
    assert status.email_normalized == "amos@example.com"
    assert status.suppressed is True
    assert status.reason == MailSuppressionReason.UNSUBSCRIBED
    assert status.notes == "clicked link"


@pytest.mark.asyncio
async def test_list_active_suppressed_emails_excludes_unsuppressed(service):
    await service.suppress("a@example.com")
    await service.suppress("b@example.com")
    await service.unsuppress("b@example.com")

    active = await service.list_active_suppressed_emails()
    assert active == {"a@example.com"}


@pytest.mark.asyncio
async def test_unrelated_crm_email_status_never_bypasses_suppression(service):
    """CrmContact.email_status is a completely separate, free-text field
    (see app/models/crm.py) -- MailSuppressionService never reads it, and
    setting it to anything (including 'Valid') has zero effect on
    suppression state. This test asserts the negative directly: suppress an
    email, then confirm is_suppressed still reports True regardless of
    whatever a CRM contact's email_status might independently claim."""
    from app.services.crm_service import CrmService

    crm = CrmService()
    contact = await crm.create_contact({"email": "amos@example.com", "email_status": "Valid"})

    await service.suppress("amos@example.com", MailSuppressionReason.HARD_BOUNCE)

    assert await service.is_suppressed(contact.email) is True  # suppression wins regardless of email_status
    refreshed = await crm.contact_store.get(contact.crm_contact_id)
    assert refreshed.email_status == "Valid"  # untouched -- this service never writes to CrmContact at all
