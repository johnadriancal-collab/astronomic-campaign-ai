"""
MailOpenTrackingService + MailEnrollmentStepStore.get_by_open_tracking_token()
(2026-09-18). Same tests run against both the Memory and SQLite
MailEnrollmentStepStore implementations via a fixture parametrized by
store class.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_open_event_store import MemoryMailOpenEventStore
from app.repositories.sqlite_mail_enrollment_step_store import SQLiteMailEnrollmentStepStore
from app.services.mail_open_tracking_service import MailOpenTrackingService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def make_step(enrollment_step_id: str, open_tracking_token: str | None, **overrides) -> MailEnrollmentStep:
    fields = dict(
        enrollment_step_id=enrollment_step_id,
        mail_campaign_id="c1",
        enrollment_id="e1",
        crm_contact_id="contact1",
        step_id="step-1",
        step_number=1,
        subject="Quick hello",
        body="Hi {{first_name}},",
        delay_days=0,
        reply_in_thread=True,
        status=MailEnrollmentStepStatus.SENT,
        sent_at=NOW,
        open_tracking_token=open_tracking_token,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(overrides)
    return MailEnrollmentStep(**fields)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def enrollment_step_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryMailEnrollmentStepStore()
    else:
        s = SQLiteMailEnrollmentStepStore(str(tmp_path / "test.db"))
        await s.connect()
        yield s
        await s.close()


# --- MailEnrollmentStepStore.get_by_open_tracking_token() -------------------


async def test_get_by_open_tracking_token_finds_the_right_step(enrollment_step_store):
    await enrollment_step_store.create(make_step("s1", "tok-1"))
    await enrollment_step_store.create(make_step("s2", "tok-2", enrollment_id="e2", step_id="step-2"))

    found = await enrollment_step_store.get_by_open_tracking_token("tok-2")
    assert found.enrollment_step_id == "s2"


async def test_get_by_open_tracking_token_returns_none_for_unknown_token(enrollment_step_store):
    await enrollment_step_store.create(make_step("s1", "tok-1"))
    assert await enrollment_step_store.get_by_open_tracking_token("does-not-exist") is None


async def test_get_by_open_tracking_token_returns_none_when_token_is_null(enrollment_step_store):
    """Every pre-feature/tracking-disabled row has open_tracking_token
    None -- a lookup by None must never accidentally match all of them."""
    await enrollment_step_store.create(make_step("s1", None))
    await enrollment_step_store.create(make_step("s2", None, enrollment_id="e2", step_id="step-2"))
    assert await enrollment_step_store.get_by_open_tracking_token("tok-1") is None


async def test_persist_prepared_fields_writes_the_open_tracking_token_findably(enrollment_step_store):
    """Mirrors the real send-path sequence: a step is created without a
    token (PENDING/QUEUED/CLAIMED), then persist_prepared_fields() sets
    it -- same call persist_prepared_fields() already uses for
    rfc_message_id/mailbox_id."""
    step = make_step("s1", None, status=MailEnrollmentStepStatus.CLAIMED)
    await enrollment_step_store.create(step)

    updated = step.model_copy(update={"open_tracking_token": "tok-fresh"})
    applied = await enrollment_step_store.persist_prepared_fields("s1", updated)

    assert applied is True
    found = await enrollment_step_store.get_by_open_tracking_token("tok-fresh")
    assert found.enrollment_step_id == "s1"


# --- MailOpenTrackingService.record_open() ----------------------------------


@pytest_asyncio.fixture
async def open_event_store():
    return MemoryMailOpenEventStore()


@pytest_asyncio.fixture
async def service(enrollment_step_store, open_event_store):
    return MailOpenTrackingService(enrollment_step_store, open_event_store)


async def test_record_open_creates_an_event_for_a_valid_token(enrollment_step_store, open_event_store, service):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    await service.record_open("tok-1", NOW)

    event = await open_event_store.get("s1")
    assert event is not None
    assert event.mail_campaign_id == "c1"
    assert event.enrollment_id == "e1"
    assert event.open_count == 1


async def test_record_open_is_silently_a_no_op_for_an_unknown_token(enrollment_step_store, open_event_store, service):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    await service.record_open("wrong-token", NOW)

    assert await open_event_store.get("s1") is None
    assert await open_event_store.list_for_campaign("c1") == []


async def test_record_open_never_mutates_the_enrollment_step_itself(enrollment_step_store, open_event_store, service):
    """Opening an email is not a step-execution-state change -- the step
    row (status, sent_at, etc.) must be byte-identical before/after."""
    step = make_step("s1", "tok-1")
    await enrollment_step_store.create(step)

    await service.record_open("tok-1", NOW)

    unchanged = await enrollment_step_store.get("s1")
    assert unchanged.status == step.status
    assert unchanged.sent_at == step.sent_at


async def test_repeated_opens_of_the_same_token_increment_count_not_unique_open(
    enrollment_step_store, open_event_store, service
):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    await service.record_open("tok-1", NOW)
    await service.record_open("tok-1", NOW)
    await service.record_open("tok-1", NOW)

    events = await open_event_store.list_for_campaign("c1")
    assert len(events) == 1
    assert events[0].open_count == 3
