"""ABC + in-memory AuthSessionStore. Mirrors the rest of this app's
repository triples (Mailbox, MailCampaign, ...) exactly."""

from abc import ABC, abstractmethod
from datetime import datetime

from app.models.auth import AuthSession


class AuthSessionStore(ABC):
    @abstractmethod
    async def create(self, session: AuthSession) -> None: ...

    @abstractmethod
    async def get(self, session_token_hash: str) -> AuthSession | None: ...

    @abstractmethod
    async def delete(self, session_token_hash: str) -> None: ...

    @abstractmethod
    async def delete_expired(self, now: datetime) -> None: ...


class MemoryAuthSessionStore(AuthSessionStore):
    def __init__(self):
        self._rows: dict[str, AuthSession] = {}

    async def create(self, session: AuthSession) -> None:
        self._rows[session.session_token_hash] = session

    async def get(self, session_token_hash: str) -> AuthSession | None:
        return self._rows.get(session_token_hash)

    async def delete(self, session_token_hash: str) -> None:
        self._rows.pop(session_token_hash, None)

    async def delete_expired(self, now: datetime) -> None:
        expired = [h for h, s in self._rows.items() if s.expires_at <= now]
        for h in expired:
            del self._rows[h]
