"""
Structural safety tests for the admin/service read-only token
(app/session_auth_middleware.py's "Admin/service read-only token" section)
-- complements tests/test_session_auth_middleware.py's behavioral
coverage (mounted against the REAL middleware function) with two things
that need direct source inspection instead: (1) an AST-based guarantee
that no logger call in this file can ever interpolate the token/
Authorization header, matching the exact pattern already established for
OAuth logging in tests/test_mailbox_sending_safety.py, and (2) direct
unit tests of the pure scope-check helper, isolated from the FastAPI
request/response cycle.
"""

import ast
import re
from pathlib import Path

from app.session_auth_middleware import _is_excluded_from_service_read, _is_in_service_read_scope

_MODULE_PATH = "app/session_auth_middleware.py"


def _logger_call_interpolated_expressions(path: str) -> list[str]:
    """Every f-string expression (as unparsed source text) interpolated
    into a `logger.<level>(...)` call anywhere in `path`. Same helper as
    tests/test_mailbox_sending_safety.py -- duplicated rather than
    imported, matching that file's own reasoning for keeping this
    self-contained per module under test."""
    source = Path(path).read_text()
    tree = ast.parse(source)
    expressions: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                for value in arg.values:
                    if isinstance(value, ast.FormattedValue):
                        expressions.append(ast.unparse(value.value))
    return expressions


def test_no_logging_call_in_this_module_interpolates_the_token_or_header():
    forbidden_whole_identifiers = (
        r"\btoken\b",
        r"\bauthorization\b",
        r"\braw_token\b",
        r"\bsettings\b",  # would catch settings.admin_service_read_token specifically
    )
    expressions = _logger_call_interpolated_expressions(_MODULE_PATH)
    assert expressions, "expected at least one logger call in this module -- did the log line move or get removed?"
    for expr in expressions:
        for pattern in forbidden_whole_identifiers:
            assert not re.search(pattern, expr), f"{_MODULE_PATH}: logger call interpolates {expr!r} (matches {pattern})"


def test_no_logging_call_in_this_module_interpolates_a_whole_request_or_response_object():
    """A second, structural layer: every interpolated expression is short
    (an outcome word, a method, a path) -- none of them is str()/repr()
    against a whole Request/Response object, which could smuggle the
    Authorization header through under a name the test above doesn't
    already know to forbid."""
    for expr in _logger_call_interpolated_expressions(_MODULE_PATH):
        assert len(expr) < 40, f"{_MODULE_PATH}: suspiciously long interpolated logger expression: {expr!r}"
        assert expr in ("outcome", "request.method", "request.url.path"), (
            f"{_MODULE_PATH}: unexpected interpolated expression {expr!r} -- review before allowing"
        )


# --- _is_in_service_read_scope: pure scope-check logic --------------------


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    def __init__(self, method: str, path: str):
        self.method = method
        self.url = _FakeURL(path)


def test_get_under_crm_is_in_scope():
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/contacts")) is True


def test_head_and_options_under_crm_are_in_scope():
    assert _is_in_service_read_scope(_FakeRequest("HEAD", "/crm/contacts")) is True
    assert _is_in_service_read_scope(_FakeRequest("OPTIONS", "/crm/contacts")) is True


def test_post_under_crm_is_out_of_scope():
    assert _is_in_service_read_scope(_FakeRequest("POST", "/crm/contacts")) is False


def test_patch_and_delete_under_crm_are_out_of_scope():
    assert _is_in_service_read_scope(_FakeRequest("PATCH", "/crm/contacts/some-id")) is False
    assert _is_in_service_read_scope(_FakeRequest("DELETE", "/crm/contacts/some-id")) is False


def test_get_outside_crm_is_out_of_scope():
    assert _is_in_service_read_scope(_FakeRequest("GET", "/mail/campaigns")) is False
    assert _is_in_service_read_scope(_FakeRequest("GET", "/mailboxes")) is False
    assert _is_in_service_read_scope(_FakeRequest("GET", "/auth/session")) is False


def test_bare_crm_path_with_no_trailing_slash_is_not_treated_as_in_scope():
    """Precision guard: the prefix check requires a trailing slash after
    "/crm" (i.e. "/crm/..."), so a hypothetical unrelated path that merely
    starts with the same four characters (e.g. "/crmfoo") is never
    mistakenly treated as in-scope."""
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crmfoo")) is False


# --- /crm/backup exclusion --------------------------------------------


def test_backup_export_itself_is_excluded():
    assert _is_excluded_from_service_read("/crm/backup/export") is True
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/backup/export")) is False


def test_the_bare_backup_path_is_excluded():
    assert _is_excluded_from_service_read("/crm/backup") is True


def test_a_hypothetical_future_nested_backup_path_is_also_excluded():
    assert _is_excluded_from_service_read("/crm/backup/some-new-route") is True
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/backup/some-new-route")) is False


def test_backupfoo_is_not_mistakenly_excluded():
    """Precision guard: a hypothetical unrelated route that merely starts
    with the same characters as "/crm/backup" must not be excluded."""
    assert _is_excluded_from_service_read("/crm/backupfoo") is False
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/backupfoo")) is True


def test_exclusion_applies_regardless_of_method():
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PATCH", "DELETE"):
        assert _is_in_service_read_scope(_FakeRequest(method, "/crm/backup/export")) is False


# --- /crm/import exclusion (same pattern as /crm/backup above) -----------


def test_import_batch_itself_is_excluded():
    assert _is_excluded_from_service_read("/crm/import/some-batch-id") is True
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/import/some-batch-id")) is False


def test_the_bare_import_path_is_excluded():
    assert _is_excluded_from_service_read("/crm/import") is True


def test_a_hypothetical_future_nested_import_path_is_also_excluded():
    assert _is_excluded_from_service_read("/crm/import/some-batch-id/nested") is True
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/import/some-batch-id/nested")) is False


def test_importfoo_is_not_mistakenly_excluded():
    assert _is_excluded_from_service_read("/crm/importfoo") is False
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/importfoo")) is True


def test_import_exclusion_applies_regardless_of_method():
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PATCH", "DELETE"):
        assert _is_in_service_read_scope(_FakeRequest(method, "/crm/import/some-batch-id")) is False


def test_per_contact_source_snapshot_route_remains_in_scope():
    """Explicitly NOT excluded, per product decision: bounded to one
    contact at a time, unlike /crm/backup and /crm/import."""
    assert _is_excluded_from_service_read("/crm/contacts/some-contact-id") is False
    assert _is_in_service_read_scope(_FakeRequest("GET", "/crm/contacts/some-contact-id")) is True
