"""ABC + in-memory MailboxStore -- see app/repositories/sqlite_mailbox_store.py
for the persistent implementation. Mirrors mail_campaign_store.py's exact
shape/conventions."""

from abc import ABC, abstractmethod

from app.models.mailbox import Mailbox


class MailboxNotFoundError(Exception):
    def __init__(self, mailbox_id: str):
        self.mailbox_id = mailbox_id
        super().__init__(f"Mailbox not found: {mailbox_id}")


class MailboxStore(ABC):
    @abstractmethod
    async def create(self, mailbox: Mailbox) -> None: ...

    @abstractmethod
    async def get(self, mailbox_id: str) -> Mailbox | None: ...

    @abstractmethod
    async def get_by_google_user_id(self, google_user_id: str) -> Mailbox | None: ...

    @abstractmethod
    async def get_by_email(self, email: str) -> Mailbox | None: ...

    @abstractmethod
    async def save(self, mailbox: Mailbox) -> None: ...

    @abstractmethod
    async def list(self) -> list[Mailbox]: ...


class MemoryMailboxStore(MailboxStore):
    def __init__(self):
        self._rows: dict[str, Mailbox] = {}

    async def create(self, mailbox: Mailbox) -> None:
        self._rows[mailbox.mailbox_id] = mailbox

    async def get(self, mailbox_id: str) -> Mailbox | None:
        return self._rows.get(mailbox_id)

    async def get_by_google_user_id(self, google_user_id: str) -> Mailbox | None:
        for mailbox in self._rows.values():
            if mailbox.google_user_id == google_user_id:
                return mailbox
        return None

    async def get_by_email(self, email: str) -> Mailbox | None:
        for mailbox in self._rows.values():
            if mailbox.email == email:
                return mailbox
        return None

    async def save(self, mailbox: Mailbox) -> None:
        if mailbox.mailbox_id not in self._rows:
            raise MailboxNotFoundError(mailbox.mailbox_id)
        self._rows[mailbox.mailbox_id] = mailbox

    async def list(self) -> list[Mailbox]:
        return sorted(self._rows.values(), key=lambda m: m.connected_at)
