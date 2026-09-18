"""
Bounce detection (2026-09-18) -- pure, dependency-free RFC 3464 delivery-
status-notification parsing. Zero network calls, zero store access; this
module only ever transforms a Gmail `format=full` message dict (from
GmailMessageBodyClient.get_message_full()) into structured data, or
decides None ("not structurally a DSN") -- MailBounceDetectionService is
the one caller, and the one place any of this output gets persisted or
acted on.

STRUCTURAL EVIDENCE ONLY, per the approved design: a message is treated
as a real DSN candidate only once BOTH of these hold:
  1. `is_likely_dsn()` -- a CHEAP header check (Content-Type contains
     `multipart/report` and `report-type=delivery-status`) -- the
     pre-filter MailBounceDetectionService runs on light metadata alone,
     BEFORE ever fetching full MIME.
  2. `parse_dsn()` -- after a full-MIME fetch, a REAL
     `message/delivery-status` MIME part is actually found and parses
     into at least one recipient block.
From/Subject text (MAILER-DAEMON, "Delivery Status Notification", etc.)
is NEVER checked by this module at all -- not even as a supporting
signal for `is_likely_dsn()` -- because the one thing genuinely
diagnostic of an RFC 3464 report is its Content-Type/MIME structure, and
adding a second, weaker heuristic path would only create a way for a
non-DSN message to be misclassified. A message whose Content-Type looks
right but has no real delivery-status part (e.g. a hand-crafted or
malformed message) correctly returns None from parse_dsn() -- never a
fabricated recipient/status.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from email import message_from_string
from email.message import Message

from app.models.mail import MailBounceType


@dataclass(frozen=True)
class ParsedDsnRecipient:
    """One RFC 3464 per-recipient status block. Every field is the raw,
    verbatim value Gmail/the reporting MTA supplied (Final-Recipient/
    Original-Recipient with their `type;` prefix already stripped,
    everything else untouched) -- never reformatted, never guessed when
    absent."""

    final_recipient: str | None
    original_recipient: str | None
    action: str | None
    status: str | None
    diagnostic_code: str | None


@dataclass(frozen=True)
class ParsedDsn:
    recipients: list[ParsedDsnRecipient]
    # The ORIGINAL outbound message's own Message-ID, angle brackets
    # already stripped (see _strip_message_id_brackets()) so it can be
    # compared directly against MailEnrollmentStep.rfc_message_id
    # (stored WITHOUT brackets -- see generate_rfc_message_id()'s own
    # docstring). None if genuinely absent from every place this parser
    # looks for it -- attribution then fails, never falls back to a
    # guess (see MailBounceDetectionService's own attribution docstring).
    original_message_id: str | None


def is_likely_dsn(headers: dict[str, str | None]) -> bool:
    """The CHEAP pre-filter, run on light From/Subject/Content-Type
    metadata alone -- never on a full-MIME fetch. Deliberately checks
    ONLY Content-Type; see this module's own docstring for why From/
    Subject are never even supporting evidence here."""
    content_type = (headers.get("Content-Type") or "").lower()
    return "multipart/report" in content_type and "report-type=delivery-status" in content_type


def _decode_base64url(data: str) -> str:
    """Same decode discipline as GmailMessageBodyClient's own private
    helper (gmail_message_body_client.py) -- a malformed/truncated part
    must never crash bounce processing; degrade to an empty string."""
    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _strip_message_id_brackets(value: str) -> str:
    """'<abc@domain>' -> 'abc@domain' -- MailEnrollmentStep.rfc_message_id
    is stored WITHOUT angle brackets (see generate_rfc_message_id()'s own
    docstring), but any Message-ID a mail system hands back (a
    transmitted MIME header, or an RFC 3464 msg-id field) always carries
    them."""
    return value.strip().lstrip("<").rstrip(">")


def _strip_type_prefix(value: str) -> str:
    """RFC 3464's `Final-Recipient`/`Original-Recipient` fields are
    `address-type; address` (almost always `rfc822; user@example.com`)
    -- this returns just the address. `Status`/`Action` have no such
    prefix and pass through unchanged; `Diagnostic-Code` is deliberately
    NEVER passed through this function (its `diagnostic-type;` prefix is
    kept, verbatim, as part of the persisted diagnostic string -- see
    MailBounce.diagnostic_code's own docstring: "for later inspection")."""
    if ";" in value:
        return value.split(";", 1)[1].strip()
    return value.strip()


def _find_mime_parts(payload: dict) -> tuple[str | None, str | None]:
    """Walks a Gmail `format=full` message's MIME tree (payload,
    possibly recursively nested `parts`) looking for the ONE
    `message/delivery-status` part and the ONE original-headers part
    (`message/rfc822-headers`, the common case, or a full `message/
    rfc822` attachment some MTAs use instead) -- returns their decoded
    text, or None for either that's genuinely absent. Same recursive-
    walk shape as GmailMessageBodyClient.extract_best_body(), applied to
    a different pair of MIME types."""
    delivery_status_text: str | None = None
    original_headers_text: str | None = None

    def walk(node: dict) -> None:
        nonlocal delivery_status_text, original_headers_text
        mime_type = (node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        data = body.get("data")
        if data and mime_type == "message/delivery-status" and delivery_status_text is None:
            delivery_status_text = _decode_base64url(data)
        elif data and mime_type in ("message/rfc822-headers", "message/rfc822") and original_headers_text is None:
            original_headers_text = _decode_base64url(data)
        for child in node.get("parts") or []:
            walk(child)

    walk(payload or {})
    return delivery_status_text, original_headers_text


def _parse_delivery_status_text(text: str) -> tuple[str | None, list[ParsedDsnRecipient]]:
    """RFC 3464's `message/delivery-status` part is itself a sequence of
    header-shaped blocks separated by blank lines: ONE per-message block
    first (Reporting-MTA, Arrival-Date, and SOMETIMES Original-Message-ID/
    Original-Envelope-Id), followed by one-or-more per-RECIPIENT blocks
    (Final-Recipient/Original-Recipient/Action/Status/Diagnostic-Code).
    Each block genuinely IS a valid header block on its own, so Python's
    own email parser (same "use the email package, don't hand-roll
    header parsing" convention as gmail_mime.py/mail_reply_detection_
    service.py) parses each one directly."""
    blocks = re.split(r"\n\s*\n", text.strip())
    if not blocks:
        return None, []

    message_block: Message = message_from_string(blocks[0])
    message_level_id = message_block.get("Original-Message-ID")

    recipients: list[ParsedDsnRecipient] = []
    for block_text in blocks[1:]:
        if not block_text.strip():
            continue
        block: Message = message_from_string(block_text)
        final_recipient = block.get("Final-Recipient")
        original_recipient = block.get("Original-Recipient")
        action = block.get("Action")
        status = block.get("Status")
        diagnostic_code = block.get("Diagnostic-Code")
        # A block with none of these is not a real recipient block (e.g.
        # a stray blank chunk) -- skip rather than emit an all-None row.
        if final_recipient is None and original_recipient is None and status is None:
            continue
        recipients.append(
            ParsedDsnRecipient(
                final_recipient=_strip_type_prefix(final_recipient) if final_recipient else None,
                original_recipient=_strip_type_prefix(original_recipient) if original_recipient else None,
                action=action.strip() if action else None,
                status=status.strip() if status else None,
                diagnostic_code=diagnostic_code.strip() if diagnostic_code else None,
            )
        )

    return (_strip_message_id_brackets(message_level_id) if message_level_id else None), recipients


def parse_dsn(full_message: dict) -> ParsedDsn | None:
    """The real, post-full-fetch structural confirmation. Returns None
    if no `message/delivery-status` part is actually present (the cheap
    Content-Type pre-filter was a false positive) OR if that part
    parses to zero real recipient blocks -- never a fabricated,
    zero-recipient ParsedDsn.

    `original_message_id` prefers the per-message `Original-Message-ID`
    field (rare but authoritative when present) and falls back to the
    attached original message's own `Message-ID` header (the common
    case -- most DSNs, including Gmail's own, attach the original
    headers as a `message/rfc822-headers` part) -- never falls back
    further than that; if neither is present, attribution simply has
    nothing to match against and MailBounceDetectionService will not
    persist this candidate at all."""
    payload = full_message.get("payload") or {}
    delivery_status_text, original_headers_text = _find_mime_parts(payload)
    if delivery_status_text is None:
        return None

    message_level_id, recipients = _parse_delivery_status_text(delivery_status_text)
    if not recipients:
        return None

    original_message_id = message_level_id
    if original_message_id is None and original_headers_text:
        original_headers: Message = message_from_string(original_headers_text)
        header_message_id = original_headers.get("Message-ID")
        if header_message_id:
            original_message_id = _strip_message_id_brackets(header_message_id)

    return ParsedDsn(recipients=recipients, original_message_id=original_message_id)


def classify_bounce_type(status: str | None) -> MailBounceType:
    """RFC 3464's `Status` field is `class.subject.detail` (e.g.
    "5.1.1") -- classification is the leading digit ONLY, per the
    approved design: 5 -> HARD (permanent), 4 -> SOFT (temporary),
    anything else (missing, malformed, or a class this codebase doesn't
    define a bucket for) -> UNKNOWN. Never inferred from Diagnostic-Code
    prose or any other field -- Status is the one structured signal RFC
    3464 defines specifically for this."""
    if not status:
        return MailBounceType.UNKNOWN
    leading_digit = status.strip().split(".", 1)[0]
    if leading_digit == "5":
        return MailBounceType.HARD
    if leading_digit == "4":
        return MailBounceType.SOFT
    return MailBounceType.UNKNOWN


__all__ = [
    "ParsedDsn",
    "ParsedDsnRecipient",
    "is_likely_dsn",
    "parse_dsn",
    "classify_bounce_type",
]
