"""
derive_reply_preview() -- see app/services/mail_reply_preview.py's own
module docstring for the full heuristic and why it's a deliberately
separate (not "inconsistent") parser from frontend/lib/reply-
formatting.ts's splitReplyQuote(). The four "production-equivalent"
fixtures below are the REAL raw `snippet` strings this app's own
production replies produced (captured, verified, and reverted via a
temporary read-only diagnostic addition -- see this feature's approved
architecture report), not synthetic examples.
"""

from app.services.mail_reply_preview import derive_reply_preview

MAILBOX_EMAIL = "victoria@useastronomic.com"

# Real production snippet, Brendan Hayes (English).
BRENDAN_SNIPPET = (
    "got it, thanks. On Thu, Sep 17, 2026 at 12:46 PM &lt;victoria@useastronomic.com&gt; wrote: "
    "Hi Brendan, Quick note from Astronomic — we&#39;re testing a new way of managing our outreach and wanted to"
)

# Real production snippet, Maximus Romy (Filipino Gmail UI locale).
MAXIMUS_SNIPPET = (
    "nice to meet you. - Maximus Noong Huw, Set 17, 2026 nang 12:45 PM, sinulat ni "
    "&lt;victoria@useastronomic.com&gt; ang: Hi Maximus, Quick note from Astronomic — we&#39;re testing a new way of managing"
)

# Real production snippet, John Adrian Cal, 5-Person Pilot.
JOHN_PILOT_SNIPPET = (
    "Nice to hear from you. this is great news. John On Thu, Sep 17, 2026 at 12:47 PM "
    "&lt;victoria@useastronomic.com&gt; wrote: Hi John Adrian, Quick note from Astronomic — we&#39;re testing a new way of"
)

# Real production snippet, John Adrian Cal, ZZTEST reply-stop verification.
JOHN_ZZTEST_SNIPPET = (
    "test reply On Wed, Sep 16, 2026 at 9:40 AM &lt;victoria@useastronomic.com&gt; wrote: "
    "Hi John Adrian, This is Step 1 of the controlled reply-stop production test. Do not send Step 2 too soon. Don&#39;t"
)


def test_brendan_production_equivalent_preview():
    assert derive_reply_preview(BRENDAN_SNIPPET, MAILBOX_EMAIL) == "got it, thanks."


def test_maximus_production_equivalent_preview_filipino_locale():
    assert derive_reply_preview(MAXIMUS_SNIPPET, MAILBOX_EMAIL) == "nice to meet you. - Maximus"


def test_john_pilot_production_equivalent_preview():
    assert derive_reply_preview(JOHN_PILOT_SNIPPET, MAILBOX_EMAIL) == "Nice to hear from you. this is great news. John"


def test_john_zztest_production_equivalent_preview():
    assert derive_reply_preview(JOHN_ZZTEST_SNIPPET, MAILBOX_EMAIL) == "test reply"


def test_none_snippet_returns_none():
    assert derive_reply_preview(None, MAILBOX_EMAIL) is None


def test_empty_snippet_returns_none():
    assert derive_reply_preview("", MAILBOX_EMAIL) is None
    assert derive_reply_preview("   ", MAILBOX_EMAIL) is None


def test_html_entities_are_decoded():
    preview = derive_reply_preview("we&#39;re happy &lt;test&gt; &amp; excited", None)
    assert preview == "we're happy <test> & excited"


def test_whitespace_is_normalized():
    preview = derive_reply_preview("hello\n\n  there   \t friend", None)
    assert preview == "hello there friend"


def test_result_never_exceeds_the_length_cap():
    long_snippet = "word " * 100
    preview = derive_reply_preview(long_snippet, None)
    assert preview is not None
    assert len(preview) <= 200


def test_no_bracket_match_at_all_keeps_the_whole_capped_snippet_rather_than_risk_deleting_real_content():
    """No mailbox_email bracket anywhere -- nothing here looks confident
    enough to cut, so the (length-capped) snippet is kept whole."""
    snippet = "Thanks so much for reaching out, this looks like a great fit for our team."
    assert derive_reply_preview(snippet, MAILBOX_EMAIL) == snippet


def test_a_prospect_mentioning_their_OWN_email_in_the_new_reply_never_causes_a_premature_cut():
    """The cut heuristic only ever matches OUR OWN known mailbox_email in
    bracket form -- a prospect writing their own (different) email
    address, bracketed or not, must never trigger a cut."""
    snippet = "Sure, reach me at prospect@example.com or call me anytime. Thanks!"
    assert derive_reply_preview(snippet, MAILBOX_EMAIL) == snippet


def test_a_prospect_mentioning_our_mailbox_email_WITHOUT_brackets_does_not_cut():
    """Only the bracketed quote-header form <email> is treated as a
    confident quote-boundary signal -- a bare mention (no angle
    brackets) of our address in the prospect's own new text is not
    assumed to be a quote header."""
    snippet = f"Please loop in {MAILBOX_EMAIL} on the next steps, thanks!"
    assert derive_reply_preview(snippet, MAILBOX_EMAIL) == snippet


def test_bracket_found_but_no_nearby_date_shape_falls_back_to_cutting_right_before_the_bracket():
    snippet = f"Thanks! Forwarding to the team. <{MAILBOX_EMAIL}> was cc'd on the original thread."
    preview = derive_reply_preview(snippet, MAILBOX_EMAIL)
    assert preview == "Thanks! Forwarding to the team."


def test_no_mailbox_email_provided_never_crashes_and_keeps_the_snippet():
    assert derive_reply_preview(BRENDAN_SNIPPET, None) is not None
    assert derive_reply_preview(BRENDAN_SNIPPET, "") is not None
