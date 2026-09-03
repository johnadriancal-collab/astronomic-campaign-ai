"""
Deterministic RFC-compliant MIME construction for Astronomic Mail Phase B2
(Gmail Sender Foundation), extended in Phase C/D for an optional HTML
alternative part (see `html_body` below) -- built entirely on Python's
standard-library `email` package (via the modern `email.message.EmailMessage`
API + default policy) -- no third-party MIME library, matching this app's
existing "small, auditable surface" philosophy (see app/google/oauth_client.py's
module docstring).

`html_body` is OPTIONAL and OFF by default: when omitted, this function
builds the exact same single-part `text/plain` message it always has --
zero behavior change for any existing caller. When given, it uses
`EmailMessage.add_alternative()` (confirmed empirically below) to build a
real RFC 2046 `multipart/alternative` message: `text/plain` first (the
existing `body`, unchanged), `text/html` second (the RECOMMENDED
least-to-most-preferred order) -- an HTML-capable client renders the HTML
part; a plain-text-only client falls back to the exact same text it
always got. This module has no opinion on WHAT the HTML looks like or
whether it's safe (e.g. properly escaped) -- that responsibility belongs
entirely to the caller (see app/services/mail_unsubscribe_composition.py's
build_html_body(), the one intended source of this value); this module
only assembles whatever string it's given into a `text/html` MIME part.

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
- `msg.set_content(body)` followed by `msg.add_alternative(html_body,
  subtype="html")` produces a top-level `Content-Type: multipart/alternative`
  message containing exactly two parts, `text/plain` then `text/html`, each
  with its own correctly-chosen Content-Transfer-Encoding -- confirmed
  directly against this exact stdlib version, not assumed from docs.
"""

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

_FORBIDDEN_HEADER_CHARS = ("\r", "\n")


class HeaderInjectionError(ValueError):
    """Raised when a value destined for a MIME header (From/To/Subject/
    Message-ID/In-Reply-To/References/List-Unsubscribe/
    List-Unsubscribe-Post) contains a raw CR or LF -- the
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
    list_unsubscribe: str | None = None,
    list_unsubscribe_post: str | None = None,
    html_body: str | None = None,
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

    `list_unsubscribe` (RFC 8058/RFC 2369, e.g. "<https://.../one-click?token=...>",
    ALREADY bracketed -- this function does not add brackets itself, unlike
    the Message-ID-family headers above, since RFC 2369 allows multiple
    comma-separated URIs inside one header value and this function has no
    business assuming there's exactly one) and `list_unsubscribe_post`
    (RFC 8058's fixed literal, "List-Unsubscribe=One-Click") are set
    together or not at all -- see this module's own tests for why B2's
    "one without the other" pattern doesn't apply here: unlike In-Reply-To/
    thread_id (two independent pieces of context), these two headers are a
    single RFC-defined pair with no meaningful partial state. Neither is
    generated here -- see app/services/mail_unsubscribe_composition.py for
    the (currently unwired -- see that module's own safety note) caller
    that builds these values from a real per-recipient token.

    `html_body`, when given, adds a `text/html` alternative part alongside
    the existing plain-text `body` -- see this module's own docstring for
    the exact resulting MIME structure. Omitted (the default): behavior is
    byte-for-byte identical to before this parameter existed. This function
    does not escape, sanitize, or otherwise inspect `html_body`'s content --
    it is the CALLER's responsibility to ensure it's safe HTML (see
    app/services/mail_unsubscribe_composition.py's build_html_body()).

    Raises HeaderInjectionError before constructing anything if
    from_email/to_email/subject/rfc_message_id/in_reply_to_message_id/any
    reference/list_unsubscribe/list_unsubscribe_post contains a raw CR or
    LF. Raises ValueError if exactly one of list_unsubscribe/
    list_unsubscribe_post is given without the other.
    """
    if (list_unsubscribe is None) != (list_unsubscribe_post is None):
        raise ValueError("list_unsubscribe and list_unsubscribe_post must be given together or not at all.")

    for value, name in ((from_email, "From"), (to_email, "To"), (subject, "Subject"), (rfc_message_id, "Message-ID")):
        _assert_safe_header_value(value, name)
    if in_reply_to_message_id is not None:
        _assert_safe_header_value(in_reply_to_message_id, "In-Reply-To")
    for ref in references or ():
        _assert_safe_header_value(ref, "References")
    if list_unsubscribe is not None:
        _assert_safe_header_value(list_unsubscribe, "List-Unsubscribe")
        _assert_safe_header_value(list_unsubscribe_post, "List-Unsubscribe-Post")

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
    if list_unsubscribe is not None:
        msg["List-Unsubscribe"] = list_unsubscribe
        msg["List-Unsubscribe-Post"] = list_unsubscribe_post
    # Adds Content-Type (text/plain; charset="utf-8"), a correctly chosen
    # Content-Transfer-Encoding, and MIME-Version -- all automatic, see
    # this module's docstring.
    msg.set_content(body)
    if html_body is not None:
        # Restructures the message into multipart/alternative with this
        # new part appended AFTER the plain-text part above -- text/plain
        # first, text/html second, matching RFC 2046's recommended
        # least-to-most-preferred ordering. See this module's own
        # docstring for the exact confirmed structure.
        msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


def encode_gmail_raw(mime_bytes: bytes) -> str:
    """Gmail's `users.messages.send` `raw` field: the full MIME message,
    base64url encoded (RFC 4648 sec. 5 -- `-`/`_` alphabet, matching
    exactly what Gmail's API documentation specifies; NOT the standard
    `+`/`/` base64 alphabet). Padding (`=`) is left in place -- Gmail's API
    accepts padded base64url; this module does not strip it, to avoid
    depending on undocumented leniency."""
    return base64.urlsafe_b64encode(mime_bytes).decode("ascii")
