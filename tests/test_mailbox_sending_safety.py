"""
Static sending-safety checks for Astronomic Mail Phase 2 (Google Workspace
Mailbox Connection). These are a backstop, not the primary guarantee --
the primary guarantee is that no send-capable code exists at all (see
tests/test_mailbox_service.py for the behavioral OAuth-flow tests, none of
which ever calls anything resembling a Gmail send).

Not marked asyncio -- these are plain sync checks, kept in their own file
so tests/test_mailbox_service.py's module-level `pytestmark =
pytest.mark.asyncio` doesn't apply to them.
"""

import re
from pathlib import Path

from app.google.oauth_client import GoogleOAuthClient, SCOPES
from app.config import settings


def test_oauth_scopes_are_exactly_openid_email_profile():
    assert set(SCOPES) == {"openid", "email", "profile"}


def test_authorize_url_has_no_hosted_domain_restriction(monkeypatch):
    """No `hd` (hosted domain) parameter, and no domain allowlist of any
    kind -- any Google account, on any Workspace organization, must be able
    to complete the same OAuth app's consent flow."""
    monkeypatch.setattr(settings, "google_oauth_client_id", "fake-client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "fake-client-secret")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "https://example.com/mailboxes/google/callback")

    url = GoogleOAuthClient().build_authorize_url("fake-state")

    assert "hd=" not in url


def test_oauth_scopes_never_include_a_gmail_scope():
    assert not any("gmail" in scope.lower() for scope in SCOPES)


def test_google_oauth_client_never_calls_a_send_method():
    source = Path("app/google/oauth_client.py").read_text()
    assert not re.search(r"\.send\s*\(", source)


def test_mailbox_service_never_calls_a_send_method():
    source = Path("app/services/mailbox_service.py").read_text()
    assert not re.search(r"\.send\s*\(", source)


def test_mailboxes_api_has_no_send_queue_or_activate_route():
    source = Path("app/api/mailboxes.py").read_text()
    for forbidden in ('"/send', '"/queue', '"/activate'):
        assert forbidden not in source


def test_mailboxes_api_declares_only_the_four_approved_routes():
    source = Path("app/api/mailboxes.py").read_text()
    routes = re.findall(r'@router\.(get|post|patch|delete)\("([^"]*)"', source)
    assert set(routes) == {
        ("get", ""),
        ("get", "/google/start"),
        ("get", "/google/callback"),
        ("post", "/{mailbox_id}/disconnect"),
    }
