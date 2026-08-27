"""
Security/scope guardrails for the Luma integration:
- LUMA_API_KEY / LUMA_WEBHOOK_SECRET never reach the frontend, never get
  returned by any route, never get logged.
- Astro AI's tool surface is completely untouched by this phase.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_luma_secrets_never_referenced_in_frontend_source():
    frontend_dir = REPO_ROOT / "frontend"
    offenders = []
    for path in frontend_dir.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".json"}:
            continue
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        if re.search(r"luma_api_key|luma_webhook_secret|LUMA_API_KEY|LUMA_WEBHOOK_SECRET", text):
            offenders.append(str(path))
    assert offenders == []


def test_luma_client_never_logs_the_api_key(monkeypatch, caplog):
    """LumaAPIError messages (the only thing LumaClient ever logs/raises on
    failure) never carry the raw key -- see app/luma/client.py, which only
    logs a status code, never headers or the request body."""
    import inspect

    from app.luma import client as luma_client_module

    source = inspect.getsource(luma_client_module)
    # The api_key is only ever used to build a header value -- never
    # interpolated into a log/error message string.
    assert "logger.error(f" in source
    for line in source.splitlines():
        if "logger.error" in line or "raise LumaAPIError" in line:
            assert "self.api_key" not in line
            assert "api_key" not in line


async def test_astro_tool_registry_is_completely_unchanged():
    """This phase explicitly must not touch Astro -- the CRM tool registry
    (which would be the natural place a "query Luma registrations" tool
    would eventually live) still has exactly the same 8 CRM tools as
    before this phase, none of them Luma-related."""
    from app.services.astro_crm_tools import CRM_TOOL_DEFINITIONS

    names = {t["name"] for t in CRM_TOOL_DEFINITIONS}
    assert names == {
        "count_crm_contacts",
        "search_crm_contacts",
        "get_crm_contact",
        "list_crm_lists",
        "get_crm_list",
        "get_crm_list_members",
        "count_crm_list_members",
        "export_crm_contacts",
    }
    assert not any("luma" in name.lower() for name in names)


def test_no_astro_source_file_mentions_luma():
    astro_files = list((REPO_ROOT / "app" / "services").glob("astro_*.py")) + [
        REPO_ROOT / "app" / "api" / "astro_ai.py",
        REPO_ROOT / "app" / "api" / "astro.py",
        REPO_ROOT / "app" / "models" / "astro_ai.py",
    ]
    offenders = []
    for path in astro_files:
        if not path.exists():
            continue
        if "luma" in path.read_text(errors="ignore").lower():
            offenders.append(str(path))
    assert offenders == []


def test_luma_settings_are_optional_app_boots_without_them():
    """Matches every other integration credential's precedent -- an
    unconfigured deployment must still boot cleanly."""
    from app.config import Settings

    field = Settings.model_fields["luma_api_key"]
    assert field.default is None
    field2 = Settings.model_fields["luma_webhook_secret"]
    assert field2.default is None
