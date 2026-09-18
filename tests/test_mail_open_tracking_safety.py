"""
Static safety checks for Astronomic Mail open tracking (2026-09-18) --
same posture as tests/test_mail_unsubscribe_safety.py: this is a real,
PUBLIC, unauthenticated route. These tests prove it stays exactly as
narrow as designed: it can record an open and return a tiny GIF, and
nothing else. It must never become a way to send, and must never be
reachable except through the one exact path this feature approved.

Not marked asyncio -- plain sync source-scanning checks.
"""

from pathlib import Path


def test_public_paths_contains_exactly_the_expected_open_tracking_entry():
    from app.session_auth_middleware import PUBLIC_PATHS

    open_tracking_paths = {p for p in PUBLIC_PATHS if p.startswith("/mail/track")}
    assert open_tracking_paths == {"/mail/track/open"}


def test_no_parameterized_open_tracking_path_was_added():
    """Same reasoning as the unsubscribe routes: PUBLIC_PATHS matches
    request.url.path by exact string only -- the token must stay in the
    query string, never a path segment."""
    from app.session_auth_middleware import PUBLIC_PATHS

    assert not any("{" in p for p in PUBLIC_PATHS)


def test_mail_open_tracking_module_never_imports_anything_gmail_send_capable():
    source = Path("app/api/mail_open_tracking.py").read_text()
    assert "gmail_sender" not in source
    assert "gmail_api_client" not in source
    assert "MailSenderPort" not in source
    assert "app.google" not in source


def test_mail_open_tracking_service_never_imports_anything_gmail_send_capable():
    source = Path("app/services/mail_open_tracking_service.py").read_text()
    assert "gmail_sender" not in source
    assert "gmail_api_client" not in source
    assert "MailSenderPort" not in source
    assert "app.google" not in source
