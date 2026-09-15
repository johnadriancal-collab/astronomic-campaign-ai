"""
Structural safety test for verify_sale_bot_webhook_token
(app/dependencies.py) -- pre-production hardening (2026-09-15), mirroring
tests/test_admin_service_auth.py's AST-based guarantee for the existing
admin/service tokens: the token must never be logged or echoed back in an
error response, verified by inspecting the source rather than trusting a
read-through.
"""

import ast
from pathlib import Path

_MODULE_PATH = "app/dependencies.py"


def _function_source(path: str, function_name: str) -> str:
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return ast.unparse(node)
    raise AssertionError(f"{function_name} not found in {path}")


def test_verify_sale_bot_webhook_token_never_calls_any_logger():
    source = _function_source(_MODULE_PATH, "verify_sale_bot_webhook_token")
    assert "logger" not in source, "verify_sale_bot_webhook_token must never log anything, token included"


def test_verify_sale_bot_webhook_token_error_details_never_interpolate_the_token():
    """Every HTTPException raised here uses a fixed, generic detail string
    -- never an f-string or concatenation that could embed `token`,
    `authorization`, or `settings.sale_bot_webhook_token`."""
    source = _function_source(_MODULE_PATH, "verify_sale_bot_webhook_token")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HTTPException":
            for kw in node.keywords:
                if kw.arg == "detail":
                    assert isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str), (
                        f"HTTPException detail must be a fixed string literal, got: {ast.unparse(kw.value)!r}"
                    )


def test_verify_sale_bot_webhook_token_uses_constant_time_comparison():
    source = _function_source(_MODULE_PATH, "verify_sale_bot_webhook_token")
    assert "hmac.compare_digest" in source, (
        "token comparison must use hmac.compare_digest (constant-time), never == or !="
    )
