"""
Verification for Luma's own documented webhook signature scheme (Svix-style
HMAC) -- https://help.luma.com/p/webhooks. NOT an invented scheme: Luma
signs every delivery with a per-webhook secret (`whsec_...`, returned once
when the webhook is created) and sends a `Webhook-Signature` header shaped
`t=<unix_ts>,v1=<hex_hmac>`, where
`hex_hmac = HMAC-SHA256(secret, f"{t}.{raw_body}").hexdigest()`.

Deliberately pure (no FastAPI/Request dependency here) so every failure
mode -- missing header, malformed header, stale timestamp, wrong signature
-- is directly unit-testable without spinning up a TestClient. The FastAPI
dependency (app/dependencies.py) wraps this and maps every
LumaWebhookSignatureError to the same 401, never distinguishing which case
fired -- that distinction is an oracle a forged-signature attacker could
use to iterate toward a valid one, so it never crosses the API boundary.
"""

import hashlib
import hmac
from datetime import datetime, timezone

# Luma's own replay-protection guidance ("reject if t is more than a few
# minutes old" -- help.luma.com/p/webhooks). 5 minutes comfortably covers
# Luma's documented retry window (up to 3 retries, 1m -> 2m -> 4m backoff)
# while still rejecting a stale/replayed delivery.
MAX_SIGNATURE_AGE_SECONDS = 300


class LumaWebhookSignatureError(Exception):
    """Any invalid/missing/malformed/stale webhook signature -- always
    mapped to a 401 at the API boundary, regardless of which check below
    actually failed."""


def verify_luma_webhook_signature(
    secret: str, raw_body: bytes, signature_header: str | None, now: datetime
) -> None:
    """Raises LumaWebhookSignatureError on any failure; returns None (does
    nothing) on success. `now` is passed in (never computed internally)
    so staleness checks are deterministic and testable."""
    if not signature_header:
        raise LumaWebhookSignatureError("Missing Webhook-Signature header.")

    parsed = _parse_signature_header(signature_header)
    if parsed is None:
        raise LumaWebhookSignatureError("Malformed Webhook-Signature header.")
    timestamp_str, provided_signature = parsed

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        raise LumaWebhookSignatureError("Malformed Webhook-Signature timestamp.")

    delivery_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    age_seconds = abs((now - delivery_time).total_seconds())
    if age_seconds > MAX_SIGNATURE_AGE_SECONDS:
        raise LumaWebhookSignatureError("Webhook signature timestamp is stale.")

    signed_payload = f"{timestamp_str}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected_signature = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_signature, provided_signature):
        raise LumaWebhookSignatureError("Webhook signature does not match.")


def _parse_signature_header(header: str) -> tuple[str, str] | None:
    """Parses "t=169000000,v1=abcdef..." -> ("169000000", "abcdef..."). None
    if either the timestamp or signature component is missing."""
    parts: dict[str, str] = {}
    for segment in header.split(","):
        if "=" not in segment:
            continue
        key, _, value = segment.partition("=")
        parts[key.strip()] = value.strip()
    if "t" not in parts or "v1" not in parts or not parts["t"] or not parts["v1"]:
        return None
    return parts["t"], parts["v1"]
