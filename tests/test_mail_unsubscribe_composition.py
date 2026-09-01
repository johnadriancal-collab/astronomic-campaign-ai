"""
app/services/mail_unsubscribe_composition.py -- reusable, currently-unwired
outbound composition pieces. Pure string logic + token generation; no
network, no store.
"""

import pytest
from cryptography.fernet import Fernet

from app.services.mail_unsubscribe_composition import (
    PublicOriginNotConfiguredError,
    build_unsubscribe_urls,
    compose_outbound_email,
)
from app.services.unsubscribe_token import decode_unsubscribe_token


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.unsubscribe_token.settings.unsubscribe_token_encryption_keys", Fernet.generate_key().decode()
    )


FAKE_ORIGIN = "https://fake-backend.test"


# --- build_unsubscribe_urls -------------------------------------------------------


def test_both_urls_share_the_same_token():
    confirm_url, one_click_url = build_unsubscribe_urls("a@example.com", public_origin=FAKE_ORIGIN)
    confirm_token = confirm_url.split("token=", 1)[1]
    one_click_token = one_click_url.split("token=", 1)[1]
    assert confirm_token == one_click_token


def test_urls_point_at_the_correct_distinct_paths():
    confirm_url, one_click_url = build_unsubscribe_urls("a@example.com", public_origin=FAKE_ORIGIN)
    assert confirm_url.startswith(f"{FAKE_ORIGIN}/mail/unsubscribe?token=")
    assert not confirm_url.startswith(f"{FAKE_ORIGIN}/mail/unsubscribe/one-click")
    assert one_click_url.startswith(f"{FAKE_ORIGIN}/mail/unsubscribe/one-click?token=")


def test_token_in_the_url_decodes_to_the_right_email():
    confirm_url, _ = build_unsubscribe_urls("someone@example.com", public_origin=FAKE_ORIGIN)
    token = confirm_url.split("token=", 1)[1]
    assert decode_unsubscribe_token(token) == "someone@example.com"


def test_trailing_slash_on_origin_is_normalized():
    confirm_url, _ = build_unsubscribe_urls("a@example.com", public_origin=f"{FAKE_ORIGIN}/")
    assert confirm_url.startswith(f"{FAKE_ORIGIN}/mail/unsubscribe?token=")
    assert "//mail" not in confirm_url


def test_missing_origin_fails_closed(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", None)
    with pytest.raises(PublicOriginNotConfiguredError):
        build_unsubscribe_urls("a@example.com")


def test_explicit_public_origin_always_wins_over_settings(monkeypatch):
    """The exact seam the B3 approval calls out for tests: 'For tests
    use an explicit fake origin.'"""
    monkeypatch.setattr(
        "app.services.mail_unsubscribe_composition.settings.public_backend_origin", "https://real-origin.example"
    )
    confirm_url, _ = build_unsubscribe_urls("a@example.com", public_origin=FAKE_ORIGIN)
    assert confirm_url.startswith(FAKE_ORIGIN)
    assert "real-origin.example" not in confirm_url


# --- compose_outbound_email -------------------------------------------------------


def test_footer_is_appended_without_mutating_the_original_string():
    original = "Hi there, join us for dinner."
    result = compose_outbound_email(snapshot_body=original, recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert original == "Hi there, join us for dinner."  # unchanged
    assert result.body != original
    assert result.body.startswith(original)
    assert "Unsubscribe:" in result.body


def test_composed_body_contains_the_confirm_url_not_the_one_click_url():
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert "/mail/unsubscribe?token=" in result.body
    assert "/mail/unsubscribe/one-click" not in result.body


def test_list_unsubscribe_header_points_at_one_click_and_is_bracketed():
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert result.list_unsubscribe_header.startswith("<")
    assert result.list_unsubscribe_header.endswith(">")
    assert "/mail/unsubscribe/one-click?token=" in result.list_unsubscribe_header


def test_list_unsubscribe_post_header_is_the_exact_rfc_8058_literal():
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert result.list_unsubscribe_post_header == "List-Unsubscribe=One-Click"


def test_footer_url_and_header_url_share_the_same_token():
    """The core B3 decision: one token per outbound message, reused by
    both the human footer and the one-click header."""
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    footer_token = result.body.split("token=", 1)[1].strip()
    header_token = result.list_unsubscribe_header.split("token=", 1)[1].rstrip(">")
    assert footer_token == header_token


def test_compose_fails_closed_without_origin(monkeypatch):
    monkeypatch.setattr("app.services.mail_unsubscribe_composition.settings.public_backend_origin", None)
    with pytest.raises(PublicOriginNotConfiguredError):
        compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com")
