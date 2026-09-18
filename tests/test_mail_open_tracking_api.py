"""
Route-level tests for GET /mail/track/open -- the public, unauthenticated
pixel surface (2026-09-18). Bare-FastAPI-plus-just-this-router
convention, matching tests/test_mail_unsubscribe_api.py.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mail_open_tracking import router as open_tracking_router
from app.dependencies import get_mail_open_tracking_service
from app.models.mail import MailEnrollmentStep, MailEnrollmentStepStatus
from app.repositories.mail_enrollment_step_store import MemoryMailEnrollmentStepStore
from app.repositories.mail_open_event_store import MemoryMailOpenEventStore
from app.services.mail_open_tracking_service import MailOpenTrackingService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def make_step(enrollment_step_id: str, open_tracking_token: str | None) -> MailEnrollmentStep:
    return MailEnrollmentStep(
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


@pytest.fixture
def enrollment_step_store():
    return MemoryMailEnrollmentStepStore()


@pytest.fixture
def open_event_store():
    return MemoryMailOpenEventStore()


@pytest.fixture
def service(enrollment_step_store, open_event_store):
    return MailOpenTrackingService(enrollment_step_store, open_event_store)


@pytest.fixture
def client(service):
    app = FastAPI()
    app.include_router(open_tracking_router)
    app.dependency_overrides[get_mail_open_tracking_service] = lambda: service
    with TestClient(app) as c:
        yield c


async def test_valid_token_returns_a_real_gif_and_records_an_open(client, enrollment_step_store, open_event_store):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    resp = client.get("/mail/track/open", params={"token": "tok-1"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/gif"
    assert resp.content[:6] == b"GIF89a"
    event = await open_event_store.get("s1")
    assert event is not None
    assert event.open_count == 1


async def test_unknown_token_still_returns_the_exact_same_gif_bytes(client, enrollment_step_store):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    known = client.get("/mail/track/open", params={"token": "tok-1"})
    unknown = client.get("/mail/track/open", params={"token": "does-not-exist"})

    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content
    assert known.headers["content-type"] == unknown.headers["content-type"]


async def test_missing_token_is_a_safe_no_op_still_returning_the_gif(client):
    resp = client.get("/mail/track/open")
    assert resp.status_code == 200
    assert resp.content[:6] == b"GIF89a"


def test_response_is_never_cached(client):
    resp = client.get("/mail/track/open", params={"token": "tok-1"})
    assert "no-store" in resp.headers["cache-control"]


async def test_repeated_loads_of_the_same_token_bump_count_never_a_second_unique_open(
    client, enrollment_step_store, open_event_store
):
    await enrollment_step_store.create(make_step("s1", "tok-1"))

    client.get("/mail/track/open", params={"token": "tok-1"})
    client.get("/mail/track/open", params={"token": "tok-1"})
    client.get("/mail/track/open", params={"token": "tok-1"})

    events = await open_event_store.list_for_campaign("c1")
    assert len(events) == 1
    assert events[0].open_count == 3


async def test_a_wrong_token_never_leaks_whether_any_real_token_exists(client, enrollment_step_store):
    """Same 'never an oracle' posture as the unsubscribe routes -- status
    code and body are identical whether or not ANY step in the store has
    ever issued a token."""
    resp_with_no_steps_at_all = client.get("/mail/track/open", params={"token": "anything"})
    await enrollment_step_store.create(make_step("s1", "tok-1"))
    resp_with_a_real_step_but_wrong_token = client.get("/mail/track/open", params={"token": "anything"})

    assert resp_with_no_steps_at_all.status_code == resp_with_a_real_step_but_wrong_token.status_code == 200
    assert resp_with_no_steps_at_all.content == resp_with_a_real_step_but_wrong_token.content
