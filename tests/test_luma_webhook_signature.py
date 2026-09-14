"""
Pure verification of Luma's own documented Svix-style webhook signature
scheme -- no FastAPI/HTTP involved, so every failure mode is directly
testable.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest

from app.luma.webhook_signature import (
    MAX_SIGNATURE_AGE_SECONDS,
    LumaWebhookSignatureError,
    verify_luma_webhook_signature,
    verify_luma_webhook_signature_with_fallback,
)

SECRET = "whsec_test_secret_value"


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    signed_payload = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_valid_signature_passes():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign(SECRET, timestamp, body)

    verify_luma_webhook_signature(SECRET, body, header, now)  # must not raise


def test_invalid_signature_is_rejected():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = f"t={timestamp},v1=" + "0" * 64  # well-formed but wrong digest

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, body, header, now)


def test_signature_computed_with_wrong_secret_is_rejected():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign("a-completely-different-secret", timestamp, body)

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, body, header, now)


def test_missing_signature_header_is_rejected():
    now = datetime.now(timezone.utc)
    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, b"{}", None, now)


def test_empty_signature_header_is_rejected():
    now = datetime.now(timezone.utc)
    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, b"{}", "", now)


@pytest.mark.parametrize(
    "header",
    [
        "not-even-key-value-pairs",
        "t=12345",  # missing v1
        "v1=abcdef",  # missing t
        "t=,v1=",  # both present but empty
    ],
)
def test_malformed_signature_header_is_rejected(header):
    now = datetime.now(timezone.utc)
    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, b"{}", header, now)


def test_non_integer_timestamp_is_rejected():
    now = datetime.now(timezone.utc)
    header = "t=not-a-number,v1=abcdef"
    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, b"{}", header, now)


def test_stale_timestamp_is_rejected():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    stale_time = now - timedelta(seconds=MAX_SIGNATURE_AGE_SECONDS + 60)
    timestamp = int(stale_time.timestamp())
    header = _sign(SECRET, timestamp, body)  # correctly signed, just too old

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, body, header, now)


def test_timestamp_just_under_the_staleness_limit_is_accepted():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    recent_time = now - timedelta(seconds=MAX_SIGNATURE_AGE_SECONDS - 5)
    timestamp = int(recent_time.timestamp())
    header = _sign(SECRET, timestamp, body)

    verify_luma_webhook_signature(SECRET, body, header, now)  # must not raise


def test_future_timestamp_beyond_tolerance_is_rejected():
    """Defends against a forged delivery claiming a future timestamp to
    dodge staleness checks -- age is computed as an absolute difference,
    so a far-future `t` is rejected just like a far-past one."""
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    future_time = now + timedelta(seconds=MAX_SIGNATURE_AGE_SECONDS + 60)
    timestamp = int(future_time.timestamp())
    header = _sign(SECRET, timestamp, body)

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, body, header, now)


def test_signature_is_body_specific():
    """A signature valid for one body must not validate a different body
    (protects against a tampered-in-transit payload)."""
    now = datetime.now(timezone.utc)
    timestamp = int(now.timestamp())
    original_body = b'{"type": "guest.registered", "data": {"id": "gst-1"}}'
    tampered_body = b'{"type": "guest.registered", "data": {"id": "gst-2"}}'
    header = _sign(SECRET, timestamp, original_body)

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature(SECRET, tampered_body, header, now)


# --- Stage 6A Capture: verify_luma_webhook_signature_with_fallback ---------
# (2026-09-14) -- LUMA_ADDITIONAL_WEBHOOK_SECRETS support. Permanent,
# general-purpose multi-webhook capability -- NOT part of the temporary
# capture mechanism itself (that lives in test_luma_calendar_event_capture.py).


ADDITIONAL_SECRET = "whsec_second_webhook_secret"


def test_fallback_with_no_additional_secrets_behaves_exactly_like_the_single_secret_check():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign(SECRET, timestamp, body)

    verify_luma_webhook_signature_with_fallback(SECRET, [], body, header, now)  # must not raise


def test_fallback_rejects_when_no_additional_secrets_and_primary_is_wrong():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign("wrong-secret", timestamp, body)

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature_with_fallback(SECRET, [], body, header, now)


def test_fallback_verifies_primary_secret_without_even_trying_additional():
    now = datetime.now(timezone.utc)
    body = b'{"type": "guest.registered", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign(SECRET, timestamp, body)  # signed with the PRIMARY secret

    verify_luma_webhook_signature_with_fallback(SECRET, [ADDITIONAL_SECRET], body, header, now)  # must not raise


def test_fallback_verifies_via_an_additional_secret_when_primary_does_not_match():
    now = datetime.now(timezone.utc)
    body = b'{"type": "calendar.person.subscribed", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign(ADDITIONAL_SECRET, timestamp, body)  # signed with the ADDITIONAL secret, not primary

    verify_luma_webhook_signature_with_fallback(SECRET, [ADDITIONAL_SECRET], body, header, now)  # must not raise


def test_fallback_tries_every_configured_additional_secret_in_order():
    now = datetime.now(timezone.utc)
    body = b'{"type": "calendar.person.subscribed", "data": {}}'
    timestamp = int(now.timestamp())
    third_secret = "whsec_third_secret"
    header = _sign(third_secret, timestamp, body)  # matches only the LAST configured additional secret

    verify_luma_webhook_signature_with_fallback(SECRET, [ADDITIONAL_SECRET, third_secret], body, header, now)  # must not raise


def test_fallback_rejects_when_primary_and_all_additional_secrets_are_wrong():
    now = datetime.now(timezone.utc)
    body = b'{"type": "calendar.person.subscribed", "data": {}}'
    timestamp = int(now.timestamp())
    header = _sign("some-completely-unrelated-secret", timestamp, body)

    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature_with_fallback(SECRET, [ADDITIONAL_SECRET, "whsec_third_secret"], body, header, now)


def test_fallback_with_missing_signature_header_still_rejects_cleanly():
    now = datetime.now(timezone.utc)
    with pytest.raises(LumaWebhookSignatureError):
        verify_luma_webhook_signature_with_fallback(SECRET, [ADDITIONAL_SECRET], b"{}", None, now)
