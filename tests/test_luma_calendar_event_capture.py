"""
Stage 6A Capture (2026-09-14) -- temporary, schema-discovery-only
calendar.person.subscribed/unsubscribed capture mechanism. Deliberately
its own, entirely self-contained test file -- see
app/models/luma.py's LumaCalendarEventCapture docstring and
app/services/luma_sync_service.py's handle_webhook() docstring for the
full contract this proves. Meant to be deleted in one shot alongside the
store/model/branch it tests, once a real payload has been captured and
this mechanism is removed.

Route-level tests here mount the REAL luma router AND the REAL
session_auth_middleware, mirroring tests/test_luma_api.py's own
convention -- these prove exactly what's deployed.
"""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.luma import router as luma_router
from app.dependencies import get_auth_service, get_luma_sync_service
from app.models.crm import CrmCustomFieldDefinition, CustomFieldType
from app.repositories.auth_session_store import MemoryAuthSessionStore
from app.repositories.crm_custom_field_store import MemoryCrmCustomFieldStore
from app.repositories.luma_backfill_checkpoint_store import MemoryLumaBackfillCheckpointStore
from app.repositories.luma_calendar_event_capture_store import MemoryLumaCalendarEventCaptureStore
from app.repositories.luma_event_store import MemoryLumaEventStore
from app.repositories.luma_question_mapping_store import MemoryLumaQuestionMappingStore
from app.repositories.luma_registration_store import MemoryLumaRegistrationStore
from app.services import auth_service as auth_service_module
from app.services.auth_service import AuthService
from app.services.crm_service import CrmService
from app.services.luma_sync_service import CALENDAR_EVENT_CAPTURE_TYPES, LumaSyncService
from app.services.password_hashing import hash_password
from app.session_auth_middleware import enforce_session_auth

pytestmark = pytest.mark.asyncio

REAL_PASSWORD = "correct horse battery staple"
WEBHOOK_SECRET = "whsec_test_secret"


def _now():
    return datetime(2026, 9, 14, tzinfo=timezone.utc)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    monkeypatch.setattr(auth_service_module.settings, "auth_email", "team@astronomic.com")
    monkeypatch.setattr(auth_service_module.settings, "auth_password_hash", hash_password(REAL_PASSWORD))
    monkeypatch.setattr(auth_service_module.settings, "cookie_secure", False)


@pytest.fixture(autouse=True)
def configured_luma_webhook_secret(monkeypatch):
    from app.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, "luma_webhook_secret", WEBHOOK_SECRET)


@pytest.fixture
def capture_flag_enabled(monkeypatch):
    """Deliberately NOT autouse -- most tests here must prove the flag's
    OFF-by-default behavior. Patches the setting where luma_sync_service.py
    actually reads it, same convention as luma_contact_enrichment_enabled/
    luma_contact_location_enrichment_enabled's own test fixtures."""
    from app.services import luma_sync_service as luma_sync_service_module

    monkeypatch.setattr(luma_sync_service_module.settings, "luma_calendar_event_capture_enabled", True)


@pytest.fixture
def auth_svc():
    return AuthService(session_store=MemoryAuthSessionStore())


@pytest.fixture
def capture_store():
    return MemoryLumaCalendarEventCaptureStore()


@pytest_asyncio.fixture
async def luma_service(capture_store):
    custom_field_store = MemoryCrmCustomFieldStore()
    await custom_field_store.create(
        CrmCustomFieldDefinition(
            crm_custom_field_id=str(uuid.uuid4()),
            field_key="investor_type",
            label="Investor Type",
            field_type=CustomFieldType.MULTI_SELECT,
            options=["Angel Investor"],
            active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    crm_service = CrmService(custom_field_store=custom_field_store)
    return LumaSyncService(
        crm_service=crm_service,
        event_store=MemoryLumaEventStore(),
        registration_store=MemoryLumaRegistrationStore(),
        mapping_store=MemoryLumaQuestionMappingStore(),
        activity_log=crm_service.activity_log,
        checkpoint_store=MemoryLumaBackfillCheckpointStore(),
        luma_client=None,
        calendar_event_capture_store=capture_store,
    )


@pytest.fixture
def client(luma_service, auth_svc):
    app = FastAPI()
    app.middleware("http")(enforce_session_auth)
    app.state.auth_service = auth_svc
    app.include_router(auth_router)
    app.include_router(luma_router)
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    app.dependency_overrides[get_luma_sync_service] = lambda: luma_service
    with TestClient(app) as c:
        yield c


def _calendar_event_body(event_type: str, delivery_ref: str = "usr-1") -> bytes:
    return json.dumps({"type": event_type, "data": {"id": delivery_ref, "email": "follower@example.com"}}).encode("utf-8")


def _post(client, event_type: str, webhook_id: str, delivery_ref: str = "usr-1"):
    body = _calendar_event_body(event_type, delivery_ref)
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    return client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": webhook_id, "Content-Type": "application/json"},
    )


def _post_calendar_route(client, event_type: str, webhook_id: str, delivery_ref: str = "usr-1", secret: str = WEBHOOK_SECRET):
    """Same shape as _post() above, but targets the NEW, dedicated
    /sync/luma-calendar-event route -- this event type's real, live entry
    point. `secret` defaults to the primary WEBHOOK_SECRET but can be
    overridden to prove the additional-secret fallback authenticates this
    route too."""
    body = _calendar_event_body(event_type, delivery_ref)
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(secret, timestamp, body)
    return client.post(
        "/sync/luma-calendar-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": webhook_id, "Content-Type": "application/json"},
    )


# --- feature flag default -----------------------------------------------------


def test_settings_luma_calendar_event_capture_enabled_defaults_false():
    from app.config import Settings

    assert Settings.model_fields["luma_calendar_event_capture_enabled"].default is False


def test_exact_two_event_types_are_the_only_ones_recognized():
    assert CALENDAR_EVENT_CAPTURE_TYPES == frozenset({"calendar.person.subscribed", "calendar.person.unsubscribed"})


# --- flag off: no stored row, exactly today's behavior -----------------------


async def test_calendar_event_with_flag_off_stores_nothing(client, capture_store):
    """No capture_flag_enabled fixture requested -- real production default."""
    resp = _post(client, "calendar.person.subscribed", "wh-1")
    assert resp.status_code == 200  # ignored, exactly like today -- never a 4xx
    assert await capture_store.list() == []


async def test_unsubscribed_with_flag_off_stores_nothing(client, capture_store):
    resp = _post(client, "calendar.person.unsubscribed", "wh-1")
    assert resp.status_code == 200
    assert await capture_store.list() == []


# --- flag on: capture fires, bounded to one row per event type --------------


async def test_subscribed_with_flag_on_stores_exactly_one_row(client, capture_store, capture_flag_enabled):
    resp = _post(client, "calendar.person.subscribed", "wh-1")
    assert resp.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].event_type == "calendar.person.subscribed"
    assert rows[0].delivery_id == "wh-1"
    assert rows[0].raw_payload == {"id": "usr-1", "email": "follower@example.com"}


async def test_unsubscribed_with_flag_on_stores_exactly_one_row(client, capture_store, capture_flag_enabled):
    resp = _post(client, "calendar.person.unsubscribed", "wh-1")
    assert resp.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].event_type == "calendar.person.unsubscribed"


async def test_repeated_same_event_type_still_stores_only_one_row(client, capture_store, capture_flag_enabled):
    _post(client, "calendar.person.subscribed", "wh-1", delivery_ref="usr-1")
    resp2 = _post(client, "calendar.person.subscribed", "wh-2", delivery_ref="usr-2")  # different delivery, different person

    assert resp2.status_code == 200  # still a clean 200 -- a no-op, never an error
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].delivery_id == "wh-1"  # the FIRST one -- never overwritten by a later delivery


async def test_duplicate_delivery_id_stores_no_duplicate(client, capture_store, capture_flag_enabled):
    _post(client, "calendar.person.subscribed", "wh-dup")
    resp2 = _post(client, "calendar.person.subscribed", "wh-dup")  # exact same Webhook-Id, retried

    assert resp2.status_code == 200
    assert len(await capture_store.list()) == 1


async def test_both_event_types_together_store_at_most_two_rows_total(client, capture_store, capture_flag_enabled):
    _post(client, "calendar.person.subscribed", "wh-1")
    _post(client, "calendar.person.subscribed", "wh-2")  # bounded -- still just 1 for this type
    _post(client, "calendar.person.unsubscribed", "wh-3")
    _post(client, "calendar.person.unsubscribed", "wh-4")  # bounded -- still just 1 for this type

    rows = await capture_store.list()
    assert len(rows) == 2
    assert {r.event_type for r in rows} == {"calendar.person.subscribed", "calendar.person.unsubscribed"}


# --- HMAC verification remains mandatory, unconditionally --------------------


async def test_invalid_signature_never_reaches_capture_even_with_flag_on(client, capture_store, capture_flag_enabled):
    body = _calendar_event_body("calendar.person.subscribed")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    bad_signature = f"t={timestamp},v1=" + "0" * 64
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": bad_signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert await capture_store.list() == []


async def test_missing_signature_never_reaches_capture_even_with_flag_on(client, capture_store, capture_flag_enabled):
    body = _calendar_event_body("calendar.person.subscribed")
    resp = client.post(
        "/sync/luma-event", content=body, headers={"Webhook-Id": "wh-1", "Content-Type": "application/json"}
    )
    assert resp.status_code == 401
    assert await capture_store.list() == []


# --- existing guest.*/ticket.* behavior is completely unaffected ------------


async def test_guest_registered_still_creates_a_contact_exactly_as_before_flag_on(client, luma_service, capture_flag_enabled):
    from tests.test_luma_sync_service import make_event, make_guest

    body = json.dumps({"type": "guest.registered", "data": {**make_guest(email="alice@example.com"), "event": make_event()}}).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    contacts = await luma_service.crm_service.contact_store.list()
    assert len(contacts) == 1
    assert contacts[0].email == "alice@example.com"


async def test_guest_registered_still_creates_a_contact_with_flag_off(client, luma_service):
    from tests.test_luma_sync_service import make_event, make_guest

    body = json.dumps({"type": "guest.registered", "data": {**make_guest(email="bob@example.com"), "event": make_event()}}).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-2", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    contacts = await luma_service.crm_service.contact_store.list()
    assert len(contacts) == 1
    assert contacts[0].email == "bob@example.com"


# --- no CRM/Contact/Activity Log mutation from a calendar event -------------


async def test_calendar_event_never_creates_a_contact(client, luma_service, capture_flag_enabled):
    _post(client, "calendar.person.subscribed", "wh-1")
    assert await luma_service.crm_service.contact_store.list() == []


async def test_calendar_event_never_writes_an_activity_log_entry(client, luma_service, capture_flag_enabled):
    _post(client, "calendar.person.subscribed", "wh-1")
    _post(client, "calendar.person.unsubscribed", "wh-2")
    page = await luma_service.activity_log.list_events()
    assert page.items == []


# --- structural guard: no MailSuppression reference anywhere in this path --


def test_capture_branch_source_never_references_mail_suppression():
    """MailSuppression is a separate, unrelated system -- this asserts,
    directly against the source, that this whole service module never
    imports or actually USES it (the identifiers real usage would require:
    an import, a store attribute, or a class reference) -- distinct from
    merely mentioning the word in a docstring/comment describing what must
    NOT happen, which this deliberately does not flag."""
    import pathlib

    source = pathlib.Path("app/services/luma_sync_service.py").read_text()
    assert "MailSuppressionStore" not in source
    assert "mail_suppression_store" not in source
    assert "import" not in "\n".join(line for line in source.splitlines() if "mail_suppression" in line.lower())


# --- SQLite store itself: atomic, at-most-one-per-event-type bound ---------
# (not just the Memory test double's own, trivially-satisfied dict semantics)


@pytest_asyncio.fixture
async def sqlite_capture_store(tmp_path):
    from app.repositories.sqlite_luma_calendar_event_capture_store import SQLiteLumaCalendarEventCaptureStore

    store = SQLiteLumaCalendarEventCaptureStore(str(tmp_path / "capture_test.db"))
    await store.connect()
    yield store
    await store.close()


async def test_sqlite_store_first_save_for_a_type_succeeds(sqlite_capture_store):
    from app.models.luma import LumaCalendarEventCapture

    capture = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-1", captured_at=_now(), raw_payload={"id": "usr-1"}
    )
    stored = await sqlite_capture_store.save_if_first_for_event_type(capture)
    assert stored is True
    fetched = await sqlite_capture_store.get("calendar.person.subscribed")
    assert fetched.delivery_id == "wh-1"
    assert fetched.raw_payload == {"id": "usr-1"}


async def test_sqlite_store_second_save_for_the_same_type_is_a_no_op(sqlite_capture_store):
    from app.models.luma import LumaCalendarEventCapture

    first = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-1", captured_at=_now(), raw_payload={"id": "usr-1"}
    )
    second = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-2", captured_at=_now(), raw_payload={"id": "usr-2"}
    )
    assert await sqlite_capture_store.save_if_first_for_event_type(first) is True
    assert await sqlite_capture_store.save_if_first_for_event_type(second) is False  # atomic no-op, never an error

    rows = await sqlite_capture_store.list()
    assert len(rows) == 1
    assert rows[0].delivery_id == "wh-1"  # the first one, never overwritten


async def test_sqlite_store_concurrent_saves_for_the_same_type_never_both_succeed(sqlite_capture_store):
    """The atomicity requirement itself: fire two saves for the SAME
    event_type concurrently (asyncio.gather, not sequential awaits) --
    SQLite's own PRIMARY KEY constraint on event_type, combined with
    INSERT OR IGNORE, guarantees exactly one wins regardless of
    interleaving."""
    import asyncio

    from app.models.luma import LumaCalendarEventCapture

    a = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-a", captured_at=_now(), raw_payload={"id": "a"}
    )
    b = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-b", captured_at=_now(), raw_payload={"id": "b"}
    )
    results = await asyncio.gather(
        sqlite_capture_store.save_if_first_for_event_type(a),
        sqlite_capture_store.save_if_first_for_event_type(b),
    )
    assert sorted(results) == [False, True]  # exactly one succeeded
    assert len(await sqlite_capture_store.list()) == 1


async def test_sqlite_store_two_different_event_types_each_get_their_own_row(sqlite_capture_store):
    from app.models.luma import LumaCalendarEventCapture

    sub = LumaCalendarEventCapture(
        event_type="calendar.person.subscribed", delivery_id="wh-1", captured_at=_now(), raw_payload={"id": "usr-1"}
    )
    unsub = LumaCalendarEventCapture(
        event_type="calendar.person.unsubscribed", delivery_id="wh-2", captured_at=_now(), raw_payload={"id": "usr-2"}
    )
    assert await sqlite_capture_store.save_if_first_for_event_type(sub) is True
    assert await sqlite_capture_store.save_if_first_for_event_type(unsub) is True
    assert len(await sqlite_capture_store.list()) == 2


# --- Dedicated /sync/luma-calendar-event route (2026-09-14) -----------------
# Required because Luma rejects two webhooks with the same URL -- see
# LumaSyncService.handle_calendar_webhook()'s own docstring.


async def test_calendar_route_with_flag_off_returns_200_and_stores_nothing(client, capture_store):
    resp = _post_calendar_route(client, "calendar.person.subscribed", "wh-1")
    assert resp.status_code == 200
    assert await capture_store.list() == []


async def test_calendar_route_subscribed_with_flag_on_stores_exactly_one_row(client, capture_store, capture_flag_enabled):
    resp = _post_calendar_route(client, "calendar.person.subscribed", "wh-1")
    assert resp.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].event_type == "calendar.person.subscribed"
    assert rows[0].delivery_id == "wh-1"


async def test_calendar_route_unsubscribed_with_flag_on_stores_exactly_one_row(client, capture_store, capture_flag_enabled):
    resp = _post_calendar_route(client, "calendar.person.unsubscribed", "wh-1")
    assert resp.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].event_type == "calendar.person.unsubscribed"


async def test_calendar_route_repeated_delivery_remains_bounded_and_idempotent(client, capture_store, capture_flag_enabled):
    _post_calendar_route(client, "calendar.person.subscribed", "wh-1", delivery_ref="usr-1")
    resp2 = _post_calendar_route(client, "calendar.person.subscribed", "wh-2", delivery_ref="usr-2")  # different delivery
    resp3 = _post_calendar_route(client, "calendar.person.subscribed", "wh-1")  # exact same delivery id retried

    assert resp2.status_code == 200
    assert resp3.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1
    assert rows[0].delivery_id == "wh-1"  # the first one, never overwritten or duplicated


async def test_calendar_route_wrong_signature_is_rejected_before_capture(client, capture_store, capture_flag_enabled):
    body = _calendar_event_body("calendar.person.subscribed")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    bad_signature = f"t={timestamp},v1=" + "0" * 64
    resp = client.post(
        "/sync/luma-calendar-event",
        content=body,
        headers={"Webhook-Signature": bad_signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert await capture_store.list() == []


async def test_guest_registered_sent_to_calendar_route_never_enters_guest_processing(client, luma_service, capture_flag_enabled):
    from tests.test_luma_sync_service import make_event, make_guest

    body = json.dumps({"type": "guest.registered", "data": {**make_guest(email="carol@example.com"), "event": make_event()}}).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = _sign(WEBHOOK_SECRET, timestamp, body)
    resp = client.post(
        "/sync/luma-calendar-event",
        content=body,
        headers={"Webhook-Signature": signature, "Webhook-Id": "wh-1", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200  # a structurally valid, signed delivery -- ignored, not an error
    assert await luma_service.crm_service.contact_store.list() == []  # no Contact created
    assert await luma_service.registration_store.list() == []  # no registration created


async def test_calendar_route_is_reachable_with_no_hub_session(client):
    """Same PUBLIC_PATHS precedent as /sync/luma-event -- Luma's delivery
    carries no Hub session cookie."""
    resp = _post_calendar_route(client, "calendar.person.subscribed", "wh-1")
    assert resp.status_code == 200


async def test_additional_secret_authenticates_the_calendar_route(client, capture_store, capture_flag_enabled, monkeypatch):
    from app.dependencies import settings as deps_settings

    additional_secret = "whsec_second_webhook_secret"
    monkeypatch.setattr(deps_settings, "luma_additional_webhook_secrets", additional_secret)
    resp = _post_calendar_route(client, "calendar.person.subscribed", "wh-1", secret=additional_secret)
    assert resp.status_code == 200
    rows = await capture_store.list()
    assert len(rows) == 1


async def test_primary_secret_still_authenticates_calendar_route_when_additional_is_also_configured(
    client, capture_store, capture_flag_enabled, monkeypatch
):
    from app.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, "luma_additional_webhook_secrets", "whsec_second_webhook_secret")
    resp = _post_calendar_route(client, "calendar.person.subscribed", "wh-1")  # signed with PRIMARY (default) secret
    assert resp.status_code == 200
    assert len(await capture_store.list()) == 1


# --- no CRM/Contact/Activity/MailSuppression/EngagementParticipant mutation -
# from the new route -----------------------------------------------------


async def test_calendar_route_never_creates_a_contact(client, luma_service, capture_flag_enabled):
    _post_calendar_route(client, "calendar.person.subscribed", "wh-1")
    assert await luma_service.crm_service.contact_store.list() == []


async def test_calendar_route_never_writes_an_activity_log_entry(client, luma_service, capture_flag_enabled):
    _post_calendar_route(client, "calendar.person.subscribed", "wh-1")
    _post_calendar_route(client, "calendar.person.unsubscribed", "wh-2")
    page = await luma_service.activity_log.list_events()
    assert page.items == []


def test_calendar_webhook_methods_never_use_mail_suppression_engagement_participant_or_activity_log():
    """Structural guard scoped to the TWO new methods specifically (not
    the whole file -- the existing guest pipeline elsewhere in this same
    module legitimately uses LumaEngagementParticipantSyncService/
    activity_log, so a whole-file or bare-word check would be
    meaningless/false-positive on their own docstrings, which describe
    what these methods must NOT do). Checks the METHOD BODY (docstring
    stripped) for actual code-usage patterns -- an attribute access or
    call -- never a bare English word."""
    import ast
    import inspect
    import textwrap

    from app.services.luma_sync_service import LumaSyncService

    forbidden_patterns = (
        "mail_suppression",
        "MailSuppressionStore",
        "EngagementParticipantStore",
        "participant_sync_service",
        "activity_log.record",
    )
    for method_name in ("handle_calendar_webhook", "_maybe_capture_calendar_event"):
        method = getattr(LumaSyncService, method_name)
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        func_node = tree.body[0]
        # Strip the docstring (the first statement, if it's a bare string
        # expression) before checking -- this test cares about actual
        # code, never the prose describing what the code must NOT do.
        body_without_docstring = func_node.body[1:] if ast.get_docstring(func_node) else func_node.body
        body_source = "\n".join(ast.unparse(stmt) for stmt in body_without_docstring)
        for pattern in forbidden_patterns:
            assert pattern not in body_source, f"{method_name} unexpectedly references {pattern!r}"
