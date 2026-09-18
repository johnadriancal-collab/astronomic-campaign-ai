"""
derive_reply_preview() -- Inbox "Reply" column preview (2026-09-18).

The ONE shared function both MailReplyDetectionService (for every future
reply, using Gmail's `snippet` field from the SAME metadata-scope
get_thread() call it already makes -- zero new Gmail calls, zero new
scope) and the one-time startup backfill (app/services/
mail_reply_preview_backfill.py, for the handful of replies that predate
this field) call to turn Gmail's raw `snippet` into a short, safe,
plain-text preview of ONLY the new reply -- never the quoted prior
message, never raw HTML, never the full body.

Investigated, not assumed: confirmed live against this app's real
production replies that gmail.metadata-scope responses (both
threads.get and messages.get) already include Gmail's own `snippet`
field -- contradicting this codebase's own prior assumption that
metadata "structurally cannot return body/snippet content" (true for
the full MIME body, not true for `snippet`). See this feature's
approved architecture report for the full investigation.

WHY NOT reuse frontend/lib/reply-formatting.ts's splitReplyQuote(): that
heuristic is built for a real multi-line body with literal ">" quote-
prefix lines. Gmail's `snippet` is a flattened, whitespace-collapsed,
~200-char excerpt with NO line breaks and NO ">" markers -- running the
line-based heuristic on it is a correctness no-op, not a reuse. This is
a genuinely different, lossier data shape, so a separate (not
"inconsistent" -- just applicable to a different input) heuristic is
unavoidable here. Validated by hand against every real reply this
mailbox has produced so far (see tests/test_mail_reply_preview.py).

THE HEURISTIC, in priority order (never truncate real reply content by
mistake; avoid the OBVIOUS quoted-message spillover when confident;
don't have to be perfect in every locale):

1. Decode HTML entities, collapse whitespace.
2. Look for the SENDING mailbox's own email address in bracket form
   (`<mailbox_email>`) -- Gmail's quote-header line always renders the
   quoted sender's address this way, in every locale checked (English,
   Filipino). This is the one deliberately narrow, high-confidence
   signal: matching against a SPECIFIC, already-known address (never
   "any email"), so a prospect mentioning a different email address in
   their own new reply can never trigger a premature cut.
3. If that bracket is found, additionally look for a Gmail-style
   date/time expression (`<word>, <word> <day>, <year> <word>
   <hour>:<minute> AM/PM` -- matched by SHAPE, not by English
   vocabulary, so it matches equally well in Filipino's "Huw, Set 17,
   2026 nang 12:45 PM") appearing before that bracket, and extend the
   cut point back to the START of that match, consuming at most ONE
   additional whitespace-delimited word immediately before it (the
   localized "On"/"Noong" connector) -- never more than one word, so a
   real preceding word (a name, part of a sentence) is never eaten.
4. If the bracket isn't found at all, nothing here looks confident
   enough to cut -- keep the whole (length-capped) snippet rather than
   risk deleting real content.
"""

import html
import re

PREVIEW_MAX_CHARS = 200

_DATE_TIME_SHAPE_RE = re.compile(
    r"\S+,?\s+\S+\s+\d{1,2},\s*\d{4}\s+\S+\s+\d{1,2}:\d{2}\s*(?:AM|PM)",
    re.IGNORECASE,
)


def _extend_cut_by_one_preceding_word(text: str, boundary: int) -> int:
    """Given `text` and a `boundary` index (the start of the date/time-
    shape match), returns an earlier boundary that ALSO consumes exactly
    one immediately-preceding whitespace-delimited, letters-only word
    (e.g. "On "/"Noong ") -- never more than one, so real preceding
    content is never eaten. Returns `boundary` unchanged if there is no
    such word directly adjacent."""
    prefix = text[:boundary].rstrip()
    match = re.search(r"[A-Za-z]+$", prefix)
    if not match:
        return boundary
    word_start = match.start()
    if word_start > 0 and not prefix[word_start - 1].isspace():
        return boundary  # would split a word in half -- never do that
    return word_start


def derive_reply_preview(raw_snippet: str | None, mailbox_email: str | None) -> str | None:
    """Pure, deterministic, no I/O. Returns None for empty/whitespace-only
    input -- callers store that as `reply_preview=None`, never a
    fabricated placeholder string."""
    if not raw_snippet:
        return None

    decoded = html.unescape(raw_snippet)
    normalized = re.sub(r"\s+", " ", decoded).strip()
    if not normalized:
        return None

    cutoff: int | None = None
    if mailbox_email:
        bracket_pattern = re.compile(r"<\s*" + re.escape(mailbox_email) + r"\s*>", re.IGNORECASE)
        bracket_match = bracket_pattern.search(normalized)
        if bracket_match:
            cutoff = bracket_match.start()
            before_bracket = normalized[:cutoff]
            date_match = _DATE_TIME_SHAPE_RE.search(before_bracket)
            if date_match:
                cutoff = _extend_cut_by_one_preceding_word(normalized, date_match.start())

    preview = normalized[:cutoff] if cutoff is not None else normalized
    preview = preview.rstrip()[:PREVIEW_MAX_CHARS].rstrip()
    return preview or None
