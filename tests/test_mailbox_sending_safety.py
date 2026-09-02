"""
Static sending-safety checks for Astronomic Mail Phase 2 (Google Workspace
Mailbox Connection) + Phase B1 (Gmail scope upgrade + token refresh
foundation). These are a backstop, not the primary guarantee -- the
primary guarantee is that no send-capable code exists at all (see
tests/test_mailbox_service.py for the behavioral OAuth-flow tests, none of
which ever calls anything resembling a Gmail send).

Not marked asyncio -- these are plain sync checks, kept in their own file
so tests/test_mailbox_service.py's module-level `pytestmark =
pytest.mark.asyncio` doesn't apply to them.
"""

import re
from pathlib import Path

from app.google.oauth_client import GMAIL_SEND_SCOPE, GoogleOAuthClient, SCOPES
from app.config import settings


def test_oauth_scopes_are_exactly_openid_email_profile():
    assert set(SCOPES) == {"openid", "email", "profile"}


def test_gmail_send_scope_constant_is_the_expected_url_but_never_in_base_scopes():
    """Phase B1: GMAIL_SEND_SCOPE exists (a caller -- MailboxService.
    begin_gmail_send_upgrade() -- can explicitly opt into requesting it),
    but the BASE `SCOPES` tuple the ordinary connect flow always uses must
    never silently include it."""
    assert GMAIL_SEND_SCOPE == "https://www.googleapis.com/auth/gmail.send"
    assert GMAIL_SEND_SCOPE not in SCOPES


def test_mailbox_public_model_has_no_credential_fields_at_all():
    """Structural, not just behavioral: the public Mailbox model (every
    response_model in app/api/mailboxes.py) has neither of
    MailboxCredential's two secret fields -- `encrypted_refresh_token` and
    (Phase B1's new) `previous_encrypted_refresh_token` -- cannot leak
    through it even if a future route accidentally tried to serialize the
    wrong object, because FastAPI's response_model validation would
    simply drop/reject fields that don't exist on Mailbox. `mailbox_id` is
    deliberately excluded from this check -- it's the shared join key,
    not a secret, and legitimately appears on both models."""
    from app.models.mailbox import Mailbox, MailboxCredential

    credential_secret_fields = MailboxCredential.model_fields.keys() - {"mailbox_id", "created_at", "updated_at"}
    assert credential_secret_fields == {"encrypted_refresh_token", "previous_encrypted_refresh_token"}
    assert Mailbox.model_fields.keys().isdisjoint(credential_secret_fields)


def test_no_gmail_api_send_endpoint_url_appears_anywhere_in_oauth_or_mailbox_code():
    """The most direct possible signal that a real sending capability was
    added: the literal Gmail API send-endpoint URL/path
    (`gmail/v1/users/.../messages/send`) must not appear anywhere in the
    OAuth client or mailbox service, even as a stray string constant."""
    for path in ("app/google/oauth_client.py", "app/services/mailbox_service.py", "app/api/mailboxes.py"):
        source = Path(path).read_text()
        assert "messages/send" not in source, f"found a Gmail send-endpoint-shaped string in {path}"
        assert "googleapis.com/gmail" not in source, f"found a Gmail API host reference in {path}"


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


def test_mailboxes_api_declares_only_the_five_approved_routes():
    source = Path("app/api/mailboxes.py").read_text()
    routes = re.findall(r'@router\.(get|post|patch|delete)\("([^"]*)"', source)
    assert set(routes) == {
        ("get", ""),
        ("get", "/google/start"),
        ("get", "/{mailbox_id}/google/gmail-send/start"),
        ("get", "/google/callback"),
        ("post", "/{mailbox_id}/disconnect"),
    }


# --- No OAuth logging call independently logs a secret/state/URL -----------
# Complements app/access_log_filter.py's fix for Uvicorn's OWN access log
# (a framework-level leak, not this application's own logging -- see that
# module's docstring). This checks the OTHER, application-level half of
# the same concern: that no `logger.*()` call in the OAuth-adjacent
# modules independently interpolates an authorization code, an access/
# refresh token, an OAuth `state` value, or a constructed request URL/
# query string into its own message. AST-based (not a blanket substring
# search across the whole file) specifically so this doesn't false-
# positive on legitimate, harmless identifiers that merely CONTAIN one
# of these words as a substring (e.g. `resp.status_code`,
# `MailboxOAuthStateError`, `_consume_state()`) -- only a WHOLE
# interpolated identifier/attribute access matching one of these names
# is forbidden.


def _logger_call_interpolated_expressions(path: str) -> list[str]:
    """Every f-string expression (as unparsed source text) interpolated
    into a `logger.<level>(...)` call anywhere in `path`."""
    import ast

    source = Path(path).read_text()
    tree = ast.parse(source)
    expressions: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                for value in arg.values:
                    if isinstance(value, ast.FormattedValue):
                        expressions.append(ast.unparse(value.value))
    return expressions


def test_no_oauth_logging_call_interpolates_a_code_state_token_or_url():
    forbidden_whole_identifiers = (
        r"\bcode\b",
        r"\bstate\b",
        r"\baccess_token\b",
        r"\brefresh_token\b",
        r"\bquery_string\b",
        r"\burl\b",
        r"\brequest\b",
    )
    for path in ("app/services/mailbox_service.py", "app/google/oauth_client.py"):
        for expr in _logger_call_interpolated_expressions(path):
            for pattern in forbidden_whole_identifiers:
                assert not re.search(pattern, expr), f"{path}: logger call interpolates {expr!r} (matches {pattern})"


def test_no_oauth_logging_call_interpolates_more_than_a_short_identifier_or_enum_value():
    """A second, structural layer on top of the name-based check above:
    every interpolated expression across both modules is short (an id,
    an enum's .value, a type name, an HTTP status code) -- none of them
    is a call to str()/repr() against a whole request/response object,
    which could smuggle a secret through under a name this test doesn't
    already know to forbid."""
    for path in ("app/services/mailbox_service.py", "app/google/oauth_client.py"):
        for expr in _logger_call_interpolated_expressions(path):
            assert len(expr) < 40, f"{path}: suspiciously long interpolated logger expression: {expr!r}"
