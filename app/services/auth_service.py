"""
AuthService -- Astronomic Hub's internal login. A single shared
email+password account guards the ENTIRE application as one boundary;
there is deliberately no signup, per-user accounts, roles, teams, or user
administration in this phase -- Hub is used by a small internal team, and
building real multi-user identity is explicitly out of scope until it's
actually needed (see the phase's own instructions: "Do not add signup,
registration, forgot-password, invitations, roles, teams, SSO, or user
administration in this phase").

Sessions are a real, persisted row (AuthSessionStore) rather than a
stateless signed token -- this is what makes "logout invalidates the
session" a true, immediate guarantee (delete the row) rather than "the
browser stops sending a cookie that would otherwise still be valid."

Nothing here ever logs a password or a raw session token -- only outcomes
(see verify_credentials()/validate_session()'s call sites in app/api/auth.py
and app/dependencies.py).
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.auth import AuthSession
from app.models.crm import normalize_email
from app.repositories.auth_session_store import AuthSessionStore
from app.services.password_hashing import verify_password

SESSION_COOKIE_NAME = "astro_session"
SESSION_TTL = timedelta(days=7)


class AuthNotConfiguredError(Exception):
    """AUTH_EMAIL/AUTH_PASSWORD_HASH aren't both set -- callers should
    surface this as a 503, matching itf_webhook_token's precedent. The app
    fails CLOSED: with no configured credentials, no login can ever
    succeed, rather than silently allowing anyone through."""


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, session_store: AuthSessionStore):
        self.session_store = session_store

    def verify_credentials(self, email: str, password: str) -> bool:
        if not settings.auth_email or not settings.auth_password_hash:
            raise AuthNotConfiguredError("Hub login is not configured (AUTH_EMAIL/AUTH_PASSWORD_HASH).")
        if normalize_email(email) != normalize_email(settings.auth_email):
            return False
        return verify_password(password, settings.auth_password_hash)

    async def create_session(self) -> tuple[str, datetime]:
        """Returns (raw_token, expires_at). The raw token is what goes in
        the HTTP-only cookie -- it is never itself persisted, only its hash."""
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + SESSION_TTL
        await self.session_store.create(
            AuthSession(session_token_hash=_hash_token(raw_token), created_at=now, expires_at=expires_at)
        )
        return raw_token, expires_at

    async def validate_session(self, raw_token: str | None) -> bool:
        if not raw_token:
            return False
        session = await self.session_store.get(_hash_token(raw_token))
        if session is None:
            return False
        if session.expires_at <= datetime.now(timezone.utc):
            await self.session_store.delete(session.session_token_hash)
            return False
        return True

    async def invalidate_session(self, raw_token: str | None) -> None:
        """Best-effort: a missing/already-invalid token is a silent no-op,
        so logout always "succeeds" from the caller's perspective."""
        if not raw_token:
            return
        await self.session_store.delete(_hash_token(raw_token))
