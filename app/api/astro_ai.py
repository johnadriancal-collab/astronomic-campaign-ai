"""
Astro AI chat -- POST /astro-ai/chat. Phase 1 general-assistant foundation:
no CRM/campaign/mailbox/activity data access, no persistence, no
autonomous actions. See app/services/astro_ai_service.py's module
docstring for the full scope and the backend-owned system prompt.

GET /astro-ai/exports/{export_id} (added for the CRM CSV export
capability) serves the CSV bytes a prior export_crm_contacts tool call
generated (app/services/astro_crm_tools.py), held in AstroExportStore for
15 minutes. `export_id` is an opaque uuid4 with no guessable structure;
an unknown or expired id returns a plain 404 with no CRM data in the
body, identically for both cases so an expired vs. never-existed id can't
be distinguished from the outside.

Authentication: neither route carries an auth dependency of its own on
purpose -- neither needs one. The centralized session-authentication
middleware (app/session_auth_middleware.py) denies every route by default
except its own short, explicit allowlist, and both paths are deliberately
NOT on that list, so each requires a valid Hub session exactly like every
other data route in this application. There is nothing to weaken or
bypass here.

Every error path below returns a clean, generic message -- never
Anthropic's raw response body, never the API key, never anything that
could carry a secret.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.claude.client import (
    ClaudeAuthenticationError,
    ClaudeNotConfiguredError,
    ClaudeProviderError,
    ClaudeRateLimitError,
    ClaudeTimeoutError,
)
from app.dependencies import get_astro_ai_service, get_astro_export_store
from app.models.astro_ai import AstroChatMessage, AstroChatRequest
from app.services.astro_ai_service import AstroAiService, AstroAiValidationError
from app.services.astro_export_store import AstroExportStore

router = APIRouter(prefix="/astro-ai", tags=["astro-ai"])


@router.post("/chat", response_model=AstroChatMessage)
async def chat(payload: AstroChatRequest, service: AstroAiService = Depends(get_astro_ai_service)):
    try:
        return await service.chat(payload.messages)
    except AstroAiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ClaudeNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ClaudeAuthenticationError:
        raise HTTPException(status_code=502, detail="Astro AI is temporarily unavailable (provider authentication error).")
    except ClaudeRateLimitError:
        raise HTTPException(status_code=429, detail="Astro AI is getting a lot of requests right now -- please try again shortly.")
    except ClaudeTimeoutError:
        raise HTTPException(status_code=504, detail="Astro AI took too long to respond -- please try again.")
    except ClaudeProviderError:
        raise HTTPException(status_code=502, detail="Astro AI is temporarily unavailable -- please try again shortly.")


@router.get("/exports/{export_id}")
async def download_export(export_id: str, store: AstroExportStore = Depends(get_astro_export_store)):
    export = store.get(export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="This export is no longer available -- ask Astro to export it again.")
    return Response(
        content=export.csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )
