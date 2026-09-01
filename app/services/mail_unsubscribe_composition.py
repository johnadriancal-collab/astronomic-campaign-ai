"""
Reusable outbound-email composition pieces for Astronomic Mail, built
dormant in Phase B3 and wired into the execution layer in Phase C (see
MailSendingService.prepare_and_send_step(), the ONE intended caller of
compose_outbound_email() -- see this module's own safety note at the
bottom, and tests/test_mail_unsubscribe_safety.py's static checks, which
now enforce a NARROWER guarantee: this module stays execution-layer-
owned, never imported by the Gmail-specific adapter (app/google/
gmail_sender.py, app/google/gmail_api_client.py) directly. Being called
from the execution layer does not, by itself, enable a real send --
prepare_and_send_step() is itself gated by mail_sending_engine_enabled,
the controlled-test allowlists, and the worker lease (see
app/services/mail_execution_worker.py's own module docstring).

Produces the plain-text unsubscribe footer and the RFC 8058
List-Unsubscribe/List-Unsubscribe-Post header values a real outbound
message would need, from a MailEnrollmentStep's already-snapshotted body
-- WITHOUT ever mutating that snapshot (see MailEnrollmentStep's own
"content snapshot" docstring in app/models/mail.py: the execution
record's body must remain exactly what was actually queued to send,
understandable on its own). The footer is appended only to the STRING
this module returns; nothing here ever calls a store's save()/model_copy()
against a persisted row.

Generates exactly ONE unsubscribe token per call and derives BOTH the
human-footer URL and the one-click header URL from that SAME token (two
different paths, `/mail/unsubscribe` vs `/mail/unsubscribe/one-click`,
sharing one token value) -- per the approved B3 decision: "use the same
unsubscribe token/URL for the human footer and one-click header for a
given outbound message unless the protocol requires otherwise." The
protocol requires a different ENDPOINT (one-click must be a bare POST
with no confirmation page), not a different token.
"""

from dataclasses import dataclass

from app.config import settings
from app.services.unsubscribe_token import generate_unsubscribe_token

STANDARD_UNSUBSCRIBE_FOOTER = "\n\n---\nDon't want these emails? Unsubscribe: {url}"


class PublicOriginNotConfiguredError(Exception):
    """PUBLIC_BACKEND_ORIGIN is not set -- callers should surface this as
    a 503 (or, in this phase, simply fail rather than proceed), never
    fall back to a guessed/localhost URL. See app/config.py's
    public_backend_origin docstring for why this is deliberately unset in
    production until api.astronomicconnect.com's TLS is actually ready."""


@dataclass(frozen=True)
class ComposedOutboundEmail:
    """Everything a future MailSendRequest (app/services/
    mail_sending_service.py) would need from this module for one real
    send. `body` is the caller-supplied snapshot body PLUS the
    standardized footer -- never the snapshot itself, mutated."""

    body: str
    list_unsubscribe_header: str
    list_unsubscribe_post_header: str


def _resolve_origin(public_origin: str | None) -> str:
    """`public_origin`, when given, ALWAYS wins over settings -- this is
    the seam tests use to supply an explicit fake origin (see this
    module's own docstring / the B3 approval: "For tests use an explicit
    fake origin. Do NOT configure production yet")."""
    origin = public_origin if public_origin is not None else settings.public_backend_origin
    if not origin:
        raise PublicOriginNotConfiguredError(
            "PUBLIC_BACKEND_ORIGIN is not configured -- cannot build an unsubscribe URL."
        )
    return origin.rstrip("/")


def build_unsubscribe_urls(email: str, *, public_origin: str | None = None) -> tuple[str, str]:
    """Returns (confirm_url, one_click_url) -- both built from the SAME
    freshly generated token. `confirm_url` targets the human GET
    confirmation-page route; `one_click_url` targets the RFC 8058 one-
    click POST route. Raises PublicOriginNotConfiguredError or whatever
    generate_unsubscribe_token() itself can raise (UnsubscribeToken
    NotConfiguredError, ValueError for an unusable email) -- never
    silently returns a broken URL."""
    origin = _resolve_origin(public_origin)
    token = generate_unsubscribe_token(email)
    return (
        f"{origin}/mail/unsubscribe?token={token}",
        f"{origin}/mail/unsubscribe/one-click?token={token}",
    )


def compose_outbound_email(*, snapshot_body: str, recipient_email: str, public_origin: str | None = None) -> ComposedOutboundEmail:
    """The one entry point Phase C/D would call per real send. Composes
    the final body (snapshot + standardized footer) and both List-
    Unsubscribe header values from ONE shared token -- see this module's
    docstring for why sharing the token (not the URL path) is the
    invariant that matters."""
    confirm_url, one_click_url = build_unsubscribe_urls(recipient_email, public_origin=public_origin)
    return ComposedOutboundEmail(
        body=snapshot_body + STANDARD_UNSUBSCRIBE_FOOTER.format(url=confirm_url),
        list_unsubscribe_header=f"<{one_click_url}>",
        list_unsubscribe_post_header="List-Unsubscribe=One-Click",
    )


# SAFETY NOTE (updated for Phase C): app/services/mail_sending_service.py
# is now the ONE intended caller (MailSendingService.
# prepare_and_send_step()) -- deliberately still NEVER imported by
# app/google/gmail_sender.py, app/google/gmail_api_client.py, or any
# route under app/api/ -- see tests/test_mail_unsubscribe_safety.py's
# narrower, current enforcement of exactly that boundary.
