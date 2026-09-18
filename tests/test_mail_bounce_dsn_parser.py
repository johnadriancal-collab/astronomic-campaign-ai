"""
Bounce detection (2026-09-18) -- pure DSN parsing. Real RFC 3464 shapes
(a text/plain human-readable part + message/delivery-status +
message/rfc822-headers, the standard Gmail-generated bounce structure),
constructed as base64url-encoded Gmail `format=full` fixtures -- no
network, no store, matching this module's own "pure function" scope.
"""

import base64

from app.models.mail import MailBounceType
from app.services.mail_bounce_dsn_parser import classify_bounce_type, is_likely_dsn, parse_dsn


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _leaf(mime_type: str, text: str) -> dict:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


DELIVERY_STATUS_TEXT = (
    "Reporting-MTA: dns; mx.google.com\n"
    "Arrival-Date: Thu, 18 Sep 2026 12:00:00 -0700\n"
    "\n"
    "Final-Recipient: rfc822; nobody@example.com\n"
    "Original-Recipient: rfc822; nobody@example.com\n"
    "Action: failed\n"
    "Status: 5.1.1\n"
    "Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.\n"
)

ORIGINAL_HEADERS_TEXT = (
    "From: victoria@useastronomic.com\n"
    "To: nobody@example.com\n"
    "Subject: Quick hello\n"
    "Message-ID: <abc123def456@useastronomic.com>\n"
)


def make_full_dsn_message(delivery_status_text: str = DELIVERY_STATUS_TEXT, original_headers_text: str | None = ORIGINAL_HEADERS_TEXT) -> dict:
    parts = [
        _leaf("text/plain", "Delivery has failed to these recipients or groups."),
        _leaf("message/delivery-status", delivery_status_text),
    ]
    if original_headers_text is not None:
        parts.append(_leaf("message/rfc822-headers", original_headers_text))
    return {
        "id": "dsn-msg-1",
        "threadId": "thr-dsn-1",
        "payload": {
            "mimeType": "multipart/report",
            "headers": [{"name": "Content-Type", "value": 'multipart/report; report-type=delivery-status; boundary="x"'}],
            "parts": parts,
        },
    }


def make_full_ordinary_message(body_text: str = "Hi, thanks for reaching out!") -> dict:
    return {
        "id": "ordinary-msg-1",
        "threadId": "thr-1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Content-Type", "value": "text/plain"}],
            "body": {"data": _b64(body_text)},
        },
    }


# --- is_likely_dsn (cheap pre-filter) ---------------------------------------


def test_is_likely_dsn_true_for_real_delivery_status_content_type():
    headers = {"Content-Type": 'multipart/report; report-type=delivery-status; boundary="x"'}
    assert is_likely_dsn(headers) is True


def test_is_likely_dsn_false_for_ordinary_email():
    headers = {"Content-Type": "text/plain; charset=UTF-8"}
    assert is_likely_dsn(headers) is False


def test_is_likely_dsn_false_for_mailer_daemon_subject_alone():
    """MAILER-DAEMON in From/Subject is NEVER sufficient by itself --
    this function doesn't even look at those fields."""
    headers = {
        "From": "MAILER-DAEMON@mx.google.com",
        "Subject": "Delivery Status Notification (Failure)",
        "Content-Type": "text/plain; charset=UTF-8",
    }
    assert is_likely_dsn(headers) is False


def test_is_likely_dsn_false_for_multipart_report_without_delivery_status_type():
    """A different report-type (e.g. disposition-notification, a read
    receipt) must not be treated as a bounce."""
    headers = {"Content-Type": 'multipart/report; report-type=disposition-notification; boundary="x"'}
    assert is_likely_dsn(headers) is False


# --- parse_dsn (real structural confirmation) -------------------------------


def test_parse_dsn_recognizes_a_real_delivery_status_report():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed is not None
    assert len(parsed.recipients) == 1


def test_parse_dsn_returns_none_for_an_ordinary_message():
    assert parse_dsn(make_full_ordinary_message()) is None


def test_parse_dsn_returns_none_when_content_type_looked_right_but_no_real_delivery_status_part_exists():
    """A false positive from the cheap pre-filter -- e.g. a hand-crafted
    message whose Content-Type header claims multipart/report but has
    no actual message/delivery-status part."""
    message = {
        "id": "fake-1",
        "threadId": "thr-1",
        "payload": {
            "mimeType": "multipart/report",
            "parts": [_leaf("text/plain", "Not a real DSN.")],
        },
    }
    assert parse_dsn(message) is None


def test_parse_dsn_final_recipient_parsed():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.recipients[0].final_recipient == "nobody@example.com"


def test_parse_dsn_original_recipient_parsed():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.recipients[0].original_recipient == "nobody@example.com"


def test_parse_dsn_action_parsed():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.recipients[0].action == "failed"


def test_parse_dsn_status_parsed():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.recipients[0].status == "5.1.1"


def test_parse_dsn_diagnostic_code_parsed_verbatim_with_type_prefix_kept():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.recipients[0].diagnostic_code == "smtp; 550 5.1.1 The email account that you tried to reach does not exist."


def test_parse_dsn_original_message_id_parsed_from_attached_original_headers():
    parsed = parse_dsn(make_full_dsn_message())
    assert parsed.original_message_id == "abc123def456@useastronomic.com"


def test_parse_dsn_original_message_id_prefers_message_level_field_when_present():
    delivery_status_with_original_id = DELIVERY_STATUS_TEXT.replace(
        "Reporting-MTA: dns; mx.google.com\n",
        "Reporting-MTA: dns; mx.google.com\nOriginal-Message-ID: <from-status-part@useastronomic.com>\n",
    )
    parsed = parse_dsn(make_full_dsn_message(delivery_status_text=delivery_status_with_original_id))
    assert parsed.original_message_id == "from-status-part@useastronomic.com"


def test_parse_dsn_original_message_id_is_none_when_neither_source_has_it():
    parsed = parse_dsn(make_full_dsn_message(original_headers_text=None))
    assert parsed.original_message_id is None


def test_parse_dsn_handles_multiple_recipient_blocks():
    two_recipients = DELIVERY_STATUS_TEXT + (
        "\nFinal-Recipient: rfc822; other@example.com\n"
        "Original-Recipient: rfc822; other@example.com\n"
        "Action: failed\n"
        "Status: 4.2.2\n"
        "Diagnostic-Code: smtp; 452 4.2.2 mailbox full\n"
    )
    parsed = parse_dsn(make_full_dsn_message(delivery_status_text=two_recipients))
    assert len(parsed.recipients) == 2
    assert parsed.recipients[1].status == "4.2.2"


# --- classify_bounce_type ---------------------------------------------------


def test_classify_5xx_as_hard():
    assert classify_bounce_type("5.1.1") == MailBounceType.HARD


def test_classify_4xx_as_soft():
    assert classify_bounce_type("4.2.2") == MailBounceType.SOFT


def test_classify_missing_status_as_unknown():
    assert classify_bounce_type(None) == MailBounceType.UNKNOWN


def test_classify_ambiguous_status_as_unknown():
    assert classify_bounce_type("2.1.1") == MailBounceType.UNKNOWN


def test_classify_malformed_status_as_unknown_never_guessed():
    assert classify_bounce_type("not-a-real-status") == MailBounceType.UNKNOWN
