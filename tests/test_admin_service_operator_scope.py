"""
Structural safety tests for the admin/service OPERATOR token's scope check
(app/session_auth_middleware.py's "Admin/service OPERATOR token" section)
-- mirrors tests/test_admin_service_auth.py's split (pure logic + AST
logging-safety here; tests/test_session_auth_middleware.py covers the full
HTTP behavior against the REAL mounted middleware). Every allowed rule and
every explicitly-excluded action from the approved scope gets its own
direct assertion against `_is_in_service_operator_scope`, isolated from
the FastAPI request/response cycle.
"""

import ast
import re
from pathlib import Path

from app.session_auth_middleware import _is_in_service_operator_scope

_MODULE_PATH = "app/session_auth_middleware.py"


def _logger_call_interpolated_expressions(path: str) -> list[str]:
    """Same helper as tests/test_admin_service_auth.py -- duplicated
    rather than imported, matching that file's own reasoning for keeping
    this self-contained per module under test."""
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
    """Re-asserted here (not just in test_admin_service_auth.py) because
    this feature added new logger.info call sites to the same module --
    this proves the NEW calls also satisfy the guarantee, not just the
    ones that existed before Phase 2."""
    forbidden_whole_identifiers = (r"\btoken\b", r"\bauthorization\b", r"\braw_token\b", r"\bsettings\b")
    expressions = _logger_call_interpolated_expressions(_MODULE_PATH)
    assert len(expressions) >= 3, "expected at least one logger call per identity (read, operator, unrecognized)"
    for expr in expressions:
        for pattern in forbidden_whole_identifiers:
            assert not re.search(pattern, expr), f"{_MODULE_PATH}: logger call interpolates {expr!r} (matches {pattern})"


def test_no_logging_call_in_this_module_interpolates_a_whole_request_or_response_object():
    for expr in _logger_call_interpolated_expressions(_MODULE_PATH):
        assert len(expr) < 40, f"{_MODULE_PATH}: suspiciously long interpolated logger expression: {expr!r}"
        assert expr in ("outcome", "request.method", "request.url.path"), (
            f"{_MODULE_PATH}: unexpected interpolated expression {expr!r} -- review before allowing"
        )


class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    def __init__(self, method: str, path: str):
        self.method = method
        self.url = _FakeURL(path)


def _in_scope(method: str, path: str) -> bool:
    return _is_in_service_operator_scope(_FakeRequest(method, path))


# --- Mail campaigns: allowed -------------------------------------------------


def test_list_and_create_campaigns_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns") is True
    assert _in_scope("POST", "/mail/campaigns") is True


def test_get_and_patch_a_single_campaign_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns/c1") is True
    assert _in_scope("PATCH", "/mail/campaigns/c1") is True


def test_mark_ready_unlock_activate_and_pause_are_in_scope():
    assert _in_scope("POST", "/mail/campaigns/c1/ready") is True
    assert _in_scope("POST", "/mail/campaigns/c1/unlock") is True
    assert _in_scope("POST", "/mail/campaigns/c1/activate") is True
    assert _in_scope("POST", "/mail/campaigns/c1/pause") is True


def test_review_and_enrollments_reads_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns/c1/review") is True
    assert _in_scope("GET", "/mail/campaigns/c1/enrollments") is True


def test_channels_read_and_write_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns/c1/channels") is True
    assert _in_scope("PUT", "/mail/campaigns/c1/channels") is True


def test_schedule_read_and_write_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns/c1/schedule") is True
    assert _in_scope("PUT", "/mail/campaigns/c1/schedule") is True


def test_steps_crud_and_reorder_are_in_scope():
    assert _in_scope("GET", "/mail/campaigns/c1/steps") is True
    assert _in_scope("POST", "/mail/campaigns/c1/steps") is True
    assert _in_scope("PATCH", "/mail/campaigns/c1/steps/s1") is True
    assert _in_scope("DELETE", "/mail/campaigns/c1/steps/s1") is True
    assert _in_scope("POST", "/mail/campaigns/c1/steps/reorder") is True


# --- Mail campaigns: explicitly excluded ------------------------------------


def test_resume_and_archive_remain_out_of_scope():
    """Activate and Pause were explicitly approved (2026-09-03) as a
    matched pair of safety gates -- see the module docstring. Resume and
    Archive remain excluded and need their own future review."""
    assert _in_scope("POST", "/mail/campaigns/c1/resume") is False
    assert _in_scope("POST", "/mail/campaigns/c1/archive") is False


def test_suppressions_are_out_of_scope():
    assert _in_scope("GET", "/mail/suppressions") is False
    assert _in_scope("POST", "/mail/suppressions") is False
    assert _in_scope("POST", "/mail/suppressions/unsuppress") is False
    assert _in_scope("GET", "/mail/suppressions/someone@example.com") is False


def test_execution_admin_actions_are_out_of_scope():
    assert _in_scope("POST", "/mail/execution/step1/resolve-sent") is False
    assert _in_scope("POST", "/mail/execution/step1/resolve-not-sent") is False
    assert _in_scope("POST", "/mail/execution/step1/resolve-prepare-blocked") is False


# --- Mailboxes ---------------------------------------------------------------


def test_bare_mailbox_list_is_in_scope():
    assert _in_scope("GET", "/mailboxes") is True


def test_mailbox_oauth_and_disconnect_routes_are_out_of_scope():
    assert _in_scope("GET", "/mailboxes/google/start") is False
    assert _in_scope("GET", "/mailboxes/mbx-1/google/gmail-send/start") is False
    assert _in_scope("GET", "/mailboxes/google/callback") is False
    assert _in_scope("POST", "/mailboxes/mbx-1/disconnect") is False


def test_write_methods_on_the_bare_mailbox_list_are_out_of_scope():
    """No route exists for these today, but the scope check itself should
    never treat this path as writable -- defense in depth."""
    assert _in_scope("POST", "/mailboxes") is False
    assert _in_scope("PATCH", "/mailboxes") is False
    assert _in_scope("DELETE", "/mailboxes") is False


# --- CRM contact lists: allowed ----------------------------------------------


def test_create_and_edit_a_list_are_in_scope():
    assert _in_scope("POST", "/crm/lists") is True
    assert _in_scope("PATCH", "/crm/lists/list-1") is True


def test_bulk_add_and_bulk_remove_are_in_scope():
    assert _in_scope("POST", "/crm/lists/list-1/contacts/bulk-add") is True
    assert _in_scope("POST", "/crm/lists/list-1/contacts/bulk-remove") is True


def test_single_contact_removal_from_a_list_is_in_scope():
    assert _in_scope("DELETE", "/crm/lists/list-1/contacts/contact-1") is True


# --- CRM: explicitly excluded ------------------------------------------------


def test_whole_list_deletion_is_out_of_scope():
    """Deliberately NOT granted -- only creation/editing/membership were
    approved, not deleting an entire list."""
    assert _in_scope("DELETE", "/crm/lists/list-1") is False


def test_list_reads_are_out_of_scope_for_the_operator_token():
    """Deliberately not duplicated here -- already reachable via the
    separate, existing ADMIN_SERVICE_READ_TOKEN's broader /crm/* GET
    scope. Keeping the operator token write-only for CRM avoids
    overlapping two independently-reasoned-about scopes."""
    assert _in_scope("GET", "/crm/lists") is False
    assert _in_scope("GET", "/crm/lists/list-1") is False
    assert _in_scope("GET", "/crm/lists/list-1/contacts") is False


def test_crm_contact_record_writes_are_out_of_scope():
    assert _in_scope("POST", "/crm/contacts") is False
    assert _in_scope("PATCH", "/crm/contacts/contact-1") is False
    assert _in_scope("DELETE", "/crm/contacts/contact-1") is False


def test_custom_field_writes_are_out_of_scope():
    assert _in_scope("POST", "/crm/custom-fields") is False
    assert _in_scope("PATCH", "/crm/custom-fields/field-1") is False


def test_luma_mapping_writes_are_out_of_scope():
    assert _in_scope("POST", "/crm/luma-question-mappings") is False
    assert _in_scope("PATCH", "/crm/luma-question-mappings/mapping-1") is False


def test_backup_and_import_are_out_of_scope():
    assert _in_scope("GET", "/crm/backup/export") is False
    assert _in_scope("GET", "/crm/import/batch-1") is False


# --- Unrelated surfaces --------------------------------------------------


def test_auth_and_admin_configuration_routes_are_out_of_scope():
    assert _in_scope("POST", "/auth/login") is False
    assert _in_scope("GET", "/auth/session") is False


def test_unrelated_top_level_surfaces_are_out_of_scope():
    assert _in_scope("GET", "/campaign") is False
    assert _in_scope("POST", "/sync/itf-contact") is False
