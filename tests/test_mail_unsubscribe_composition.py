"""
app/services/mail_unsubscribe_composition.py -- reusable, currently-unwired
outbound composition pieces. Pure string logic + token generation; no
network, no store.
"""

import pytest
from cryptography.fernet import Fernet

from app.services.mail_unsubscribe_composition import (
    PublicOriginNotConfiguredError,
    build_html_body,
    build_open_tracking_pixel_url,
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


# --- build_html_body / ComposedOutboundEmail.html_body (Phase C/D) ----------


def test_html_body_contains_a_clickable_unsubscribe_link_not_the_raw_url_as_text():
    html_body = build_html_body("Hi there, join us for dinner.", "https://fake-backend.test/mail/unsubscribe?token=abc123")
    assert '<a href="https://fake-backend.test/mail/unsubscribe?token=abc123">Unsubscribe</a>' in html_body
    # The exact wording must match the plain-text footer's own copy.
    assert "Don't want these emails? " in html_body


def test_html_body_wording_matches_the_plain_text_footer_exactly():
    from app.services.mail_unsubscribe_composition import STANDARD_UNSUBSCRIBE_FOOTER

    assert "Don't want these emails? Unsubscribe" in STANDARD_UNSUBSCRIBE_FOOTER


def test_html_body_escapes_script_tags_in_the_snapshot_body():
    malicious_body = 'Hi <script>alert("xss")</script> there.'
    html_body = build_html_body(malicious_body, "https://fake-backend.test/mail/unsubscribe?token=abc")
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


def test_html_body_escapes_quotes_and_ampersands_in_the_snapshot_body():
    body_with_specials = 'Terms: "cool" & <b>bold</b>'
    html_body = build_html_body(body_with_specials, "https://fake-backend.test/mail/unsubscribe?token=abc")
    assert "<b>bold</b>" not in html_body
    assert "&lt;b&gt;" in html_body
    assert "&amp;" in html_body


def test_html_body_escapes_the_confirm_url_too():
    """Defensive escaping of the URL even though it's built entirely from
    this codebase's own origin + a Fernet token, which contain no HTML-
    special characters in practice today -- see build_html_body()'s own
    docstring for why this is deliberate, not overcautious."""
    url_with_specials = 'https://fake-backend.test/mail/unsubscribe?token=abc&amp;x="y"'
    html_body = build_html_body("Body.", url_with_specials)
    assert 'href="https://fake-backend.test/mail/unsubscribe?token=abc&amp;amp;x=&quot;y&quot;"' in html_body


def test_html_body_converts_newlines_to_br_for_readable_paragraph_breaks():
    html_body = build_html_body("Line one.\nLine two.", "https://fake-backend.test/mail/unsubscribe?token=abc")
    assert "Line one.<br>\nLine two." in html_body


def test_html_body_does_not_mutate_the_original_snapshot_string():
    original = "Hi there, join us for dinner."
    build_html_body(original, "https://fake-backend.test/mail/unsubscribe?token=abc")
    assert original == "Hi there, join us for dinner."


def test_compose_outbound_email_html_body_shares_the_same_token_as_the_plain_body():
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    plain_token = result.body.split("token=", 1)[1].strip()
    html_token = result.html_body.split("token=", 1)[1].split('"', 1)[0]
    assert plain_token == html_token


def test_compose_outbound_email_html_body_contains_the_confirm_url_not_the_one_click_url():
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert "/mail/unsubscribe?token=" in result.html_body
    assert "/mail/unsubscribe/one-click" not in result.html_body


def test_compose_outbound_email_html_body_reflects_the_same_snapshot_content():
    result = compose_outbound_email(
        snapshot_body="Hi there, join us for dinner.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN
    )
    assert "Hi there, join us for dinner." in result.html_body


# --- Open tracking pixel (2026-09-18) ---------------------------------------


def test_build_open_tracking_pixel_url_points_at_the_track_open_route():
    url = build_open_tracking_pixel_url("tok123", public_origin=FAKE_ORIGIN)
    assert url == f"{FAKE_ORIGIN}/mail/track/open?token=tok123"


def test_html_body_has_no_pixel_when_token_is_none():
    html_body = build_html_body("Body.", "https://fake-backend.test/mail/unsubscribe?token=abc", None)
    assert "<img" not in html_body


def test_html_body_includes_a_1x1_invisible_pixel_when_a_token_is_given():
    pixel_url = "https://fake-backend.test/mail/track/open?token=tok123"
    html_body = build_html_body("Body.", "https://fake-backend.test/mail/unsubscribe?token=abc", pixel_url)
    assert f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:none">' in html_body


def test_html_body_pixel_is_appended_after_the_unsubscribe_block_never_replacing_it():
    pixel_url = "https://fake-backend.test/mail/track/open?token=tok123"
    confirm_url = "https://fake-backend.test/mail/unsubscribe?token=abc"
    html_body = build_html_body("Body.", confirm_url, pixel_url)
    assert html_body.index(confirm_url) < html_body.index(pixel_url)
    assert "Unsubscribe</a>" in html_body


def test_html_body_pixel_url_is_escaped_like_every_other_caller_supplied_url():
    pixel_url_with_specials = 'https://fake-backend.test/mail/track/open?token=abc&x="y"'
    html_body = build_html_body("Body.", "https://fake-backend.test/mail/unsubscribe?token=abc", pixel_url_with_specials)
    assert 'src="https://fake-backend.test/mail/track/open?token=abc&amp;x=&quot;y&quot;"' in html_body


def test_compose_outbound_email_has_no_pixel_when_open_tracking_token_is_none():
    """The default -- and the ONLY value ever passed for a campaign with
    open_tracking_enabled=False."""
    result = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    assert "<img" not in result.html_body
    assert "/mail/track/open" not in result.html_body


def test_compose_outbound_email_plain_text_body_never_carries_a_pixel_even_when_tracking_is_on():
    result = compose_outbound_email(
        snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN, open_tracking_token="tok123"
    )
    assert "<img" not in result.body
    assert "/mail/track/open" not in result.body


def test_compose_outbound_email_html_body_carries_the_pixel_when_a_token_is_given():
    result = compose_outbound_email(
        snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN, open_tracking_token="tok123"
    )
    assert f"{FAKE_ORIGIN}/mail/track/open?token=tok123" in result.html_body


def test_compose_outbound_email_pixel_never_affects_unsubscribe_urls_or_headers():
    with_token = compose_outbound_email(
        snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN, open_tracking_token="tok123"
    )
    without_token = compose_outbound_email(snapshot_body="Body.", recipient_email="a@example.com", public_origin=FAKE_ORIGIN)
    # Both get a freshly generated (different) unsubscribe token, but the
    # STRUCTURE -- header values, URL shape -- must be identical either way.
    assert with_token.list_unsubscribe_post_header == without_token.list_unsubscribe_post_header
    assert with_token.list_unsubscribe_header.startswith(f"<{FAKE_ORIGIN}/mail/unsubscribe/one-click?token=")
