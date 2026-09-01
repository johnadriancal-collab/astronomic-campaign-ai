"""
Static sending-safety checks for Astronomic Mail Phase B2 (Gmail Sender
Foundation). B2 adds a REAL, capable Gmail provider implementation for the
first time in this codebase (app/google/gmail_sender.py,
app/google/gmail_api_client.py, app/google/gmail_mime.py) -- these tests
exist specifically to distinguish "a dormant, fully-tested provider
implementation exists" (true after B2, and fine -- see GmailSender's own
module docstring) from "something can actually reach it and send a real
email" (must remain false everywhere outside a direct, deliberate test
call). The primary guarantee is still architectural (no route/worker/
scheduler wires a real sender to anything); these are a backstop, same
role tests/test_mailbox_sending_safety.py plays for Phase 2/B1.

Not marked asyncio -- plain sync checks, kept in their own file so
tests/test_gmail_sender.py's module-level `pytestmark = pytest.mark.asyncio`
doesn't apply here.
"""

import re
from pathlib import Path


def test_gmail_sender_is_never_imported_by_app_wiring_or_api_routes():
    """app/main.py (app startup wiring), app/dependencies.py, and every
    module under app/api/ must never reference GmailSender/GmailApiClient
    -- nothing constructs a real sender and nothing exposes one through a
    route."""
    forbidden = ("gmail_sender", "gmail_api_client", "GmailSender", "GmailApiClient")
    watched = [Path("app/main.py"), Path("app/dependencies.py"), *sorted(Path("app/api").glob("*.py"))]
    for path in watched:
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"found {token!r} in {path} -- Gmail sender must remain unwired in B2"


def test_no_background_worker_or_scheduler_module_exists():
    """Phase B2 explicitly does not add a worker/scheduler -- no module
    anywhere under app/ may be named like one yet (app/services/
    mail_scheduler.py is pure schedule-window MATH, not a background
    loop -- explicitly excluded here, not a false negative)."""
    forbidden_names = {"worker.py", "scheduler_loop.py", "send_worker.py", "background_worker.py", "mail_worker.py"}
    existing = {p.name for p in Path("app").rglob("*.py")}
    assert not (forbidden_names & existing), existing & forbidden_names


def test_mail_sending_engine_enabled_still_defaults_false():
    from app.config import Settings

    assert Settings.model_fields["mail_sending_engine_enabled"].default is False


def test_mailboxes_api_still_declares_only_the_four_approved_routes():
    """Re-asserted here (duplicate of a B1 check in
    tests/test_mailbox_sending_safety.py) so a future PR that touches
    app/api/mailboxes.py as part of Gmail-sending work trips THIS file
    too, not only the B1 one."""
    source = Path("app/api/mailboxes.py").read_text()
    routes = re.findall(r'@router\.(get|post|patch|delete)\("([^"]*)"', source)
    assert set(routes) == {
        ("get", ""),
        ("get", "/google/start"),
        ("get", "/google/callback"),
        ("post", "/{mailbox_id}/disconnect"),
    }


def test_no_send_or_test_send_or_activate_route_exists_anywhere_in_the_api():
    for path in sorted(Path("app/api").glob("*.py")):
        source = path.read_text()
        assert '"/send' not in source, f"found a /send-shaped route in {path}"
        assert '"/test-send' not in source, f"found a /test-send-shaped route in {path}"


def test_gmail_send_endpoint_url_appears_only_in_the_gmail_api_client_module():
    """The literal Gmail send endpoint now legitimately exists in this
    codebase (app/google/gmail_api_client.py) -- but ONLY there. Anywhere
    else it appeared would be a second, unaudited path capable of
    reaching Gmail."""
    hits = []
    allowed = Path("app/google/gmail_api_client.py")
    for path in Path("app").rglob("*.py"):
        if path == allowed:
            continue
        source = path.read_text()
        if "messages/send" in source or "googleapis.com/gmail" in source:
            hits.append(str(path))
    assert hits == []


def test_gmail_sender_module_has_no_module_level_instances():
    """Importing app/google/gmail_sender.py or app/google/gmail_api_client.py
    must never itself construct a GmailSender/GmailApiClient instance --
    they should be pure class/function definitions, exactly like every
    other provider client in this codebase (GoogleOAuthClient, LumaClient).
    Checked by actually importing both modules and inspecting every
    module-level name, not by string-matching source text."""
    import app.google.gmail_api_client as api_mod
    import app.google.gmail_sender as sender_mod
    from app.google.gmail_api_client import GmailApiClient
    from app.google.gmail_sender import GmailSender

    for mod in (api_mod, sender_mod):
        for name, value in vars(mod).items():
            if name.startswith("_"):
                continue
            assert not isinstance(value, (GmailSender, GmailApiClient)), (
                f"{mod.__name__}.{name} is a module-level {type(value).__name__} instance"
            )


def test_no_test_module_exercises_gmail_sender_against_a_real_network():
    """Every test file that imports GmailSender or GmailApiClient must
    also reference a fake/mock -- a MockTransport, a Fake* double, or
    monkeypatch -- never a bare, real instantiation pointed at the
    internet. A cheap heuristic, not a substitute for code review, but
    catches the obvious mistake."""
    for path in sorted(Path("tests").glob("test_gmail_*.py")):
        source = path.read_text()
        if "GmailSender" in source or "GmailApiClient" in source:
            assert (
                "MockTransport" in source or "Fake" in source or "monkeypatch" in source
            ), f"{path} touches a real Gmail client with no visible fake/mock"
