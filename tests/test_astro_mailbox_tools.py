"""
AstroMailboxTools tests -- Astro AI Phase 3 read-only mailbox surface.
Exercised against a REAL MailboxStore (in-memory), never a mock, so these
prove actual query behavior. Special attention to the structural
credential-safety guarantee: AstroMailboxTools is constructed with a
MailboxStore directly, never a MailboxService (which also holds a
MailboxCredentialStore reference) -- see astro_mailbox_tools.py's module
docstring.
"""

import ast
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.repositories.mailbox_store import MemoryMailboxStore
from app.services.astro_mailbox_tools import ASTRO_MAILBOX_TOOL_DEFINITIONS, MAILBOX_LIST_LIMIT, AstroMailboxTools

pytestmark = pytest.mark.asyncio


def _now():
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def make_mailbox(**overrides) -> Mailbox:
    defaults = dict(
        mailbox_id=str(uuid.uuid4()),
        provider=MailboxProvider.GOOGLE,
        email="someone@astronomicconnect.com",
        display_name="Someone",
        status=MailboxStatus.CONNECTED,
        google_user_id="google-sub-123",
        granted_scopes=["openid", "email", "profile"],
        connected_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return Mailbox(**defaults)


@pytest_asyncio.fixture
async def mailbox_store():
    store = MemoryMailboxStore()
    await store.create(
        make_mailbox(email="victoria@astronomicconnect.com", display_name="Victoria Bennett")
    )
    return store


@pytest.fixture
def tools(mailbox_store):
    return AstroMailboxTools(mailbox_store)


async def test_list_connected_mailboxes_returns_safe_fields_only(tools):
    result = await tools.dispatch("list_connected_mailboxes", {})
    assert result["total"] == 1
    mailbox = result["mailboxes"][0]
    assert mailbox["email"] == "victoria@astronomicconnect.com"
    assert mailbox["display_name"] == "Victoria Bennett"
    assert mailbox["status"] == "connected"
    assert set(mailbox.keys()) == {
        "email", "display_name", "provider", "status", "connected_at", "disconnected_at", "granted_scopes"
    }


async def test_list_connected_mailboxes_never_exceeds_hard_limit(tools, mailbox_store):
    for i in range(MAILBOX_LIST_LIMIT + 5):
        await mailbox_store.create(make_mailbox(email=f"bulk{i}@astronomicconnect.com"))

    result = await tools.dispatch("list_connected_mailboxes", {})

    assert result["total"] > MAILBOX_LIST_LIMIT
    assert result["returned"] == MAILBOX_LIST_LIMIT
    assert len(result["mailboxes"]) == MAILBOX_LIST_LIMIT


async def test_get_mailbox_by_exact_email(tools):
    result = await tools.dispatch("get_mailbox", {"email": "victoria@astronomicconnect.com"})
    assert result["status"] == "found"
    assert result["mailbox"]["display_name"] == "Victoria Bennett"


async def test_get_mailbox_not_found_is_explicit(tools):
    result = await tools.dispatch("get_mailbox", {"email": "nobody@astronomicconnect.com"})
    assert result == {"status": "not_found"}


async def test_get_mailbox_ambiguous_by_name_never_arbitrarily_picks_one(tools, mailbox_store):
    await mailbox_store.create(make_mailbox(email="other@astronomicconnect.com", display_name="Victoria Bennett"))

    result = await tools.dispatch("get_mailbox", {"name": "Victoria Bennett"})

    assert result["status"] == "ambiguous"
    assert result["total"] == 2


async def test_get_mailbox_requires_email_or_name(tools):
    result = await tools.dispatch("get_mailbox", {})
    assert result["error"] == "invalid_filter"


async def test_unknown_tool_name_is_rejected(tools):
    result = await tools.dispatch("disconnect_mailbox", {})
    assert result == {"error": "unknown_tool", "message": "'disconnect_mailbox' is not an available tool."}


async def test_connect_and_reconnect_tool_names_are_not_available(tools):
    for name in ["connect_mailbox", "reconnect_mailbox", "delete_mailbox", "get_mailbox_credential", "get_oauth_token"]:
        result = await tools.dispatch(name, {})
        assert result["error"] == "unknown_tool"


async def test_no_response_ever_contains_credential_fields(tools):
    """The Mailbox model has no credential field at all, so this is really
    proving the projection can't accidentally include one even if the
    model grew a field later -- an explicit allowlist, not just 'whatever
    the model has'."""
    result = await tools.dispatch("list_connected_mailboxes", {})
    serialized = str(result)
    for forbidden in ["refresh_token", "access_token", "encrypted", "client_secret", "encryption_key"]:
        assert forbidden not in serialized.lower()


async def test_projection_contains_only_the_allowlisted_fields(tools):
    result = await tools.dispatch("get_mailbox", {"email": "victoria@astronomicconnect.com"})
    assert set(result["mailbox"].keys()) == {
        "email", "display_name", "provider", "status", "connected_at", "disconnected_at", "granted_scopes"
    }


# --- structural credential protection (not just convention) ----------------


def test_astro_mailbox_tools_constructor_only_accepts_a_mailbox_store():
    """AstroMailboxTools.__init__ must type-hint MailboxStore, never
    MailboxService or MailboxCredentialStore -- the structural guarantee
    the architecture review called for."""
    import inspect

    sig = inspect.signature(AstroMailboxTools.__init__)
    params = list(sig.parameters.values())
    assert len(params) == 2  # self, mailbox_store
    annotation = params[1].annotation
    assert "MailboxStore" in str(annotation)
    assert "MailboxService" not in str(annotation)
    assert "Credential" not in str(annotation)


def test_astro_mailbox_tools_never_imports_credential_store_or_service():
    tree = ast.parse(Path("app/services/astro_mailbox_tools.py").read_text())
    imported_modules = set()
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
    assert "app.repositories.mailbox_credential_store" not in imported_modules
    assert "app.services.mailbox_service" not in imported_modules
    assert "MailboxService" not in imported_names
    assert "MailboxCredentialStore" not in imported_names
    assert "MailboxCredential" not in imported_names


def test_astro_mailbox_tools_instance_has_no_credential_store_attribute():
    """Even if a future edit added a second constructor arg, an instance
    built the documented way structurally cannot hold a credential store
    reference today."""
    from app.repositories.mailbox_store import MemoryMailboxStore

    tools = AstroMailboxTools(MemoryMailboxStore())
    attrs = vars(tools)
    assert set(attrs.keys()) == {"mailbox_store"}


def test_no_write_tools_exist_in_the_mailbox_tool_registry():
    names = {t["name"] for t in ASTRO_MAILBOX_TOOL_DEFINITIONS}
    assert names == {"list_connected_mailboxes", "get_mailbox"}
    for forbidden in ["disconnect", "reconnect", "credential", "token", "delete", "update", "create"]:
        assert not any(forbidden in name.lower() for name in names)


def test_mailbox_tool_descriptions_disclaim_rather_than_promise_placeholder_stats():
    """Deliverability Index / Emails Sent Today / Queue / Campaigns are
    frontend-only placeholders with no backing Mailbox field. The tool
    descriptions are expected to explicitly DISCLAIM them (so Claude knows
    not to ask about or report them) -- this asserts the disclaimer is
    present, not that the words never appear."""
    full_text = " ".join(t["description"].lower() for t in ASTRO_MAILBOX_TOOL_DEFINITIONS)
    assert "no sending, deliverability, queue, or campaign-count data" in full_text
