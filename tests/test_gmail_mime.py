"""
app/google/gmail_mime.py -- pure MIME construction, zero network/mailbox
involved. No asyncio needed (every function here is sync). Message-ID
GENERATION is tested separately -- see tests/test_rfc_message_id.py --
since app/services/rfc_message_id.py is where that now lives (the B2
hardening pass moved it there; see gmail_mime.py's module docstring).
"""

import base64
from datetime import datetime, timezone
from email import policy
from email import message_from_bytes as _message_from_bytes

import pytest

from app.google.gmail_mime import HeaderInjectionError, build_mime_message, encode_gmail_raw

# --- build_mime_message: required headers ------------------------------------


def test_all_required_headers_are_present():
    raw = build_mime_message(
        from_email="victoria@astronomic.com",
        to_email="lead@example.com",
        subject="Let's talk",
        body="Hello there.",
        rfc_message_id="abc123@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["From"] == "victoria@astronomic.com"
    assert parsed["To"] == "lead@example.com"
    assert parsed["Subject"] == "Let's talk"
    assert parsed["Message-ID"] == "<abc123@astronomic.com>"
    assert parsed["Date"] is not None
    assert parsed["MIME-Version"] == "1.0"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content_charset() == "utf-8"
    assert parsed["Content-Transfer-Encoding"] is not None
    assert parsed.get_content().strip() == "Hello there."


def test_explicit_date_is_used_verbatim_when_given():
    fixed = datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc)
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com", date=fixed,
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert "15 Jan 2026 12:30:00" in parsed["Date"]


def test_no_in_reply_to_or_references_header_for_a_first_message():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


# --- build_mime_message: threading headers -----------------------------------


def test_in_reply_to_and_single_reference_are_wrapped_in_angle_brackets():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="Re: s", body="b",
        rfc_message_id="mid2@astronomic.com",
        in_reply_to_message_id="mid1@astronomic.com",
        references=["mid1@astronomic.com"],
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["In-Reply-To"] == "<mid1@astronomic.com>"
    assert parsed["References"] == "<mid1@astronomic.com>"


def test_references_chain_renders_as_space_separated_bracketed_ids_in_order():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="Re: s", body="b",
        rfc_message_id="mid3@astronomic.com",
        in_reply_to_message_id="mid2@astronomic.com",
        references=["mid1@astronomic.com", "mid2@astronomic.com"],
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["References"] == "<mid1@astronomic.com> <mid2@astronomic.com>"


# --- Unicode -------------------------------------------------------------------


def test_unicode_subject_round_trips():
    raw = build_mime_message(
        from_email="victoria@astronomic.com", to_email="lead@example.com",
        subject="Héllo wörld — Ünïcödé ✓", body="plain body",
        rfc_message_id="mid@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == "Héllo wörld — Ünïcödé ✓"


def test_unicode_body_round_trips():
    body = "Hi there —\n\nThis has emoji 🎉 and accents café, naïve, Zürich.\n\nBest,\nVictoria"
    raw = build_mime_message(
        from_email="victoria@astronomic.com", to_email="lead@example.com",
        subject="s", body=body, rfc_message_id="mid@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed.get_content().strip() == body.strip()
    assert parsed.get_content_charset() == "utf-8"


# --- Header injection ----------------------------------------------------------


@pytest.mark.parametrize("field", ["from_email", "to_email", "subject"])
def test_crlf_in_a_core_header_field_is_rejected(field):
    kwargs = dict(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
    )
    kwargs[field] = "value\r\nBcc: attacker@evil.com"
    with pytest.raises(HeaderInjectionError):
        build_mime_message(**kwargs)


def test_lf_only_injection_attempt_is_also_rejected():
    with pytest.raises(HeaderInjectionError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com",
            subject="s\nX-Injected: true", body="b", rfc_message_id="mid@astronomic.com",
        )


def test_crlf_in_rfc_message_id_is_rejected():
    """rfc_message_id is now execution-supplied (see this module's
    docstring on the B2 hardening pass), so it must get the same
    header-injection protection as every other externally-influenced
    header value -- not just From/To/Subject."""
    with pytest.raises(HeaderInjectionError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid\r\nBcc: attacker@evil.com@astronomic.com",
        )


def test_crlf_in_in_reply_to_is_rejected():
    with pytest.raises(HeaderInjectionError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid@astronomic.com",
            in_reply_to_message_id="x@y.com\r\nBcc: attacker@evil.com",
        )


def test_crlf_in_a_reference_is_rejected():
    with pytest.raises(HeaderInjectionError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid@astronomic.com",
            references=["ok@astronomic.com", "bad\r\n@evil.com"],
        )


def test_injection_is_rejected_before_any_message_object_is_built():
    """A HeaderInjectionError must be the ONLY thing that happens -- no
    partially-built message, no side effect. Calling twice with the same
    bad input must fail identically both times (pure, no hidden state)."""
    kwargs = dict(
        from_email="a@astronomic.com", to_email="b@example.com",
        subject="bad\r\nheader", body="b", rfc_message_id="mid@astronomic.com",
    )
    with pytest.raises(HeaderInjectionError):
        build_mime_message(**kwargs)
    with pytest.raises(HeaderInjectionError):
        build_mime_message(**kwargs)


# --- encode_gmail_raw -----------------------------------------------------------


# --- build_mime_message: List-Unsubscribe headers (Phase B3) -----------------


def test_no_list_unsubscribe_headers_by_default():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["List-Unsubscribe"] is None
    assert parsed["List-Unsubscribe-Post"] is None


def test_list_unsubscribe_headers_set_together():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
        list_unsubscribe="<https://api.astronomicconnect.com/mail/unsubscribe/one-click?token=abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["List-Unsubscribe"] == "<https://api.astronomicconnect.com/mail/unsubscribe/one-click?token=abc>"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_list_unsubscribe_without_post_is_rejected():
    """RFC 8058: having only List-Unsubscribe without List-Unsubscribe-Post
    does not satisfy one-click -- this function refuses to construct that
    half-compliant shape at all."""
    with pytest.raises(ValueError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid@astronomic.com",
            list_unsubscribe="<https://example.com/one-click?token=abc>",
        )


def test_list_unsubscribe_post_without_list_unsubscribe_is_rejected():
    with pytest.raises(ValueError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid@astronomic.com",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )


def test_crlf_in_list_unsubscribe_is_rejected():
    with pytest.raises(HeaderInjectionError):
        build_mime_message(
            from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
            rfc_message_id="mid@astronomic.com",
            list_unsubscribe="<https://example.com/x>\r\nBcc: attacker@evil.com",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )


# --- build_mime_message: HTML alternative (Phase C/D) ------------------------


def test_html_body_omitted_is_byte_for_byte_the_old_single_part_shape():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed.is_multipart() is False
    assert parsed.get_content_type() == "text/plain"


def test_html_body_given_produces_multipart_alternative_with_plain_then_html():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="Plain version.",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>HTML version.</p>",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed.is_multipart() is True
    assert parsed.get_content_type() == "multipart/alternative"
    parts = list(parsed.iter_parts())
    assert len(parts) == 2
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"


def test_plain_part_is_unchanged_when_html_body_is_also_given():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="Plain version.",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>HTML version.</p>",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    plain_part = next(p for p in parsed.iter_parts() if p.get_content_type() == "text/plain")
    assert plain_part.get_content().strip() == "Plain version."
    assert plain_part.get_content_charset() == "utf-8"


def test_html_part_content_and_charset_match_the_given_html_body():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="Plain version.",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>HTML version.</p>",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    html_part = next(p for p in parsed.iter_parts() if p.get_content_type() == "text/html")
    assert html_part.get_content().strip() == "<p>HTML version.</p>"
    assert html_part.get_content_charset() == "utf-8"


def test_unicode_html_body_round_trips():
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="Plain café.",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>HTML café \U0001f389</p>",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    html_part = next(p for p in parsed.iter_parts() if p.get_content_type() == "text/html")
    assert html_part.get_content().strip() == "<p>HTML café \U0001f389</p>"


def test_html_alternative_coexists_with_list_unsubscribe_headers():
    """The two Phase B3/C features are independent -- adding an HTML part
    must not disturb the top-level List-Unsubscribe/List-Unsubscribe-Post
    headers, which live on the outer multipart message, not on either part."""
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="Plain version.",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>HTML version.</p>",
        list_unsubscribe="<https://api.astronomicconnect.com/mail/unsubscribe/one-click?token=abc>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    assert parsed["List-Unsubscribe"] == "<https://api.astronomicconnect.com/mail/unsubscribe/one-click?token=abc>"
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert parsed.get_content_type() == "multipart/alternative"


def test_html_body_header_injection_style_content_is_not_treated_as_a_header():
    """html_body is body CONTENT, not a header value -- this module's
    HeaderInjectionError checks apply only to the header-family params
    (see _assert_safe_header_value's call sites); a raw newline or a
    string that merely looks like a header inside html_body must pass
    straight through as ordinary part content."""
    raw = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
        html_body="<p>Line one.</p>\n<p>X-Not-A-Real-Header: still just text</p>",
    )
    parsed = _message_from_bytes(raw, policy=policy.default)
    html_part = next(p for p in parsed.iter_parts() if p.get_content_type() == "text/html")
    assert "X-Not-A-Real-Header: still just text" in html_part.get_content()


def test_encode_gmail_raw_uses_urlsafe_alphabet_and_round_trips():
    mime_bytes = build_mime_message(
        from_email="a@astronomic.com", to_email="b@example.com", subject="s", body="b",
        rfc_message_id="mid@astronomic.com",
    )
    encoded = encode_gmail_raw(mime_bytes)
    assert "+" not in encoded and "/" not in encoded
    assert base64.urlsafe_b64decode(encoded.encode("ascii")) == mime_bytes
