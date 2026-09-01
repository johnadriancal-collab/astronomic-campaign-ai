"""
Deterministic RFC-compliant MIME construction for Astronomic Mail Phase B2
(Gmail Sender Foundation) -- plain-text only, matching the current
MailSequenceStep/MailEnrollmentStep content model (see app/models/mail.py;
there is no HTML/rich-text body anywhere in this codebase). Built entirely
on Python's standard-library `email` package (via the modern
`email.message.EmailMessage` API + default policy) -- no third-party MIME
library, matching this app's existing "small, auditable surface" philosophy
(see app/google/oauth_client.py's module docstring).

Nothing in this module makes a network call or knows about Gmail's API
shape (that's app/google/gmail_api_client.py) or Mailbox/OAuth state (that's
app/google/gmail_sender.py) -- purely a pure-function MIME builder,
independently testable with zero mocking.

UPDATED for the B2 hardening pass: RFC Message-ID GENERATION moved out of
this module entirely -- it now lives in app/services/rfc_message_id.py,
owned by the execution layer (Phase A), not this provider adapter (see
MailSendRequest's docstring in app/services/mail_sending_service.py for
the full rationale). This module still BUILDS a MIME message around
whatever `rfc_message_id` its caller supplies -- it never invents one.

CONFIRMED empirically (not assumed) against this exact stdlib version:
- EmailMessage's default policy already rejects a header value containing
  a raw CR or LF with a ValueError at assignment time -- this module adds
  its OWN explicit pre-check (HeaderInjectionError) anyway, so callers get
  a stable, purpose-named exception type rather than depending on stdlib
  internals that could change wording/behavior across Python versions.
- `EmailMessage.set_content()` automatically adds `MIME-Version: 1.0`,
  `Content-Type: text/plain; charset="utf-8"`, and an appropriate
  `Content-Transfer-Encoding` (7bit/8bit/quoted-printable/base64, chosen by
  the body's actual byte content) -- none of these are set manually here.
- A non-ASCII Subject/From/To header is automatically RFC 2047
  encoded-word encoded by the default policy's generator; a non-ASCII body
  is correctly UTF-8/quoted-printable-or-base64 encoded by set_content().
  Both were verified directly, not assumed from documentation.
"""

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

_FORBIDDEN_HEADER_CHARS = ("\r", "\n")


class HeaderInjectionError(ValueError):
    """Raised when a value destined for a MIME header (From/To/Subject/
    Message-ID/In-Reply-To/References) contains a raw CR or LF -- the
    classic email-header-injection vector (smuggling an extra header, or
    an extra recipient, through a value this codebase otherwise treats as
    plain text -- e.g. a CRM contact's name, or a campaign's subject
    line). Rejected outright, never stripped/escaped -- a caller upstream
    should see a loud, unambiguous failure rather than a silently
    mutated message."""


def _assert_safe_header_value(value: str, header_name: str) -> None:
    if any(ch in value for ch in _FORBIDDEN_HEADER_CHARS):
        raise HeaderInjectionError(f"{header_name} contains a disallowed line break.")


def build_mime_message(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    rfc_message_id: str,
    date: datetime | None = None,
    in_reply_to_message_id: str | None = None,
    references: list[str] | None = None,
) -> bytes:
    """Builds a complete, RFC 5322 / RFC 2045-2047 compliant plain-text
    MIME message as raw bytes, ready for base64url encoding (see
    encode_gmail_raw()) and Gmail's `users.messages.send` `raw` field.

    `rfc_message_id`/`in_reply_to_message_id`/each entry of `references`
    are passed WITHOUT angle brackets (matching app/services/
    rfc_message_id.py's generate_rfc_message_id() bare-id convention, and
    MailEnrollmentStep.rfc_message_id's existing stored format -- see
    app/models/mail.py) -- angle brackets are added here, at the one place
    they belong: the actual header value. This function does NOT generate
    `rfc_message_id` itself -- it must be supplied, already decided, by
    the caller (see this module's docstring).

    `references`, when given, is rendered as a single space-separated
    References header (oldest-to-newest, in whatever order the caller
    passes) -- this module does not reorder or deduplicate; that is the
    caller's responsibility once real threading history is being tracked
    (Phase C, not built here -- see this module's docstring).

    Raises HeaderInjectionError before constructing anything if
    from_email/to_email/subject/rfc_message_id/in_reply_to_message_id/any
    reference contains a raw CR or LF.
    """
    for value, name in ((from_email, "From"), (to_email, "To"), (subject, "Subject"), (rfc_message_id, "Message-ID")):
        _assert_safe_header_value(value, name)
    if in_reply_to_message_id is not None:
        _assert_safe_header_value(in_reply_to_message_id, "In-Reply-To")
    for ref in references or ():
        _assert_safe_header_value(ref, "References")

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date or datetime.now(timezone.utc))
    msg["Message-ID"] = f"<{rfc_message_id}>"
    if in_reply_to_message_id is not None:
        msg["In-Reply-To"] = f"<{in_reply_to_message_id}>"
    if references:
        msg["References"] = " ".join(f"<{ref}>" for ref in references)
    # Adds Content-Type (text/plain; charset="utf-8"), a correctly chosen
    # Content-Transfer-Encoding, and MIME-Version -- all automatic, see
    # this module's docstring.
    msg.set_content(body)
    return msg.as_bytes()


def encode_gmail_raw(mime_bytes: bytes) -> str:
    """Gmail's `users.messages.send` `raw` field: the full MIME message,
    base64url encoded (RFC 4648 sec. 5 -- `-`/`_` alphabet, matching
    exactly what Gmail's API documentation specifies; NOT the standard
    `+`/`/` base64 alphabet). Padding (`=`) is left in place -- Gmail's API
    accepts padded base64url; this module does not strip it, to avoid
    depending on undocumented leniency."""
    return base64.urlsafe_b64encode(mime_bytes).decode("ascii")
