"""
Astronomic Hub internal login -- POST /auth/login, POST /auth/logout,
GET /auth/session. These three routes (plus /health and the two existing
webhook endpoints and the Google OAuth callback) are the ONLY ones exempt
from the session-cookie enforcement middleware in app/main.py -- every
other route in this application requires a valid session.

Nothing here ever returns a password or raw session token in a response
body, and nothing here ever logs one -- see AuthService's own docstring.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_auth_service
from app.services.auth_service import SESSION_COOKIE_NAME, AuthNotConfiguredError, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthStatusResponse(BaseModel):
    authenticated: bool


@router.post("/login", response_model=AuthStatusResponse)
async def login(payload: LoginRequest, response: Response, service: AuthService = Depends(get_auth_service)):
    try:
        valid = service.verify_credentials(payload.email, payload.password)
    except AuthNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    raw_token, expires_at = await service.create_session()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        expires=expires_at,
        path="/",
    )
    return AuthStatusResponse(authenticated=True)


@router.post("/logout", response_model=AuthStatusResponse)
async def logout(request: Request, response: Response, service: AuthService = Depends(get_auth_service)):
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    await service.invalidate_session(raw_token)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return AuthStatusResponse(authenticated=False)


@router.get("/session", response_model=AuthStatusResponse)
async def get_session_status(request: Request, service: AuthService = Depends(get_auth_service)):
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    authenticated = await service.validate_session(raw_token)
    return AuthStatusResponse(authenticated=authenticated)
