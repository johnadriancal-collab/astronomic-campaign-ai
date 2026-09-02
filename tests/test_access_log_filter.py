"""
app/access_log_filter.py -- verifies the OAuth-callback-secret-in-access-
logs fix. See that module's own docstring for the root cause (Uvicorn's
own "uvicorn.access" logger, not this application's own logging) and why
the fix is a blanket query-string strip rather than a per-path redaction.
"""

import logging

import pytest

from app.access_log_filter import (
    UVICORN_ACCESS_LOGGER_NAME,
    StripQueryStringFromAccessLog,
    install,
    strip_query_string,
)

# The exact positional-args shape Uvicorn's access logger is called with
# -- see both uvicorn/protocols/http/httptools_impl.py and h11_impl.py
# (installed package, not this repo): self.access_logger.info(
#   '%s - "%s %s HTTP/%s" %d', client_addr, method, path_with_query, http_version, status
# )
UVICORN_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def _make_record(path_with_query: str, *, method: str = "GET", status: int = 307) -> logging.LogRecord:
    return logging.LogRecord(
        name=UVICORN_ACCESS_LOGGER_NAME,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=UVICORN_ACCESS_FORMAT,
        args=("127.0.0.1:12345", method, path_with_query, "1.1", status),
        exc_info=None,
    )


# --- strip_query_string() -- pure function ----------------------------------


def test_strip_query_string_removes_everything_from_the_first_question_mark():
    assert strip_query_string("/mailboxes/google/callback?code=abc&state=xyz") == "/mailboxes/google/callback"


def test_strip_query_string_is_a_noop_when_there_is_no_query_string():
    assert strip_query_string("/health") == "/health"


def test_strip_query_string_only_splits_on_the_first_question_mark():
    assert strip_query_string("/a?b=1?c=2") == "/a"


# --- StripQueryStringFromAccessLog -- the LogRecord-level filter -----------


def test_filter_strips_the_query_string_from_the_path_argument():
    record = _make_record("/mailboxes/google/callback?code=SECRET_CODE&state=SECRET_STATE&scope=email+profile")
    StripQueryStringFromAccessLog().filter(record)

    assert record.args[2] == "/mailboxes/google/callback"
    assert "SECRET_CODE" not in record.getMessage()
    assert "SECRET_STATE" not in record.getMessage()


def test_filter_preserves_method_path_http_version_and_status():
    record = _make_record("/mailboxes/google/callback?code=abc", method="GET", status=307)
    StripQueryStringFromAccessLog().filter(record)

    rendered = record.getMessage()
    assert "GET" in rendered
    assert "/mailboxes/google/callback" in rendered
    assert "HTTP/1.1" in rendered
    assert "307" in rendered


def test_filter_leaves_a_query_string_free_path_completely_unchanged():
    record = _make_record("/health")
    StripQueryStringFromAccessLog().filter(record)

    assert record.args[2] == "/health"
    assert record.getMessage() == '127.0.0.1:12345 - "GET /health HTTP/1.1" 307'


def test_filter_always_returns_true_it_never_drops_a_record():
    record = _make_record("/mailboxes/google/callback?code=abc")
    assert StripQueryStringFromAccessLog().filter(record) is True


def test_filter_applies_to_any_path_not_only_the_oauth_callback():
    """Blanket policy, not a per-path allowlist -- see the module's own
    docstring for why. A completely unrelated query string (e.g. a
    future endpoint's own parameters) is stripped identically."""
    record = _make_record("/crm/contacts?q=someone%40example.com&page=2")
    StripQueryStringFromAccessLog().filter(record)

    assert record.args[2] == "/crm/contacts"
    assert "someone" not in record.getMessage()


def test_filter_does_not_hash_or_truncate_the_stripped_value_it_is_simply_gone():
    record = _make_record("/mailboxes/google/callback?code=SECRET_CODE_VALUE")
    StripQueryStringFromAccessLog().filter(record)

    rendered = record.getMessage()
    assert "SECRET_CODE_VALUE" not in rendered
    # No hash/hex/truncated fragment of the value should appear either --
    # a prefix would still be a partial leak.
    assert "SECRET_CODE" not in rendered
    assert "SECRET" not in rendered


def test_filter_tolerates_a_record_with_the_wrong_arg_shape_without_raising():
    """Defensive: if some OTHER logger ever reuses this filter, or a
    future Uvicorn version changes its call shape, this must not crash
    request handling -- it should just pass the record through
    unmodified rather than raising."""
    record = logging.LogRecord(
        name=UVICORN_ACCESS_LOGGER_NAME, level=logging.INFO, pathname=__file__, lineno=1,
        msg="Started server process [%d]", args=(1234,), exc_info=None,
    )
    result = StripQueryStringFromAccessLog().filter(record)
    assert result is True
    assert record.args == (1234,)


# --- End-to-end through the real "uvicorn.access" logger + a real handler --


@pytest.fixture
def captured_access_log():
    """Attaches a capturing handler to the REAL "uvicorn.access" logger
    (the same one Uvicorn itself writes to and install() targets),
    installs the filter, and yields the list of fully-rendered log
    lines. Restores the logger's prior filters/handlers/level afterward
    so this test can never leak state into any other test."""
    logger = logging.getLogger(UVICORN_ACCESS_LOGGER_NAME)
    original_filters = list(logger.filters)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate

    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    logger.filters = []
    logger.handlers = [_Capture()]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    install()

    try:
        yield logger, lines
    finally:
        logger.filters = original_filters
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_end_to_end_oauth_callback_request_line_never_contains_the_secret_values(captured_access_log):
    logger, lines = captured_access_log
    logger.info(
        UVICORN_ACCESS_FORMAT,
        "203.0.113.9:54321",
        "GET",
        "/mailboxes/google/callback?state=REAL_STATE_VALUE&code=4%2FREAL_AUTH_CODE&scope=openid+email+profile",
        "1.1",
        307,
    )

    assert len(lines) == 1
    assert "REAL_STATE_VALUE" not in lines[0]
    assert "REAL_AUTH_CODE" not in lines[0]
    assert "code=" not in lines[0]
    assert "state=" not in lines[0]
    assert "scope=" not in lines[0]


def test_end_to_end_method_path_status_still_observable(captured_access_log):
    logger, lines = captured_access_log
    logger.info(
        UVICORN_ACCESS_FORMAT,
        "203.0.113.9:54321",
        "GET",
        "/mailboxes/google/callback?state=REAL_STATE_VALUE&code=abc",
        "1.1",
        307,
    )

    assert lines[0] == '203.0.113.9:54321 - "GET /mailboxes/google/callback HTTP/1.1" 307'


def test_end_to_end_ordinary_request_with_no_query_string_logs_normally(captured_access_log):
    logger, lines = captured_access_log
    logger.info(UVICORN_ACCESS_FORMAT, "203.0.113.9:54321", "GET", "/health", "1.1", 200)

    assert lines[0] == '203.0.113.9:54321 - "GET /health HTTP/1.1" 200'


def test_end_to_end_error_callback_query_params_also_never_appear(captured_access_log):
    """Google's OWN error redirect (access_denied, etc.) still carries a
    live `state` value -- must be stripped exactly like a success
    callback's code."""
    logger, lines = captured_access_log
    logger.info(
        UVICORN_ACCESS_FORMAT,
        "203.0.113.9:54321",
        "GET",
        "/mailboxes/google/callback?state=REAL_STATE_VALUE&error=access_denied",
        "1.1",
        307,
    )

    assert "REAL_STATE_VALUE" not in lines[0]
    assert "state=" not in lines[0]
    assert "error=" not in lines[0]


def test_install_is_idempotent_does_not_stack_duplicate_filters():
    logger = logging.getLogger(UVICORN_ACCESS_LOGGER_NAME)
    original_filters = list(logger.filters)
    try:
        logger.filters = []
        install()
        install()
        install()
        matching = [f for f in logger.filters if isinstance(f, StripQueryStringFromAccessLog)]
        assert len(matching) == 1
    finally:
        logger.filters = original_filters
