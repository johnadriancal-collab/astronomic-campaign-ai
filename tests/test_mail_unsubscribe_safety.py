"""
Static safety checks for Astronomic Mail Phase B3 (Unsubscribe
Architecture). B3 adds a real, working PUBLIC route family for the first
time in Astronomic Mail's history -- these tests exist to prove that
capability stays exactly as narrow as designed: it can unsubscribe an
address, and nothing else. It must never become a way to send, and it
must never be reachable except through the two exact paths this phase
approved.

Not marked asyncio -- plain sync source-scanning checks, mirroring
tests/test_gmail_sending_safety.py's own convention.
"""

import ast
import re
from pathlib import Path


def _code_body(path: Path) -> str:
    """The module's source with its top-level docstring stripped --
    every check below scans CODE, not prose. A module docstring
    legitimately mentions the very names/strings these checks forbid
    (explaining why they're absent, or referencing another module by
    name); only their appearance in actual code (imports, calls,
    identifiers) matters."""
    source = path.read_text()
    tree = ast.parse(source)
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        docstring_end_line = tree.body[0].end_lineno
        lines = source.splitlines()
        return "\n".join(lines[docstring_end_line:])
    return source


def test_public_paths_contains_exactly_the_expected_unsubscribe_entries():
    from app.session_auth_middleware import PUBLIC_PATHS

    unsubscribe_paths = {p for p in PUBLIC_PATHS if p.startswith("/mail/unsubscribe")}
    assert unsubscribe_paths == {"/mail/unsubscribe", "/mail/unsubscribe/one-click"}


def test_no_parameterized_unsubscribe_path_was_added():
    """The whole reason the token lives in the query string: PUBLIC_PATHS
    matches request.url.path by exact string only. A path segment like
    '/mail/unsubscribe/{token}' would silently never match and would 401
    -- guard against that mistake ever being introduced."""
    from app.session_auth_middleware import PUBLIC_PATHS

    assert not any("{" in p for p in PUBLIC_PATHS)


def test_mail_unsubscribe_module_is_never_imported_by_gmail_sender_or_execution_engine():
    """The reusable composition pieces (app/services/
    mail_unsubscribe_composition.py) must remain unwired -- nothing under
    app/google/ or app/services/mail_sending_service.py may import
    anything unsubscribe-shaped."""
    forbidden = ("mail_unsubscribe_composition", "unsubscribe_token", "mail_unsubscribe")
    watched = [
        Path("app/services/mail_sending_service.py"),
        Path("app/google/gmail_sender.py"),
        Path("app/google/gmail_api_client.py"),
    ]
    for path in watched:
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"found {token!r} in {path} -- unsubscribe must remain unwired in B3"


def test_mail_unsubscribe_route_module_has_no_gmail_send_capability():
    """Same discipline as every other route file in this app -- the
    public unsubscribe surface must never gain a path to a real send."""
    code = _code_body(Path("app/api/mail_unsubscribe.py"))
    for forbidden in ("gmail_sender", "gmail_api_client", "GmailSender", "GmailApiClient", "MailSenderPort"):
        assert forbidden not in code


def test_no_worker_or_scheduler_module_exists():
    forbidden_names = {"worker.py", "scheduler_loop.py", "send_worker.py", "background_worker.py", "mail_worker.py"}
    existing = {p.name for p in Path("app").rglob("*.py")}
    assert not (forbidden_names & existing)


def test_mail_sending_engine_enabled_still_defaults_false():
    from app.config import Settings

    assert Settings.model_fields["mail_sending_engine_enabled"].default is False


def test_composition_module_safety_note_still_present():
    """Cheap regression guard on the explicit safety note at the bottom
    of the composition module -- if a future edit removes it, this test
    should force a second look, not just quietly let the note vanish."""
    source = Path("app/services/mail_unsubscribe_composition.py").read_text()
    assert "SAFETY NOTE" in source


def test_unsubscribe_routes_never_read_cookies_or_auth_headers():
    """RFC 8058: the one-click POST 'MUST NOT include cookies, HTTP
    authorization, or any other context information.' Structural check:
    the route module must never reference request.cookies/request.headers
    at all -- it has no `request: Request` parameter on any handler, so
    there's nothing to read from in the first place."""
    code = _code_body(Path("app/api/mail_unsubscribe.py"))
    assert "request.cookies" not in code
    assert "request.headers" not in code
    assert re.search(r"\bRequest\b", code) is None


def test_one_click_response_declares_no_redirect_response_class():
    code = _code_body(Path("app/api/mail_unsubscribe.py"))
    assert "RedirectResponse" not in code
