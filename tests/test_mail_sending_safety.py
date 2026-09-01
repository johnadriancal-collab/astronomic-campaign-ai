"""
Static sending-safety checks for Astronomic Mail Phase A (durable execution
model). These are a backstop, not the primary guarantee -- the primary
guarantee is architectural: nothing under app/ ever WIRES a concrete
MailSenderPort implementation into anything reachable (no route, no
worker, no scheduler -- see tests/test_gmail_sending_safety.py, which is
where that guarantee is actually enforced now). See tests/
test_mail_sending_service.py for the behavioral tests (all driven by
FakeMailSender, defined only in tests/).

UPDATED for Phase B2 (Gmail Sender Foundation): MailSenderPort gained its
first real subclass, GmailSender (app/google/gmail_sender.py) -- see that
class's own module docstring for exactly why "a concrete implementation
exists" and "a concrete implementation is reachable" are deliberately
different guarantees, and why only the second one is the actual safety
boundary. test_no_concrete_mailsenderport_implementation_exists_under_app()
below is intentionally renamed/narrowed to reflect this: it now allow-lists
EXACTLY GmailSender (the one approved, fully-tested, dormant
implementation) and still fails on any OTHER concrete subclass appearing
anywhere under app/ -- an unreviewed second implementation slipping in
unnoticed remains exactly the failure mode this file exists to catch.

Not marked asyncio -- these are plain sync source-scanning checks, mirroring
tests/test_mailbox_sending_safety.py's own convention.
"""

import re
from pathlib import Path

APP_ROOT = Path("app")

# Phase B2: the ONE approved concrete MailSenderPort implementation.
# Adding a second entry here must never be a casual edit -- see this
# module's docstring.
_APPROVED_MAILSENDERPORT_IMPLEMENTATIONS = {
    ("app/google/gmail_sender.py", "GmailSender"),
}


def _all_app_source() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def test_only_the_approved_mailsenderport_implementation_exists_under_app():
    """MailSenderPort is an ABC with one abstract method (send()) -- every
    class under app/ that subclasses it must be exactly the
    Phase-B2-approved GmailSender; anything else is an unreviewed real
    send capability slipping into production code."""
    pattern = re.compile(r"class\s+(\w+)\s*\(\s*MailSenderPort\s*\)")
    found: set[tuple[str, str]] = set()
    for path in _all_app_source():
        for match in pattern.finditer(path.read_text()):
            found.add((str(path), match.group(1)))
    assert found == _APPROVED_MAILSENDERPORT_IMPLEMENTATIONS


def test_mail_sending_service_never_imports_gmail_smtp_or_oauth():
    """Static proof, at the IMPORT STATEMENT level (not prose/comments,
    which legitimately mention these names descriptively -- see this
    module's own docstring) -- mirrors test_mail_api.py's identical
    convention."""
    import ast
    import inspect

    import app.services.mail_sending_service as mail_sending_service

    tree = ast.parse(inspect.getsource(mail_sending_service))
    imported_module_paths = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    ]
    lowered_paths = [p.lower() for p in imported_module_paths]
    for forbidden in ("gmail", "smtp", "oauth", "googleapiclient"):
        assert not any(forbidden in p for p in lowered_paths), f"found forbidden import containing '{forbidden}'"


def test_mail_sending_service_never_calls_a_real_provider_client():
    """The one `.send(` call in this file is `sender.send(...)` against the
    injected MailSenderPort abstraction -- this asserts no OTHER
    send-shaped call to a real provider client exists (Gmail's
    `.messages().send(`, `smtplib.SMTP(...).send(`, etc.)."""
    source = Path("app/services/mail_sending_service.py").read_text()
    assert not re.search(r"\.messages\(\)\.send\(", source)
    assert not re.search(r"smtplib", source, re.IGNORECASE)
    assert not re.search(r"build\(\s*[\"']gmail[\"']", source, re.IGNORECASE)


def test_mail_api_has_no_send_queue_dispatch_or_worker_route():
    """/activate, /pause, /resume ARE legitimate routes now (see
    app/api/mail.py's module docstring) -- this checks for everything that
    would actually DISPATCH a send, which must still be completely
    absent."""
    source = Path("app/api/mail.py").read_text()
    for forbidden in ('"/send', '"/send-now', '"/queue', '"/dispatch', '"/launch', '"/start', '"/worker'):
        assert forbidden not in source, f"found forbidden route fragment '{forbidden}' in app/api/mail.py"


def test_mail_api_module_docstring_still_disclaims_gmail_capability():
    """The module docstring's safety claim must keep asserting no real
    Gmail/SMTP/worker capability exists -- a regression here means either
    the claim was weakened or (worse) it's now false."""
    source = Path("app/api/mail.py").read_text()
    docstring = source.split('"""')[1]
    assert "gmail" in docstring.lower() or "smtp" in docstring.lower()
    assert "worker" in docstring.lower()
