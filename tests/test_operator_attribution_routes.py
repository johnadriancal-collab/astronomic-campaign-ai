"""
Proves the ACTUAL route handlers in app/api/mail.py and app/api/crm.py
correctly compute `actor` from `request.state.identity` and pass it
through to the service call -- the wiring between
app/session_auth_middleware.py's `request.state.identity` and the
`actor` argument added to the relevant MailCampaignService/CrmService
methods (see tests/test_mail_campaign_service.py and
tests/test_crm_service.py for the service-layer half of this).

Calls the route functions directly (they're plain async functions; a
FastAPI `Depends(...)` default is simply not present when called this
way, so a stub is passed positionally/by keyword instead) against
lightweight stub services that record what `actor` they were called
with -- no ASGI stack, no real store, matching this file's narrow scope.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.api.crm import (
    CrmListBulkContactIdsRequest,
    CrmListCreateRequest,
    bulk_add_to_list,
    bulk_remove_from_list,
    create_contact_list,
    update_contact_list,
)
from app.api.mail import (
    MailCampaignCreateRequest,
    MailCampaignScheduleUpdateRequest,
    create_campaign,
    mark_campaign_ready,
    set_campaign_schedule,
    unlock_campaign,
    update_campaign,
)

pytestmark = pytest.mark.asyncio


class _FakeState:
    def __init__(self, identity: str | None):
        if identity is not None:
            self.identity = identity


class _FakeRequest:
    def __init__(self, identity: str | None):
        self.state = _FakeState(identity)


@dataclass
class _RecordedCall:
    args: tuple
    kwargs: dict


class _StubMailCampaignService:
    def __init__(self):
        self.calls: dict[str, _RecordedCall] = {}

    async def create_campaign(self, *args, **kwargs):
        self.calls["create_campaign"] = _RecordedCall(args, kwargs)
        return _FakeCampaign()

    async def update_campaign(self, *args, **kwargs):
        self.calls["update_campaign"] = _RecordedCall(args, kwargs)
        return _FakeCampaign()

    async def mark_ready(self, *args, **kwargs):
        self.calls["mark_ready"] = _RecordedCall(args, kwargs)
        return _FakeCampaign()

    async def unlock_campaign(self, *args, **kwargs):
        self.calls["unlock_campaign"] = _RecordedCall(args, kwargs)
        return _FakeCampaign()

    async def set_schedule(self, *args, **kwargs):
        self.calls["set_schedule"] = _RecordedCall(args, kwargs)
        return _FakeSchedule()


class _StubSuppressionService:
    async def list_active_suppressed_emails(self):
        return set()


@dataclass
class _FakeCampaign:
    mail_campaign_id: str = "c1"


@dataclass
class _FakeSchedule:
    mail_campaign_id: str = "c1"
    timezone: str = "UTC"
    windows: list = field(default_factory=list)


class _StubCrmService:
    def __init__(self):
        self.calls: dict[str, _RecordedCall] = {}

    async def create_contact_list(self, *args, **kwargs):
        self.calls["create_contact_list"] = _RecordedCall(args, kwargs)
        return _FakeListSummary()

    async def update_contact_list(self, *args, **kwargs):
        self.calls["update_contact_list"] = _RecordedCall(args, kwargs)
        return _FakeListSummary()

    async def bulk_add_to_list(self, *args, **kwargs):
        self.calls["bulk_add_to_list"] = _RecordedCall(args, kwargs)
        return _FakeBulkAddResult()

    async def bulk_remove_from_list(self, *args, **kwargs):
        self.calls["bulk_remove_from_list"] = _RecordedCall(args, kwargs)
        return _FakeBulkRemoveResult()


@dataclass
class _FakeListSummary:
    list_id: str = "list-1"
    name: str = "List"
    description: str | None = None
    created_at: Any = None
    updated_at: Any = None
    contact_count: int = 0


@dataclass
class _FakeBulkAddResult:
    added: int = 1
    already_member: int = 0
    not_found: int = 0


@dataclass
class _FakeBulkRemoveResult:
    removed: int = 1


# --- app/api/mail.py ---------------------------------------------------


async def test_create_campaign_route_passes_claude_operator_actor_when_identity_is_service_operator():
    service = _StubMailCampaignService()
    payload = MailCampaignCreateRequest(name="Test")

    await create_campaign(payload, _FakeRequest("service_operator"), service=service)

    assert service.calls["create_campaign"].kwargs.get("actor") == "claude_operator"


async def test_create_campaign_route_passes_none_actor_for_an_ordinary_session():
    """No `request.state.identity` at all -- the shape of a real cookie-
    authenticated request (see app/session_auth_middleware.py's
    enforce_session_auth, which never sets `.state.identity` on the
    cookie path)."""
    service = _StubMailCampaignService()
    payload = MailCampaignCreateRequest(name="Test")

    await create_campaign(payload, _FakeRequest(None), service=service)

    assert service.calls["create_campaign"].kwargs.get("actor") is None


async def test_create_campaign_route_also_threads_actor_into_the_inner_update_campaign_call():
    """create_campaign() composes create_campaign()+update_campaign() at
    this one route when the payload carries extra fields -- both calls
    must get the same actor."""
    service = _StubMailCampaignService()
    payload = MailCampaignCreateRequest(name="Test", timezone="America/Chicago")

    await create_campaign(payload, _FakeRequest("service_operator"), service=service)

    assert service.calls["update_campaign"].kwargs.get("actor") == "claude_operator"


async def test_create_campaign_route_passes_none_actor_for_a_service_read_identity():
    """The READ token's identity ("service_read") must never be treated
    as the operator identity -- only the exact string "service_operator"
    triggers attribution."""
    service = _StubMailCampaignService()
    payload = MailCampaignCreateRequest(name="Test")

    await create_campaign(payload, _FakeRequest("service_read"), service=service)

    assert service.calls["create_campaign"].kwargs.get("actor") is None


async def test_update_campaign_route_passes_actor_through():
    service = _StubMailCampaignService()

    await update_campaign("c1", _FakeRequest("service_operator"), patch={"name": "New"}, service=service)

    assert service.calls["update_campaign"].kwargs.get("actor") == "claude_operator"


async def test_mark_campaign_ready_route_passes_actor_through():
    campaign_service = _StubMailCampaignService()

    await mark_campaign_ready(
        "c1", _FakeRequest("service_operator"), campaign_service=campaign_service, suppression_service=_StubSuppressionService()
    )

    assert campaign_service.calls["mark_ready"].kwargs.get("actor") == "claude_operator"


async def test_unlock_campaign_route_passes_actor_through():
    service = _StubMailCampaignService()

    await unlock_campaign("c1", _FakeRequest("service_operator"), service=service)

    assert service.calls["unlock_campaign"].kwargs.get("actor") == "claude_operator"


async def test_set_campaign_schedule_route_passes_actor_through():
    service = _StubMailCampaignService()
    payload = MailCampaignScheduleUpdateRequest(timezone="UTC", windows=[])

    await set_campaign_schedule("c1", payload, _FakeRequest("service_operator"), service=service)

    assert service.calls["set_schedule"].kwargs.get("actor") == "claude_operator"


# --- app/api/crm.py ------------------------------------------------------


async def test_create_contact_list_route_passes_actor_through():
    service = _StubCrmService()
    req = CrmListCreateRequest(name="List")

    await create_contact_list(req, _FakeRequest("service_operator"), service=service)

    assert service.calls["create_contact_list"].kwargs.get("actor") == "claude_operator"


async def test_create_contact_list_route_passes_none_actor_for_an_ordinary_session():
    service = _StubCrmService()
    req = CrmListCreateRequest(name="List")

    await create_contact_list(req, _FakeRequest(None), service=service)

    assert service.calls["create_contact_list"].kwargs.get("actor") is None


async def test_update_contact_list_route_passes_actor_through():
    service = _StubCrmService()

    await update_contact_list("list-1", _FakeRequest("service_operator"), patch={"name": "New"}, service=service)

    assert service.calls["update_contact_list"].kwargs.get("actor") == "claude_operator"


async def test_bulk_add_to_list_route_passes_actor_through():
    service = _StubCrmService()
    req = CrmListBulkContactIdsRequest(contact_ids=["c1"])

    await bulk_add_to_list("list-1", req, _FakeRequest("service_operator"), service=service)

    assert service.calls["bulk_add_to_list"].kwargs.get("actor") == "claude_operator"


async def test_bulk_remove_from_list_route_passes_actor_through():
    service = _StubCrmService()
    req = CrmListBulkContactIdsRequest(contact_ids=["c1"])

    await bulk_remove_from_list("list-1", req, _FakeRequest("service_operator"), service=service)

    assert service.calls["bulk_remove_from_list"].kwargs.get("actor") == "claude_operator"
