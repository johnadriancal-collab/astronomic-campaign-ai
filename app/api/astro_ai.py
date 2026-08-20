"""
Astro AI chat -- POST /astro-ai/chat. Phase 1 general-assistant foundation:
no CRM/campaign/mailbox/activity data access, no persistence, no
autonomous actions. See app/services/astro_ai_service.py's module
docstring for the full scope and the backend-owned system prompt.

Authentication: this route carries NO auth dependency of its own on
purpose -- it doesn't need one. The centralized session-authentication
middleware (app/session_auth_middleware.py) denies every route by default
except its own short, explicit allowlist, and this path is deliberately
NOT on that list, so it requires a valid Hub session exactly like every
other data route in this application. There is nothing to weaken or
bypass here.

Every error path below returns a clean, generic message -- never
Anthropic's raw response body, never the API key, never anything that
could carry a secret.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.claude.client import (
    ClaudeAuthenticationError,
    ClaudeNotConfiguredError,
    ClaudeProviderError,
    ClaudeRateLimitError,
    ClaudeTimeoutError,
)
from app.dependencies import get_astro_ai_service
from app.models.astro_ai import AstroChatMessage, AstroChatRequest
from app.services.astro_ai_service import AstroAiService, AstroAiValidationError

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
