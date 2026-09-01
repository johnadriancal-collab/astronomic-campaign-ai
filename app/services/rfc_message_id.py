"""
RFC 5322 Message-ID generation -- deliberately owned by the EXECUTION
layer, not any one provider adapter (see MailSendRequest's docstring in
app/services/mail_sending_service.py for the full rationale: the durable
execution row must know its own outbound Message-ID before crossing the
provider-call uncertainty boundary, which means generating it is this
layer's job, not the Gmail adapter's).

This module has ZERO Gmail/SMTP/OAuth knowledge, deliberately, so that
both app/services/mail_sending_service.py (which must never import
anything Gmail/SMTP/OAuth-shaped -- see tests/test_mail_sending_safety.py's
test_mail_sending_service_never_imports_gmail_smtp_or_oauth) and any
concrete provider adapter (e.g. app/google/gmail_sender.py) can share the
exact same generation logic without either importing the other's
namespace.
"""

import uuid


def generate_rfc_message_id(sender_domain: str) -> str:
    """A fresh RFC 5322 Message-ID local-part@domain -- WITHOUT angle
    brackets (brackets belong only in an actual MIME header value; see
    app/google/gmail_mime.py's build_mime_message()). Uniqueness comes
    from uuid4 (122 bits of randomness) alone -- no caller input beyond
    the domain is required or accepted.

    Deliberately NOT seeded/deterministic from an execution identifier
    (e.g. an enrollment_step_id + attempt_count) here either: Phase C is
    what decides WHEN this is called and WHAT gets persisted from its
    result (generate once, persist onto MailEnrollmentStep.rfc_message_id
    BEFORE the CLAIMED->SENDING transition, then reuse that same
    persisted value for any retry/reconciliation of the same attempt --
    see MailSendRequest's docstring) -- this function only guarantees a
    fresh value is unique, which is all a pure generator can promise on
    its own."""
    return f"{uuid.uuid4().hex}@{sender_domain}"
