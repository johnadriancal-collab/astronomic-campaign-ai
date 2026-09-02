"""
Strips query strings from Uvicorn's own HTTP access-log lines -- a
blanket policy, not a per-path allowlist (see StripQueryStringFromAccessLog's
own docstring for why).

ROOT CAUSE this exists to fix: Uvicorn's access logger (the standard
library `logging.Logger` named "uvicorn.access") builds its request-line
argument via uvicorn.protocols.utils.get_path_with_query_string(scope),
which is `scope["path"]` PLUS `"?" + scope["query_string"]` when a query
string is present (see both uvicorn/protocols/http/httptools_impl.py and
h11_impl.py in the installed uvicorn package -- both call
`self.access_logger.info('%s - "%s %s HTTP/%s" %d', client_addr, method,
get_path_with_query_string(self.scope), http_version, status_code)`,
passing that combined path+query string as one positional log argument).
This has NOTHING to do with this application's own logging (loguru calls
in app/services/*.py never log a query string, a token, or a code -- see
tests/test_mailbox_api.py's/test_gmail_sending_safety.py's existing
never-logs-a-secret coverage) -- it is Uvicorn's own, generic, framework-
level access-log formatting, which by design mirrors NCSA/Apache's
"log the exact request line" convention. For most requests that's
harmless; for a GET-based OAuth callback (Google's own redirect
mechanism -- see app/api/mailboxes.py's google_oauth_callback(), which
this fix does NOT touch), that request line necessarily contains a live
`code` and `state` value, which then lands verbatim in Railway's captured
deploy logs.

WHY A BLANKET POLICY, NOT A PER-PATH REDACTION: a per-path allowlist
(e.g. "strip only /mailboxes/google/callback") only protects the paths
someone remembered to list, and stays correct only until the next query
parameter anywhere in this app happens to carry something sensitive.
Stripping the query string from EVERY access-log line is simpler, covers
this case and any future one uniformly, and costs nothing operationally:
method + path + HTTP version + status code (this app's own routes are
never distinguished by query string alone) remain fully visible, and
the real, complete query string is still exactly what FastAPI/Starlette
receives and parses for request handling -- this filter only changes
what gets WRITTEN to the access log line, strictly after the response
has already been produced from the real, untouched request.

NEVER hashes or truncates the stripped value -- there is no operational
need to retain any trace of it (see this module's own test file for the
explicit "produces no version of the sensitive value at all" proof).
"""

import logging

UVICORN_ACCESS_LOGGER_NAME = "uvicorn.access"

# The exact position, within the positional args Uvicorn's access logger is
# called with, of the "path[?query_string]" string -- see this module's own
# docstring for the two call sites (httptools_impl.py, h11_impl.py) this
# mirrors. A short, explicit constant beats a bare "2" at the call site.
_PATH_WITH_QUERY_ARG_INDEX = 2
_MIN_ARGS_LENGTH = _PATH_WITH_QUERY_ARG_INDEX + 1


def strip_query_string(path_with_query: str) -> str:
    """Pure helper, directly unit-testable without constructing a real
    LogRecord: returns everything before the first '?', unchanged if
    there is no '?' at all."""
    return path_with_query.split("?", 1)[0]


class StripQueryStringFromAccessLog(logging.Filter):
    """Attached to the "uvicorn.access" logger (see install() below) --
    never to request handling. Mutates ONLY the in-memory LogRecord's
    args, strictly after Uvicorn has already built it from the real
    request/response, and strictly before any handler (stdout, and from
    there Railway's log capture) ever renders it to text. Always returns
    True: this filter's job is to sanitize a record, never to drop one.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= _MIN_ARGS_LENGTH:
            path_with_query = record.args[_PATH_WITH_QUERY_ARG_INDEX]
            if isinstance(path_with_query, str) and "?" in path_with_query:
                sanitized = list(record.args)
                sanitized[_PATH_WITH_QUERY_ARG_INDEX] = strip_query_string(path_with_query)
                record.args = tuple(sanitized)
        return True


def install() -> None:
    """Idempotent -- safe to call more than once (e.g. under a test
    runner that imports app.main repeatedly): logging.Filter has no
    identity-deduplication of its own, so this checks first rather than
    letting repeated calls silently stack duplicate (harmless, but
    wasteful) filter instances on the same logger."""
    access_logger = logging.getLogger(UVICORN_ACCESS_LOGGER_NAME)
    if not any(isinstance(f, StripQueryStringFromAccessLog) for f in access_logger.filters):
        access_logger.addFilter(StripQueryStringFromAccessLog())
