"""
MailSendRequest / SendOutcomeCertainty / MailSendError -- the provider
boundary's execution-owned contract (app/services/mail_sending_service.py),
introduced by the B2 hardening pass. Pure dataclass/validation tests, no
network, no mailbox stores -- see tests/test_gmail_sender.py for
GmailSender's consumption of this contract, and tests/
test_gmail_api_client.py for the outcome-certainty taxonomy built on top
of it.
"""

from datetime import datetime, timezone

import pytest

from app.models.mailbox import Mailbox, MailboxProvider, MailboxStatus
from app.services.mail_sending_service import (
    MailSendError,
    MailSendRequest,
    MailSendRequestValidationError,
    SendOutcomeCertainty,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_mailbox() -> Mailbox:
    return Mailbox(
        mailbox_id="mb-1",
        provider=MailboxProvider.GOOGLE,
        email="victoria@astronomic.com",
        display_name="Victoria",
        status=MailboxStatus.CONNECTED,
        google_user_id="sub-1",
        granted_scopes=[],
        connected_at=NOW,
        updated_at=NOW,
    )


def make_request(**overrides) -> MailSendRequest:
    defaults = dict(
        mailbox=make_mailbox(),
        to_email="lead@example.com",
        subject="Hi",
        body="Body",
        rfc_message_id="abc123@astronomic.com",
        reply_in_thread=False,
    )
    defaults.update(overrides)
    return MailSendRequest(**defaults)


# --- Valid shapes ----------------------------------------------------------------


def test_a_well_formed_new_thread_request_constructs_successfully():
    request = make_request()
    assert request.rfc_message_id == "abc123@astronomic.com"
    assert request.in_reply_to_message_id is None
    assert request.thread_id is None


def test_a_well_formed_reply_request_constructs_successfully():
    request = make_request(
        reply_in_thread=True,
        in_reply_to_message_id="prior@astronomic.com",
        thread_id="thr-1",
        references=("prior@astronomic.com",),
    )
    assert request.in_reply_to_message_id == "prior@astronomic.com"
    assert request.thread_id == "thr-1"


def test_a_reply_with_no_references_is_still_valid():
    """A first reply has only one ancestor -- itself the
    in_reply_to_message_id -- so an empty references tuple is fine as
    long as in_reply_to_message_id/thread_id are both present."""
    request = make_request(reply_in_thread=True, in_reply_to_message_id="prior@astronomic.com", thread_id="thr-1")
    assert request.references == ()


def test_reply_in_thread_true_with_no_threading_context_is_valid():
    """The normal Step 1 shape: MailSequenceStep's reply_in_thread
    defaults True even though Step 1 has no predecessor to thread under
    -- this must NOT be rejected. See MailSendRequest's docstring."""
    request = make_request(reply_in_thread=True)
    assert request.in_reply_to_message_id is None
    assert request.thread_id is None


# --- rfc_message_id validation ----------------------------------------------------


def test_empty_message_id_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="")


def test_whitespace_only_message_id_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="   ")


def test_message_id_without_at_sign_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="not-an-email-shape")


def test_message_id_with_angle_brackets_is_rejected():
    """Angle brackets belong only in the actual MIME header value (added
    by build_mime_message()) -- a caller passing them here is a bug."""
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="<abc123@astronomic.com>")


def test_message_id_with_crlf_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="abc\r\nBcc: attacker@evil.com@astronomic.com")


def test_message_id_with_lf_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(rfc_message_id="abc\nBcc: attacker@evil.com@astronomic.com")


# --- Threading shape validation ---------------------------------------------------


def test_reply_in_thread_true_with_thread_id_but_no_in_reply_to_is_rejected():
    """Incomplete threading context -- one field without the other."""
    with pytest.raises(MailSendRequestValidationError):
        make_request(reply_in_thread=True, thread_id="thr-1")


def test_reply_in_thread_true_with_in_reply_to_but_no_thread_id_is_rejected():
    """Incomplete threading context -- one field without the other."""
    with pytest.raises(MailSendRequestValidationError):
        make_request(reply_in_thread=True, in_reply_to_message_id="prior@astronomic.com")


def test_reply_in_thread_false_with_in_reply_to_is_rejected():
    """A new-thread send must not carry stale threading context."""
    with pytest.raises(MailSendRequestValidationError):
        make_request(reply_in_thread=False, in_reply_to_message_id="prior@astronomic.com", thread_id="thr-1")


def test_reply_in_thread_false_with_thread_id_only_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(reply_in_thread=False, thread_id="thr-1")


def test_reply_in_thread_false_with_references_only_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(reply_in_thread=False, references=("prior@astronomic.com",))


def test_references_without_in_reply_to_is_rejected_even_when_reply_in_thread_true():
    """Can't happen in practice (reply_in_thread=True already requires
    in_reply_to_message_id), but the References-implies-a-parent rule is
    asserted directly here as its own invariant."""
    with pytest.raises(MailSendRequestValidationError):
        MailSendRequest(
            mailbox=make_mailbox(), to_email="lead@example.com", subject="s", body="b",
            rfc_message_id="abc@astronomic.com", reply_in_thread=True,
            in_reply_to_message_id=None, references=("prior@astronomic.com",), thread_id="thr-1",
        )


# --- SendOutcomeCertainty / MailSendError -----------------------------------------


def test_send_outcome_certainty_has_exactly_two_values():
    assert {c.value for c in SendOutcomeCertainty} == {"definitely_not_sent", "outcome_unknown"}


def test_mail_send_error_defaults_to_outcome_unknown():
    assert MailSendError.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


def test_a_bare_mail_send_error_instance_carries_the_conservative_default():
    err = MailSendError("something went wrong")
    assert err.certainty == SendOutcomeCertainty.OUTCOME_UNKNOWN


def test_a_bare_mail_send_error_instance_defaults_retryable_to_false():
    err = MailSendError("something went wrong")
    assert err.retryable is False


# --- list_unsubscribe_header / list_unsubscribe_post_header (Phase C) -----------


def test_both_list_unsubscribe_headers_together_is_valid():
    request = make_request(
        list_unsubscribe_header="<https://astronomic.example/u/tok-1>",
        list_unsubscribe_post_header="List-Unsubscribe=One-Click",
    )
    assert request.list_unsubscribe_header == "<https://astronomic.example/u/tok-1>"
    assert request.list_unsubscribe_post_header == "List-Unsubscribe=One-Click"


def test_neither_list_unsubscribe_header_is_valid():
    request = make_request()
    assert request.list_unsubscribe_header is None
    assert request.list_unsubscribe_post_header is None


def test_list_unsubscribe_header_without_post_header_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(list_unsubscribe_header="<https://astronomic.example/u/tok-1>")


def test_list_unsubscribe_post_header_without_header_is_rejected():
    with pytest.raises(MailSendRequestValidationError):
        make_request(list_unsubscribe_post_header="List-Unsubscribe=One-Click")
