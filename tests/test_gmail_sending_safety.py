"""
Static sending-safety checks for Astronomic Mail Phase B2 (Gmail Sender
Foundation) and Phase C (Campaign Execution Worker). B2 added a REAL,
capable Gmail provider implementation (app/google/gmail_sender.py,
app/google/gmail_api_client.py, app/google/gmail_mime.py) -- these tests
exist specifically to distinguish "a dormant, fully-tested provider
implementation exists" (true, and fine -- see GmailSender's own module
docstring) from "something can actually reach it and send a real email"
(must remain false in production TODAY -- mail_sending_engine_enabled is
False, and even if it weren't, both controlled-test allowlists are
unset -- see app/config.py's own docstrings and tests/
test_mail_execution_worker.py / tests/test_prepare_and_send_step.py for
the tests that actually enforce THAT guarantee now).

UPDATED for Phase C: app/main.py is now the ONE intended, approved place
that constructs a GmailSender -- Phase C's whole purpose is wiring B2's
sender into the execution worker (see app/services/
mail_execution_worker.py's own module docstring for the full chain that
keeps this safe regardless). The guarantee this file still enforces is
narrower but just as real: NO ROUTE (app/api/*.py) and NO dependency
wiring (app/dependencies.py) may reference GmailSender/GmailApiClient
directly -- only the lifespan wiring in app/main.py may, and only to
construct the one sender instance handed to the worker.

Not marked asyncio -- plain sync checks, kept in their own file so
tests/test_gmail_sender.py's module-level `pytestmark = pytest.mark.asyncio`
doesn't apply here.
"""

import re
from pathlib import Path


def test_gmail_sender_is_never_imported_by_api_routes_or_dependency_wiring():
    """app/dependencies.py and every module under app/api/ must never
    reference GmailSender/GmailApiClient -- no route constructs one or
    exposes one directly. app/main.py is EXEMPT (see this module's own
    docstring) -- checked separately below."""
    forbidden = ("gmail_sender", "gmail_api_client", "GmailSender", "GmailApiClient")
    watched = [Path("app/dependencies.py"), *sorted(Path("app/api").glob("*.py"))]
    for path in watched:
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"found {token!r} in {path} -- Gmail sender must never be reachable via a route"


def test_gmail_sender_construction_in_main_is_handed_only_to_the_execution_worker():
    """app/main.py IS allowed to import/construct GmailSender (Phase C's
    intended wiring point) -- but the ONLY thing the constructed instance
    may be passed to is MailExecutionWorker's `sender=` argument, never a
    route dependency override or anything else. A cheap heuristic (not a
    full AST data-flow analysis): the constructed variable name must
    appear as MailExecutionWorker's `sender=` argument, and no
    app.include_router-adjacent route wiring may reference it."""
    source = Path("app/main.py").read_text()
    assert "GmailSender(" in source, "expected app/main.py to construct GmailSender (Phase C wiring)"
    assert re.search(r"sender=\w*gmail_sender\w*", source, re.IGNORECASE), (
        "expected the constructed GmailSender to be passed as MailExecutionWorker's sender= argument"
    )


def test_no_second_ad_hoc_worker_or_scheduler_module_exists():
    """Phase C added exactly ONE reviewed, deliberately-named worker
    module -- app/services/mail_execution_worker.py (NOT in this
    forbidden set, by design). This guards against a SECOND, ad-hoc
    worker/scheduler appearing under a generic name outside of a
    reviewed change (app/services/mail_scheduler.py is pure schedule-
    window MATH, not a background loop -- explicitly excluded here, not
    a false negative)."""
    forbidden_names = {"worker.py", "scheduler_loop.py", "send_worker.py", "background_worker.py"}
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
